"""Browse completed recorder sessions relative to a verified recording volume."""

import errno
import hashlib
import heapq
import json
import os
import re
import stat
import time
import zipfile
from contextlib import ExitStack, closing, contextmanager

_SESSION = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}")
_VIDEOS = ("video.y4m", "video.mjpeg")
_PAGE_SIZE = 30
_MAX_MANIFEST = 65536


class RecordingSourceChanged(RuntimeError):
    """The destination or volume changed after the catalog entry was read."""


def _target_id(target, expected):
    fingerprint = json.dumps([str(target.path), target.identity, os.fstat(target.fd).st_ino],
                             sort_keys=True).encode()
    identifier = hashlib.sha256(fingerprint).hexdigest()
    if expected is not None and expected != identifier:
        raise RecordingSourceChanged("The recording source changed. Refresh the recording list.")
    return identifier


def _missing():
    return FileNotFoundError("The completed recording or file is unavailable.")


@contextmanager
def _session(storage, target, name):
    if not _SESSION.fullmatch(name):
        raise _missing()
    with ExitStack() as stack:
        try:
            mount = storage.fd_mount_id(target.fd)
            fd = os.open(name, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW, dir_fd=target.fd)
            stack.callback(os.close, fd)
            if storage.fd_mount_id(fd) != mount:
                raise _missing()

            def regular_file(filename):
                # Check before opening a device and again after opening to handle replacements.
                entry = os.stat(filename, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                    raise _missing()
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
                child = os.open(filename, flags, dir_fd=fd)
                stack.callback(os.close, child)
                actual = os.fstat(child)
                if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1
                        or not os.path.samestat(entry, actual) or storage.fd_mount_id(child) != mount):
                    raise _missing()
                return child

            manifest = regular_file("session.json")
            with open(manifest, "rb", closefd=False) as file:
                data = file.read(_MAX_MANIFEST + 1)
            if len(data) > _MAX_MANIFEST:
                raise _missing()
            meta = json.loads(data)
            if (not isinstance(meta, dict) or meta.get("schema_version") != 1
                    or meta.get("status") != "complete" or meta.get("video") not in _VIDEOS
                    or type(meta.get("frames")) is not int or meta["frames"] < 1
                    or not isinstance(meta.get("started_at"), str)):
                raise _missing()
            video = meta["video"]
            files = {video: regular_file(video), "frames.csv": regular_file("frames.csv"),
                     "session.json": manifest}
        except (ValueError, RecursionError) as exc:
            raise _missing() from exc
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.ENXIO):
                raise _missing() from exc
            raise
        yield meta, files, fd


def _page_names(target, cursor):
    # Open each candidate separately so a damaged entry cannot block the rest of the page.
    try:
        with os.scandir(target.fd) as entries:
            return heapq.nlargest(_PAGE_SIZE + 1, (
                entry.name for entry in entries if _SESSION.fullmatch(entry.name)
                and (cursor is None or entry.name < cursor)
            ))
    except OSError as exc:
        if exc.errno in (errno.EIO, errno.EROFS):
            raise OSError(f"Could not read the recording directory {target.path} ({exc}). "
                          "Check the drive on the Recording drives page.") from exc
        raise


def list_recordings(storage, directory, identity, cursor=None, *, source="current", target_id=None, session=None):
    if source not in ("current", "nvme", "default", "usb"):
        raise ValueError("Unknown recording source.")
    if cursor is not None and not _SESSION.fullmatch(cursor):
        raise ValueError("Invalid recording page cursor.")
    if session is not None:
        if cursor is not None:
            raise ValueError("Choose a recording ID or a page cursor.")
        if not _SESSION.fullmatch(session):
            raise ValueError("Invalid recording ID.")
    rows = []
    unreadable = []
    with storage.open_target(directory, identity, create=False) as target:
        identifier = _target_id(target, target_id)
        suffix = f"?source={source}&target_id={identifier}"
        names = [session] if session is not None else _page_names(target, cursor)
        for name in names[:_PAGE_SIZE]:
            try:
                with _session(storage, target, name) as (meta, files, _):
                    rows.append({
                        "name": name, "started_at": meta["started_at"], "frames": meta["frames"],
                        "archive": {"name": name + ".zip", "url": f"/api/recordings/{name}/archive.zip{suffix}"},
                        "files": [{"name": filename, "size_bytes": os.fstat(fd).st_size,
                                   "url": f"/api/recordings/{name}/{filename}{suffix}"}
                                  for filename, fd in files.items()],
                    })
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno not in (errno.EIO, errno.EROFS):
                    raise
                unreadable.append(name)
    return {"directory": str(target.path), "target_id": identifier, "sessions": rows,
            "unreadable_sessions": unreadable,
            "next_cursor": names[_PAGE_SIZE - 1] if len(names) > _PAGE_SIZE else None}


@contextmanager
def _recording_files(storage, directory, identity, session, target_id):
    with storage.open_target(directory, identity, create=False) as target:
        _target_id(target, target_id)
        with _session(storage, target, session) as (_, files, _):
            yield files


def open_recording_file(storage, directory, identity, session, filename, *, target_id=None):
    if filename not in (*_VIDEOS, "frames.csv", "session.json"):
        raise _missing()
    with _recording_files(storage, directory, identity, session, target_id) as files:
        if filename not in files:
            raise _missing()
        fd = files[filename]
        os.lseek(fd, 0, os.SEEK_SET)
        return os.fdopen(os.dup(fd), "rb")


class _ZipBuffer:
    """Unseekable zipfile output, drained between reads of the source media."""

    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)
        return len(data)

    def flush(self):
        pass

    def take(self):
        data = self.data
        self.data = bytearray()
        return data


def _zip_chunks(files):
    buffer = _ZipBuffer()
    originals = {name: os.fstat(fd) for name, fd in files.items()}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, original in originals.items():
            size = original.st_size
            # ZIP timestamps have no zone field and can only represent 1980–2107.
            utc = time.gmtime(original.st_mtime)[:6]
            saved_at = min(max(utc, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))
            info = zipfile.ZipInfo(name, saved_at)
            info.file_size = size  # zipfile selects ZIP64 before streaming a large member.
            with archive.open(info, "w") as member:
                offset = 0
                while offset < size:
                    data = os.pread(files[name], min(size - offset, 128 * 1024), offset)
                    if not data:
                        raise OSError("The recording changed during the download.")
                    member.write(data)
                    offset += len(data)
                    yield buffer.take()
        for name, original in originals.items():
            actual = os.fstat(files[name])
            if (actual.st_size, actual.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
                raise OSError("The recording changed during the download.")
    # Only publish the central directory once all three inputs were read successfully.
    yield buffer.take()


@contextmanager
def open_recording_archive(storage, directory, identity, session, *, target_id=None):
    with _recording_files(storage, directory, identity, session, target_id) as files:
        with closing(_zip_chunks(files)) as chunks:
            yield chunks
