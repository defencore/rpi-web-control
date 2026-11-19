"""Provision dedicated PCIe NVMe and USB recording volumes in the host mount namespace."""

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import struct
import subprocess
import uuid

from .firmware import atomic_text
from .nvme_client import MOUNTPOINT, USB_MOUNTPOINT
from .storage import read_mounts


TARGET = Path(MOUNTPOINT)
OPTIONS = "rw,nosuid,nodev,noexec"
DISK = re.compile(r"nvme(\d+)n\d+")
FSTAB_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
COLUMNS = ("NAME,TYPE,TRAN,MAJ:MIN,SIZE,RO,MODEL,SERIAL,FSTYPE,UUID,LABEL,PARTUUID,"
           "PARTLABEL,PARTTYPE,PARTFLAGS,PTTYPE,PTUUID,MOUNTPOINTS")
SYSTEM_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",  # EFI system
    "21686148-6449-6e6f-744e-656564454649",  # BIOS boot
    "bc13c2ff-59e6-4262-a352-b275fd6f7172",  # Extended boot
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",  # x86-64 root
    "b921b045-1df0-41c3-af44-4c6f280d3fae",  # ARM64 root
    "69dad710-2ce4-4e3c-b16c-21a1d49abed3",  # ARM root
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f",  # Linux swap
    "0xef", "0x82", "0x8e", "0xfd",
}


def _command(args, *, data=None, fds=(), timeout=30):
    try:
        result = subprocess.run(args, input=data or "", capture_output=True, text=True,
                                pass_fds=tuple(fds), timeout=timeout, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                                                                          "LC_ALL": "C"}, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{args[0]} could not complete: {exc}") from exc
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"{args[0]}: {message[-1024:]}")
    return result.stdout


def _rows(tree):
    for row in tree:
        yield row
        yield from _rows(row.get("children", []))


def _devno(number):
    major, minor = str(number).split(":")
    return os.makedev(int(major), int(minor))


def _reserved(node, table_type):
    flags = int(str(node.get("partflags") or "0"), 0)
    # MBR active partitions and GPT platform-required or legacy-bootable
    # partitions retain their protection even if they are currently unmounted.
    boot_flags = {"dos": 0x80, "gpt": 0x5}.get(table_type, 0)
    labels = {(node.get(key) or "").strip().lower() for key in ("label", "partlabel")}
    reserved_fs = {"swap", "lvm2_member", "linux_raid_member", "crypto_luks", "zfs_member", "swsuspend", "jbd"}
    return (bool(flags & boot_flags) or (node.get("fstype") or "").lower() in reserved_fs
            or (node.get("parttype") or "").lower() in SYSTEM_TYPES
            or bool(labels & {"boot", "bootfs", "root", "rootfs", "system-boot", "writable",
                              "efi system partition", "bios boot", "/", "/boot", "/usr"}))


def _fstab_rows(text):
    for line in text.splitlines():
        cols = line.split("#", 1)[0].split()
        if len(cols) >= 2:
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), cols[1])
            # A trailing slash or an administrator's symlink refers to the same mountpoint.
            if point.startswith("/"):
                point = str(Path(point).resolve())
            yield cols[0], point, line


def _referenced(source, row, path):
    source = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), source)
    if source.startswith("/dev/"):
        return Path(source).resolve() == path.resolve()
    tag, eq, value = source.partition("=")
    if eq and tag in {"UUID", "PARTUUID", "LABEL", "PARTLABEL"} and row.get(tag.lower()):
        actual, value = row[tag.lower()], value.strip('"')
        return actual.lower() == value.lower() if tag in {"UUID", "PARTUUID"} else actual == value
    return False


class NvmeManager:
    def __init__(self, *, sysfs=Path("/sys"), proc=Path("/proc"), dev=Path("/dev"),
                 fstab=Path("/etc/fstab"), target=None, lock=Path("/run/rpi-web-control-nvme.lock"), medium="nvme"):
        if medium not in ("nvme", "usb"):
            raise ValueError("Choose PCIe NVMe or USB storage.")
        self.medium = medium
        target = target or (USB_MOUNTPOINT if medium == "usb" else TARGET)
        self.sysfs, self.proc, self.dev = map(Path, (sysfs, proc, dev))
        self.fstab, self.target, self.lock = map(Path, (fstab, target, lock))
        self.directory = self.target / "recordings"

    def _fstab(self):
        info = self.fstab.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("The fstab file must be a root-owned, protected regular file.")
        return self.fstab.read_text()

    def _location(self):
        if self.target.resolve() != self.target:
            raise ValueError("The managed recording mount path must not contain symbolic links.")
        for path in (self.target.parent, self.target):
            if path.exists():
                info = path.stat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError("The managed recording mount path must contain protected, root-owned directories.")

    def _tree(self):
        return json.loads(_command(["lsblk", "--json", "--tree", "--bytes", "--output", COLUMNS]))["blockdevices"]

    def _account(self):
        account = pwd.getpwnam("rpi-web-control")
        return account.pw_uid, account.pw_gid

    def _working_directory(self, number, create=False):
        """Operate through directory FDs and verify the filesystem after opening."""
        uid, gid = self._account()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        base = os.open(self.target, flags)
        try:
            root = os.fstat(base)
            if root.st_dev != _devno(number):
                raise ValueError("The recording filesystem changed at its mountpoint.")
            for info in (self.target.parent.stat(), root):
                execute = stat.S_IXUSR if info.st_uid == uid else stat.S_IXGRP if info.st_gid == gid else stat.S_IXOTH
                if not info.st_mode & execute:
                    raise ValueError("The service account cannot traverse the recording mountpoint.")
            created = False
            if create:
                try:
                    os.mkdir("recordings", 0o750, dir_fd=base)
                    created = True
                except FileExistsError:
                    pass
            child = os.open("recordings", flags, dir_fd=base)
            try:
                if created:
                    os.fchown(child, uid, gid)
                    os.fchmod(child, 0o750)
                info = os.fstat(child)
                if (info.st_dev != _devno(number) or info.st_uid != uid or info.st_mode & 0o700 != 0o700):
                    raise ValueError("The recordings directory must be owned and writable by rpi-web-control.")
            except OSError:
                if created:
                    os.rmdir("recordings", dir_fd=base)
                raise
            finally:
                os.close(child)
        finally:
            os.close(base)

    def _managed_line(self, fsuuid):
        return (f"UUID={fsuuid} {self.target} ext4 {OPTIONS},nofail,x-systemd.device-timeout=5s "
                f"0 2 # rpi-web-control {'USB' if self.medium == 'usb' else 'NVMe'}")

    def _managed_entry(self, line):
        source = line.split(maxsplit=1)[0]
        tag, _, value = source.partition("=")
        return tag == "UUID" and FSTAB_UUID.fullmatch(value) and line == self._managed_line(value)

    def _secure_mount(self, number):
        for line in (self.proc / "self/mountinfo").read_text().splitlines():
            fields = line.split(" - ", 1)[0].split()
            if fields[0] == number:
                return {"nosuid", "nodev", "noexec"}.issubset(fields[5].split(","))
        return False

    def _physical_disk(self, disk):
        """Only admit a kernel-confirmed disk attached through the requested bus."""
        name = disk["name"]
        if disk["type"] != "disk":
            return None
        if self.medium == "nvme":
            match = DISK.fullmatch(name)
            if not match:
                return None
            controller = self.sysfs / "class/nvme" / ("nvme" + match[1])
            block = self.sysfs / "class/block" / name
            if (controller.exists() and block.exists() and block.resolve().parent == controller.resolve()
                    and (controller / "transport").read_text().strip() == "pcie"):
                return block
        elif re.fullmatch(r"sd[a-z]+", name) and disk.get("tran") == "usb":
            block = self.sysfs / "class/block" / name
            if not block.exists():
                return None
            physical = block.resolve()
            if not physical.is_relative_to(self.sysfs / "devices") or physical.parent.name != "block":
                return None
            usb_bus = self.sysfs / "bus/usb"
            for parent in physical.parents:
                driver = parent / "driver"
                if (driver.is_symlink() and driver.exists() and (parent / "subsystem").resolve() == usb_bus
                        and driver.resolve() in (usb_bus / "drivers/usb-storage", usb_bus / "drivers/uas")):
                    return block
        return None

    def _system_disks(self, mounts, tree):
        """Find every physical disk under root and the mounted boot filesystems."""
        devices = {row["maj:min"]: row["type"] for row in _rows(tree)}
        protected = set()

        def trace(devno, seen):
            if devno in seen:
                raise ValueError
            node = (self.sysfs / "dev/block" / devno).resolve(strict=True)
            if not node.is_relative_to(self.sysfs / "devices") or (node / "dev").read_text().strip() != devno:
                raise ValueError
            kind = devices[devno]
            if (node / "partition").is_file():
                if kind != "part":
                    raise ValueError
                trace((node.parent / "dev").read_text().strip(), seen | {devno})
                return
            slaves = list((node / "slaves").iterdir())
            if slaves:
                for slave in slaves:
                    trace((slave / "dev").read_text().strip(), seen | {devno})
            elif kind == "disk" and not node.is_relative_to(self.sysfs / "devices/virtual"):
                protected.add(devno)
            else:
                raise ValueError

        # Btrfs, overlay, network roots and other non-single-device filesystems
        # require additional evidence about backing media. Refuse management there.
        root = [m for m in mounts.values() if m.path == Path("/")]
        firmware = [m for m in mounts.values() if m.path == Path("/boot/firmware")]
        if (len(firmware) != 1 or firmware[0].fs != "vfat" or firmware[0].root != "/"
                or any(m.dev == firmware[0].dev for m in root)):
            return set(), ("Cannot verify the Pi boot disk. Verify and mount its separate FAT volume "
                           "at /boot/firmware before managing recording drives.")
        if not root:
            return set(), "Cannot verify the root filesystem of the running system."
        try:
            for m in mounts.values():
                if m.path == Path("/") or m.path == Path("/efi") or m.path.is_relative_to("/boot"):
                    if m.fs not in ("ext2", "ext3", "ext4", "vfat"):
                        raise ValueError
                    trace(m.dev, set())
        except (OSError, KeyError, ValueError, RuntimeError):
            return set(), "Cannot verify which physical disks contain the running system or boot filesystems."
        return protected, None

    def _snapshot(self):
        tree = self._tree()
        mounts = read_mounts(self.proc / "self/mountinfo")
        table = self._fstab()
        system_disks, system_error = self._system_disks(mounts, tree)
        boot_id = (self.proc / "sys/kernel/random/boot_id").read_text().strip()
        if not FSTAB_UUID.fullmatch(boot_id):
            raise ValueError("Cannot verify the current system boot identity.")
        entries = list(_fstab_rows(table))
        targets = [line for _, point, line in entries if point == str(self.target)]
        mounted_at = [m for m in mounts.values() if m.path == self.target]
        self._location()
        target_reason = None
        if len(targets) > 1 or any(not self._managed_entry(line) for line in targets):
            target_reason = "The mountpoint is reserved by another fstab entry."
        if len(mounted_at) > 1 or any(m.path != self.target and m.path.is_relative_to(self.target)
                                      for m in mounts.values()):
            target_reason = "The recording mountpoint contains conflicting mounts."
        if not mounted_at and self.target.exists() and any(self.target.iterdir()):
            target_reason = "The unmounted recording directory must be empty."
        # A swapfile on a detached filesystem can still pin that filesystem.
        swaps = set()
        for line in (self.proc / "swaps").read_text().splitlines()[1:]:
            path = Path(line.split()[0].replace("\\040", " "))
            if path.exists():
                info = path.stat()
                swaps.add(info.st_rdev if stat.S_ISBLK(info.st_mode) else info.st_dev)

        used_uuids = {}
        for node in _rows(tree):
            if node.get("uuid"):
                used_uuids.setdefault(node["uuid"].lower(), set()).add(node["maj:min"])
        disks = []
        for disk in tree:
            name = disk["name"]
            block = self._physical_disk(disk)
            if block is None:
                continue
            partition_prefix = name if self.medium == "usb" else name + "p"
            serial = (disk.get("serial") or "").strip()
            seq = (block / "diskseq").read_text().strip()
            nodes = list(_rows([disk]))
            volumes = []
            reason = target_reason
            if ((self.medium == "nvme" and not serial) or not seq.isdecimal() or not int(seq)
                    or int(disk["size"]) <= 0):
                reason = "The disk identity could not be verified."
            if mounted_at and mounted_at[0].dev not in {n["maj:min"] for n in nodes}:
                reason = "Another device is mounted at the recording mountpoint."
            # lsblk can see a partition table transition mid-discovery. Never act
            # on a disk until every kernel partition is accounted for in the tree.
            observed_parts = {p.name for p in block.iterdir() if (p / "partition").is_file()}
            listed_parts = {n["name"] for n in disk.get("children", []) if n["type"] == "part"}
            if observed_parts != listed_parts:
                reason = "The disk partition list changed or is incomplete. Refresh the drives."
            for node in nodes:
                node_path = self.dev / node["name"]
                is_whole = node is disk
                is_part = re.fullmatch(partition_prefix + r"[1-9]\d*", node["name"]) and node["type"] == "part"
                if not (is_part or is_whole):
                    reason = "The disk has a stacked or active block device."
                    continue
                node_sys = self.sysfs / "class/block" / node["name"]
                if is_part and node_sys.resolve().parent != block.resolve():
                    raise ValueError("The disk partition's parent changed.")
                if (node_sys / "dev").read_text().strip() != node["maj:min"]:
                    raise ValueError("The disk changed during discovery.")
                if node["ro"] or any((node_sys / "holders").iterdir()) or _devno(node["maj:min"]) in swaps:
                    reason = "The disk or a partition is read-only, held by another device, or used for swap."
                if _reserved(node, disk.get("pttype")):
                    reason = "The disk contains a boot, system, swap or reserved filesystem."
                locations = set(filter(None, node.get("mountpoints") or []))
                locations.update(str(m.path) for m in mounts.values() if m.dev == node["maj:min"]
                                 or _referenced(m.source, node, node_path))
                if locations - {str(self.target)}:
                    reason = "The disk or a partition is mounted or in use elsewhere."
                for src, point, line in entries:
                    if _referenced(src, node, node_path) and not (point == str(self.target) and self._managed_entry(line)):
                        reason = "The disk is referenced by an existing fstab entry."
                if is_whole and disk.get("children"):
                    continue
                fsuuid = node.get("uuid") or ""
                volume_reason = None
                if node.get("fstype") != "ext4" or not FSTAB_UUID.fullmatch(fsuuid):
                    volume_reason = "Mounting an existing volume requires ext4 with a filesystem UUID."
                elif len(used_uuids[fsuuid.lower()]) != 1:
                    volume_reason = "Another device has the same filesystem UUID."
                here = bool(mounted_at and mounted_at[0].dev == node["maj:min"])
                ready = False
                if here:
                    m = mounted_at[0]
                    if m.root != "/" or m.fs != "ext4" or m.read_only:
                        reason = "The current mount is not a writable ext4 volume root."
                    elif not self._secure_mount(m.number):
                        reason = "The existing mount requires nodev, nosuid and noexec for managed recording."
                    elif self._managed_line(fsuuid) in targets and not volume_reason:
                        try:
                            self._working_directory(node["maj:min"])
                            ready = True
                        except (OSError, ValueError, KeyError):
                            pass
                volumes.append({"device": str(node_path), "devno": node["maj:min"], "filesystem": node.get("fstype"),
                                "uuid": fsuuid or None, "size_bytes": int(node["size"]), "mounted": bool(locations), "ready": ready,
                                "mountpoints": sorted(locations), "reason": volume_reason})
            if system_error:
                reason = system_error
            elif disk["maj:min"] in system_disks:
                reason = "This disk contains the running system or a boot filesystem."
            for volume in volumes:
                if reason:
                    volume.update(reason=reason, ready=False)
                elif mounted_at and str(self.target) not in volume["mountpoints"]:
                    volume["reason"] = "Another volume occupies the recording mountpoint."
                volume["can_mount"] = not volume["ready"] and not volume["reason"]
            erase_reason = reason
            if mounted_at or targets and any(self._managed_line(v["uuid"]) in targets for v in volumes if v["uuid"]):
                erase_reason = erase_reason or "The disk is mounted or reserved as the recording volume."
            signature = [boot_id, self.medium, str(block.resolve()), seq, disk, table,
                         sorted((str(m.path), m.dev, m.number) for m in mounted_at), erase_reason, volumes]
            revision = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
            disks.append({"device": str(self.dev / name), "model": (disk.get("model") or "").strip(),
                          "serial": serial, "size_bytes": int(disk["size"]), "revision": revision,
                          "can_prepare": not erase_reason, "reason": erase_reason, "volumes": volumes,
                          "_seq": seq, "_nodes": nodes, "_first_part": partition_prefix + "1"})
        return disks, table

    def status(self):
        output = {"available": True, "error": None, "mountpoint": str(self.target),
                  "directory": str(self.directory), "ready": False, "devices": []}
        try:
            disks, _ = self._snapshot()
            output["devices"] = [{k: v for k, v in d.items() if not k.startswith("_")} for d in disks]
            output["ready"] = any(v["ready"] for d in disks for v in d["volumes"])
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            output.update(available=False, error=str(exc))
        return output

    def _raw_users(self, nodes):
        numbers = {_devno(n["maj:min"]) for n in nodes}
        for process in self.proc.iterdir():
            if not process.name.isdecimal() or process.name == str(os.getpid()):
                continue
            try:
                for descriptor in (process / "fd").iterdir():
                    try:
                        info = descriptor.stat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISBLK(info.st_mode) and info.st_rdev in numbers:
                        raise ValueError("Another process has the block device open.")
            except (FileNotFoundError, ProcessLookupError):
                continue

    @contextlib.contextmanager
    def _pin(self, node, diskseq, exclusive_check=False):
        path = self.dev / node["name"]
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if exclusive_check:
            # The kernel also checks claims in other mount namespaces.
            claim = os.open(path, flags | os.O_EXCL)
            os.close(claim)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev != _devno(node["maj:min"]):
                raise ValueError("The selected block device node changed.")
            # BLKGETDISKSEQ returns the gendisk generation for either a whole disk or partition.
            observed = struct.unpack("=Q", fcntl.ioctl(fd, 0x80081280, bytes(8)))[0]
            if observed != int(diskseq):
                raise ValueError("The selected disk was replaced. Reload the drive list.")
            yield fd
        finally:
            os.close(fd)

    def _update_fstab(self, baseline, fsuuid):
        managed = self._managed_line(fsuuid)
        if self._fstab() != baseline:
            raise ValueError("fstab changed while the volume was being prepared.")
        if managed in baseline.splitlines():
            return
        lines = [line for line in baseline.splitlines(keepends=True)
                 if not any(point == str(self.target) and self._managed_entry(entry)
                            for _, point, entry in _fstab_rows(line))]
        text = "".join(lines)
        if text and not text.endswith("\n"):
            text += "\n"
        replacement = text + managed + "\n"
        mode = stat.S_IMODE(self.fstab.stat().st_mode)
        try:
            atomic_text(self.fstab, replacement, mode)
            _command(["systemctl", "daemon-reload"])
        except (OSError, RuntimeError):
            if self._fstab() == replacement:
                atomic_text(self.fstab, baseline, mode)
                with contextlib.suppress(RuntimeError):
                    _command(["systemctl", "daemon-reload"])
            raise

    def _disk(self, disks, path):
        return next((d for d in disks if d["device"] == path), None)

    def _partition(self, disk):
        disk_node = disk["_nodes"][0]
        part_id = str(uuid.uuid4())
        with self._pin(disk_node, disk["_seq"], exclusive_check=True) as fd:
            self._raw_users(disk["_nodes"])
            fresh = self._disk(self._snapshot()[0], disk["device"])
            if not fresh or fresh["revision"] != disk["revision"]:
                raise ValueError("The disk changed before partitioning. Reload the drive list.")
            _command(["sfdisk", "--lock=nonblock", "--wipe=always", "--wipe-partitions=always",
                      f"/proc/self/fd/{fd}"], data=f"label: gpt\n\ntype=L,uuid={part_id}\n", fds=(fd,))
        _command(["udevadm", "settle", "--timeout=15"])
        current = self._disk(self._snapshot()[0], disk["device"])
        if not current or current["_seq"] != disk["_seq"] or current["serial"] != disk["serial"] or not current["can_prepare"]:
            raise ValueError("The disk changed after partitioning. Reload the drive list.")
        nodes = current["_nodes"]
        if (len(nodes) != 2 or nodes[1]["name"] != disk["_first_part"]
                or (nodes[1].get("partuuid") or "").lower() != part_id):
            raise ValueError("The new partition could not be verified.")
        fsuuid = str(uuid.uuid4())
        with self._pin(nodes[1], disk["_seq"], exclusive_check=True) as fd:
            self._raw_users(nodes)
            latest = self._disk(self._snapshot()[0], disk["device"])
            if not latest or latest["revision"] != current["revision"]:
                raise ValueError("The disk changed before formatting. Reload the drive list.")
            _command(["mkfs.ext4", "-q", "-U", fsuuid, "-L", "RPI-VIDEO", "-m", "0",
                      "-E", "nodiscard", f"/proc/self/fd/{fd}"], fds=(fd,), timeout=180)
        # mkfs can finish after the kernel partition-add event has already been consumed.
        _command(["udevadm", "trigger", "--action=change", "--subsystem-match=block",
                  "--sysname-match=" + nodes[1]["name"]])
        _command(["udevadm", "settle", "--timeout=15"])
        return str(self.dev / nodes[1]["name"]), fsuuid

    def _mount(self, disk, volume, table):
        node = next(n for n in disk["_nodes"] if str(self.dev / n["name"]) == volume["device"])
        mounted_here = False
        try:
            if not volume["mounted"]:
                self.target.mkdir(mode=0o755, exist_ok=True)
                self._location()
                with self._pin(node, disk["_seq"], exclusive_check=True) as fd:
                    self._raw_users(disk["_nodes"])
                    src = f"/proc/self/fd/{fd}"
                    probe = _command(["blkid", "-p", "-o", "export", "-s", "UUID", "-s", "TYPE", src], fds=(fd,))
                    tags = dict(line.split("=", 1) for line in probe.splitlines() if "=" in line)
                    if tags.get("TYPE") != "ext4" or tags.get("UUID") != volume["uuid"]:
                        raise ValueError("The filesystem on the selected disk changed.")
                    _command(["e2fsck", "-n", src], fds=(fd,), timeout=180)
                    latest = self._disk(self._snapshot()[0], disk["device"])
                    if not latest or latest["revision"] != disk["revision"]:
                        raise ValueError("The disk changed before mounting. Reload the drive list.")
                    _command(["mount", "--no-canonicalize", "-t", "ext4", "-o", OPTIONS, src, str(self.target)],
                             fds=(fd,))
                mounted_here = True
            self._location()
            mounts = read_mounts(self.proc / "self/mountinfo")
            if not any(m.path == self.target and m.dev == node["maj:min"] and m.root == "/" and m.fs == "ext4"
                       and not m.read_only for m in mounts.values()):
                raise ValueError("The mounted recording volume identity could not be verified.")
            self._working_directory(node["maj:min"], create=True)
            self._update_fstab(table, volume["uuid"])
        except Exception:
            if mounted_here:
                mounts = read_mounts(self.proc / "self/mountinfo")
                here = [m for m in mounts.values() if m.path == self.target]
                if len(here) == 1 and here[0].dev == node["maj:min"]:
                    _command(["umount", str(self.target)])
            raise

    def apply(self, body):
        if not isinstance(body, dict) or body.get("action") not in ("mount", "prepare"):
            raise ValueError("Choose a supported disk operation.")
        prepare = body["action"] == "prepare"
        if (set(body) != {"action", "device", "revision", "confirmation" if prepare else "volume"}
                or not all(type(v) is str for v in body.values())):
            raise ValueError("Invalid disk operation fields.")
        if prepare and body["confirmation"] != body["device"]:
            raise ValueError("Enter the exact disk path to confirm erasing it.")
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another disk operation is running.") from exc
            disks, table = self._snapshot()
            disk = self._disk(disks, body["device"])
            if not disk or body["revision"] != disk["revision"]:
                raise ValueError("The disk or its configuration changed. Reload the drive list.")
            if prepare:
                if not disk["can_prepare"]:
                    raise ValueError(disk["reason"])
                volume_path, fsuuid = self._partition(disk)
                updated, current_table = self._snapshot()
                new_disk = self._disk(updated, disk["device"])
                if (not new_disk or disk["_seq"] != new_disk["_seq"] or disk["serial"] != new_disk["serial"]
                        or table != current_table):
                    raise ValueError("The disk configuration changed. Reload the drive list.")
                disk = new_disk
            else:
                volume_path, fsuuid = body["volume"], None
            volume = next((v for v in disk["volumes"] if v["device"] == volume_path), None)
            if not volume or fsuuid and fsuuid != volume["uuid"]:
                raise ValueError("The selected recording volume could not be verified.")
            if not volume["can_mount"]:
                raise ValueError(volume["reason"] or "This recording volume is already mounted for recording.")
            self._mount(disk, volume, table)
            result = self.status()
            if not any(v["device"] == volume_path and v["uuid"] == volume["uuid"] and v["ready"]
                       for d in result["devices"] if d["device"] == disk["device"] and d["serial"] == disk["serial"]
                       for v in d["volumes"]):
                raise RuntimeError(result["error"] or "The disk recording directory could not be verified.")
            return result
        finally:
            os.close(fd)
