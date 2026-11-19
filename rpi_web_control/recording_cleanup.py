"""Plan and remove completed recorder sessions on a verified, dedicated data volume."""

import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from .downloads import RecordingSourceChanged, _SESSION, _VIDEOS, _session, _target_id
from .firmware import atomic_text


_CHANGED = "The recording directory changed. Review a new cleanup plan."
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _inode(info):
    return [info.st_dev, info.st_ino]


def _file_stamp(info):
    return [info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size]


def private_recording_directory(fd):
    # The recorder creates 0750 directories. In a directory writable by another
    # account a filename could be substituted between checking it and unlinkat().
    info = os.fstat(fd)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022


@contextmanager
def _locked(fd, mode):
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RecordingSourceChanged("The recording directory is busy. Review the cleanup plan again.") from exc
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _journal(state_dir, target_id, media_revision, name):
    key = _digest([target_id, media_revision, name])
    return Path(state_dir) / "recording-cleanup" / (key + ".json")


def _member_names(fd):
    with os.scandir(fd) as entries:
        names = [entry.name for entry in islice(entries, 4)]
    if len(names) > 3:
        raise RecordingSourceChanged(_CHANGED)
    return sorted(names)


@dataclass
class Candidate:
    name: str
    intent: dict
    remaining: list
    directory_ctime_ns: int

    @property
    def size(self):
        return sum(self.intent["files"][name][-1] for name in self.remaining)


def _initial_intent(storage, target, name):
    with _session(storage, target, name) as (meta, files, fd):
        # Downloads support old manifests. For deletion require the recorder's
        # capture metadata and the exact contents of a finished session directory.
        if (meta.get("backend") not in ("picamera2", "simulated", "simulator")
                or not isinstance(meta.get("settings"), dict)
                or type(meta.get("start_sensor_ns")) is not int
                or not isinstance(meta.get("ended_at"), str)
                or set(_member_names(fd)) != files.keys() or not private_recording_directory(fd)):
            return None
        return {"directory": _inode(os.fstat(fd)), "files": {
            filename: _file_stamp(os.fstat(child)) for filename, child in files.items()}}


def _verify_member(storage, fd, filename, expected, mount):
    entry = os.stat(filename, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1 or _file_stamp(entry) != expected:
        raise RecordingSourceChanged(_CHANGED)
    child = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=fd)
    try:
        if storage.fd_mount_id(child) != mount or _file_stamp(os.fstat(child)) != expected:
            raise RecordingSourceChanged(_CHANGED)
    finally:
        os.close(child)


@contextmanager
def _remaining(storage, target, name, intent):
    # A trusted journal permits missing original members after a partial failure.
    # Each surviving member still has to match its pre-deletion inode and metadata.
    files = intent["files"]
    if not any(set(files) == {video, "frames.csv", "session.json"} for video in _VIDEOS):
        raise RuntimeError("Invalid recording cleanup journal.")
    fd = os.open(name, _FLAGS, dir_fd=target.fd)
    try:
        info = os.fstat(fd)
        mount = storage.fd_mount_id(target.fd)
        remaining = _member_names(fd)
        if (not private_recording_directory(fd) or _inode(info) != intent["directory"]
                or storage.fd_mount_id(fd) != mount or set(remaining) - files.keys()):
            raise RecordingSourceChanged(_CHANGED)
        for filename in remaining:
            _verify_member(storage, fd, filename, files[filename], mount)
        yield fd, Candidate(name, intent, remaining, info.st_ctime_ns)
    finally:
        os.close(fd)


def _scan(storage, target, state_dir, media_revision, session, target_id):
    identifier = _target_id(target, target_id)
    if not private_recording_directory(target.fd):
        raise ValueError("Cleanup requires a recording directory owned and writable only by the controller.")
    if session is not None and (not isinstance(session, str) or not _SESSION.fullmatch(session)):
        raise ValueError("Select a recording from the list.")
    candidates = []
    skipped = 0
    names = [session]
    if session is None:
        names = []
        with os.scandir(target.fd) as entries:
            for entry in entries:
                if _SESSION.fullmatch(entry.name):
                    names.append(entry.name)
                else:
                    skipped += 1
        names.sort()
    for name in names:
        journal = _journal(state_dir, identifier, media_revision, name)
        try:
            if journal.exists():
                intent = json.loads(journal.read_text())
            else:
                intent = _initial_intent(storage, target, name)
            if intent:
                with _remaining(storage, target, name, intent) as (_, candidate):
                    candidates.append(candidate)
        except (FileNotFoundError, NotADirectoryError, RecordingSourceChanged):
            continue
    if session is not None and not candidates:
        raise ValueError("This folder is not an eligible, completed controller recording.")
    skipped += len(names) - len(candidates)
    signature = [identifier, media_revision, session, skipped,
                 os.fstat(target.fd).st_ctime_ns if session is None else None,
                 [vars(item) for item in candidates]]
    return candidates, {"target_id": identifier, "revision": _digest(signature),
                        "count": len(candidates), "bytes": sum(item.size for item in candidates), "skipped": skipped}


def plan_cleanup(storage, target, *, state_dir, media_revision, session=None, target_id=None):
    with _locked(target.fd, fcntl.LOCK_SH):
        _, plan = _scan(storage, target, state_dir, media_revision, session, target_id)
        return plan


def _verify_target_fd(storage, target):
    current_fd = os.open(target.path, _FLAGS)
    try:
        if (not private_recording_directory(target.fd)
                or not os.path.samestat(os.fstat(current_fd), os.fstat(target.fd))
                or storage.fd_mount_id(current_fd) != storage.fd_mount_id(target.fd)):
            raise RecordingSourceChanged(_CHANGED)
    finally:
        os.close(current_fd)


def _remove(storage, target, candidate, journal):
    storage.identity(target.path, target.identity)
    name, intent = candidate.name, candidate.intent
    with _remaining(storage, target, name, intent) as (fd, observed):
        if candidate != observed:
            raise RecordingSourceChanged(_CHANGED)
        journal.parent.mkdir(mode=0o700, exist_ok=True)
        atomic_text(journal, json.dumps(intent), 0o600)
        remaining = set(observed.remaining)
        mount = storage.fd_mount_id(fd)
        # Keep session.json until last so a partially deleted session retains its metadata.
        for filename in sorted(remaining, key=lambda key: (key == "session.json", key == "frames.csv")):
            _verify_target_fd(storage, target)
            entry = os.stat(name, dir_fd=target.fd, follow_symlinks=False)
            if (not private_recording_directory(fd) or _inode(entry) != intent["directory"]
                    or set(_member_names(fd)) != remaining):
                raise RecordingSourceChanged(_CHANGED)
            _verify_member(storage, fd, filename, intent["files"][filename], mount)
            os.unlink(filename, dir_fd=fd)
            remaining.remove(filename)
        _verify_target_fd(storage, target)
        if _inode(os.stat(name, dir_fd=target.fd, follow_symlinks=False)) != intent["directory"]:
            raise RecordingSourceChanged(_CHANGED)
        os.fsync(fd)
        os.rmdir(name, dir_fd=target.fd)
        os.fsync(target.fd)
    journal.unlink()


def execute_cleanup(storage, target, *, state_dir, media_revision, target_id, revision, session=None):
    if not target_id or not revision:
        raise ValueError("Review the cleanup plan before confirming.")
    with _locked(target.fd, fcntl.LOCK_EX):
        try:
            candidates, plan = _scan(storage, target, state_dir, media_revision, session, target_id)
        except ValueError as exc:
            raise RecordingSourceChanged(_CHANGED) from exc
        if plan["revision"] != revision:
            raise RecordingSourceChanged(_CHANGED)
        removed = removed_bytes = 0
        for candidate in candidates:
            journal = _journal(state_dir, target_id, media_revision, candidate.name)
            try:
                _remove(storage, target, candidate, journal)
            except (OSError, ValueError, RecordingSourceChanged) as exc:
                raise RecordingSourceChanged(
                    f"Cleanup interrupted after {removed} completed deletions. Review the remaining files. {exc}"
                ) from exc
            removed += 1
            removed_bytes += candidate.size
        return {"removed_count": removed, "removed_bytes": removed_bytes, "skipped": plan["skipped"]}
