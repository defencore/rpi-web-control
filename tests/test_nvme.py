"""Exercise privileged NVMe policy and transaction failures without block devices."""

import contextlib
import copy
import fcntl
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch

from rpi_web_control.nvme import NvmeManager, OPTIONS


UUID = "12345678-5678-4abc-829a-424242424242"
ROOT_LINE = "1 0 179:2 / / rw - ext4 /dev/mmcblk0p2 rw\n"
FIRMWARE_LINE = "3 1 179:1 / /boot/firmware rw - vfat /dev/mmcblk0p1 rw\n"
MOUNT_BASE = ROOT_LINE + FIRMWARE_LINE


class NvmeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.manager = m = NvmeManager(sysfs=base / "sys", proc=base / "proc", dev=base / "dev",
                                      target=base / "recording-mount", fstab=base / "fstab", lock=base / "lock")
        self.tree = [{"name": "nvme0n1", "type": "disk", "ro": False, "serial": "ABC",
                      "model": "PCIe SSD", "maj:min": "259:0", "size": 128000000000,
                      "children": [{"name": "nvme0n1p1", "type": "part", "ro": False,
                                    "maj:min": "259:1", "size": 16777216, "partuuid": UUID,
                                    "parttype": "e3c9e316-0b5c-4db8-817d-f92df00215ae"}]}]
        self.controller = m.sysfs / "devices/pci0000:00/nvme/nvme0"
        self.block = self.controller / "nvme0n1"
        self.part = self.block / "nvme0n1p1"
        for where in [m.proc / "self", m.dev, m.sysfs / "class/block", m.sysfs / "class/nvme",
                      self.block / "holders", self.part / "holders"]:
            where.mkdir(parents=True)
        (m.sysfs / "class/block/nvme0n1").symlink_to(self.block)
        (m.sysfs / "class/block/nvme0n1p1").symlink_to(self.part)
        (m.sysfs / "class/nvme/nvme0").symlink_to(self.controller)
        (self.controller / "transport").write_text("pcie")
        (self.block / "diskseq").write_text("7")
        (self.block / "dev").write_text("259:0")
        (self.part / "dev").write_text("259:1")
        self.table = "# admin settings\nUUID=system / ext4 defaults 0 1\n"
        m.fstab.write_text(self.table)
        self.mountinfo = m.proc / "self/mountinfo"
        self.mountinfo.write_text(MOUNT_BASE)
        (m.proc / "swaps").write_text("Filename Type Size Used Priority\n")
        (m.proc / "sys/kernel/random").mkdir(parents=True)
        (m.proc / "sys/kernel/random/boot_id").write_text(UUID)
        system_disk = m.sysfs / "devices/mmc_host/mmc0/mmcblk0"
        system_part = system_disk / "mmcblk0p2"
        firmware_part = system_disk / "mmcblk0p1"
        (m.sysfs / "dev/block").mkdir(parents=True)
        (system_disk / "slaves").mkdir(parents=True)
        system_part.mkdir()
        firmware_part.mkdir()
        (firmware_part / "dev").write_text("179:1")
        (firmware_part / "partition").write_text("1")
        (system_disk / "dev").write_text("179:0")
        (system_part / "dev").write_text("179:2")
        (system_part / "partition").write_text("2")
        (self.part / "partition").write_text("1")
        for devno, path in [("179:0", system_disk), ("179:1", firmware_part), ("179:2", system_part)]:
            (m.sysfs / "dev/block" / devno).symlink_to(path)
        self.system_tree = [{"maj:min": "179:0", "name": "mmcblk0", "type": "disk", "children": [
            {"maj:min": "179:1", "name": "mmcblk0p1", "type": "part"},
            {"maj:min": "179:2", "name": "mmcblk0p2", "type": "part"}]}]
        self.start_patch(patch.object(m, "_tree", side_effect=lambda: copy.deepcopy(self.tree + self.system_tree)))
        # The Darwin development account cannot manufacture root-owned fixtures.
        self.start_patch(patch.object(m, "_fstab", side_effect=m.fstab.read_text))
        self.loc = self.start_patch(patch.object(m, "_location"))
        self.node = self.tree[0]["children"][0]
        self.raw_device = str(m.dev / "nvme0n1")
        self.volume = str(m.dev / "nvme0n1p1")

    def start_patch(self, mock):
        self.addCleanup(mock.stop)
        return mock.start()

    def disk(self):
        data = self.manager.status()
        self.assertTrue(data["available"], data)
        return data["devices"][0]

    def request(self, action):
        key = "confirmation" if action == "prepare" else "volume"
        return {"action": action, "device": self.raw_device, "revision": self.disk()["revision"],
                key: self.raw_device if action == "prepare" else self.volume}

    def formatted(self):
        self.node.update(fstype="ext4", uuid=UUID, size=127990000000)

    def mount(self, dev="259:1", location=None, flags="rw,nosuid,nodev,noexec"):
        location = location or self.manager.target
        self.mountinfo.write_text(MOUNT_BASE + f"2 1 {dev} / {location} {flags} - ext4 {self.volume} rw\n")

    def test_unmounted_msr_drive_and_non_pcie_transport(self):
        disk = self.disk()
        self.assertTrue(disk["can_prepare"])
        self.assertFalse(disk["volumes"][0]["can_mount"])
        self.assertEqual(disk["size_bytes"], 128000000000)
        (self.controller / "transport").write_text("tcp")
        self.assertEqual(self.manager.status()["devices"], [])

    def test_exclude_system_swap_holders_stacks_and_in_use_elsewhere(self):
        for setup, restore in [
            (lambda: self.mount(location="/boot/firmware"), lambda: self.mountinfo.write_text(MOUNT_BASE)),
            (lambda: self.node.update(parttype="c12a7328-f81f-11d2-ba4b-00a0c93ec93b"),
             lambda: self.node.update(parttype="")),
            (lambda: self.node.update(fstype="swap"), lambda: self.node.pop("fstype")),
            (lambda: self.node.update(ro=True), lambda: self.node.update(ro=False)),
            (lambda: (self.part / "holders/dm-1").touch(), lambda: (self.part / "holders/dm-1").unlink()),
            (lambda: self.node.update(children=[{"type": "lvm", "name": "system", "maj:min": "9:8"}]),
             lambda: self.node.pop("children")),
            (lambda: self.manager.fstab.write_text(f"PARTUUID={UUID} /data auto defaults 0 0\n"),
             lambda: self.manager.fstab.write_text(self.table)),
            (lambda: self.mount(dev="8:1"), lambda: self.mountinfo.write_text(MOUNT_BASE)),
        ]:
            setup()
            try:
                self.assertFalse(self.disk()["can_prepare"])
                with self.assertRaises(ValueError):
                    self.manager.apply(self.request("prepare"))
            finally:
                restore()

    def test_mountpoint_conflicts_and_uuid_collisions(self):
        self.formatted()
        self.tree.append({"type": "part", "name": "sdb1", "maj:min": "8:1", "uuid": UUID})
        self.assertFalse(self.disk()["volumes"][0]["can_mount"])
        self.tree.pop()
        self.assertTrue(self.disk()["volumes"][0]["can_mount"])
        self.mount(flags="rw")
        self.assertFalse(self.disk()["volumes"][0]["can_mount"])
        self.mountinfo.write_text(MOUNT_BASE)
        self.manager.fstab.write_text(f"UUID={UUID} {self.manager.target} ext4 defaults 0 0\n")
        with self.assertRaises(ValueError):
            self.manager.apply(self.request("mount"))
        self.manager.fstab.write_text(self.table)
        self.manager.target.mkdir()
        (self.manager.target / "underlying-file").touch()
        self.assertFalse(self.disk()["can_prepare"])

    def usb(self, serial=""):
        m = self.manager
        m.medium = "usb"
        self.tree[0].update(name="sdb", tran="usb", serial=serial, model="USB thumb drive")
        self.node.update(name="sdb1")
        usb = m.sysfs / "bus/usb"
        (usb / "drivers/usb-storage").mkdir(parents=True)
        interface = m.sysfs / "devices/pci0/usb4/4-1/4-1:1.0"
        self.block = interface / "host4/target4:0:0/4:0:0:0/block/sdb"
        self.part = self.block / "sdb1"
        (self.block / "holders").mkdir(parents=True)
        (self.block / "slaves").mkdir()
        (self.part / "holders").mkdir(parents=True)
        (self.part / "partition").write_text("1")
        (self.block / "diskseq").write_text("827")
        (interface / "subsystem").symlink_to(usb)
        (interface / "driver").symlink_to(usb / "drivers/usb-storage")
        for path, devno in ((self.block, "259:0"), (self.part, "259:1")):
            (m.sysfs / "class/block" / path.name).symlink_to(path)
            (m.sysfs / "dev/block" / devno).symlink_to(path)
            (path / "dev").write_text(devno)
        self.raw_device, self.volume = str(m.dev / "sdb"), str(m.dev / "sdb1")
        return interface

    def test_usb_serialless_mass_storage_can_initialize_in_one_managed_slot(self):
        self.usb()
        self.assertEqual(NvmeManager(medium="usb").target, Path("/srv/rpi-web-control-usb"))
        self.assertEqual(NvmeManager().target, Path("/srv/rpi-web-control-nvme"))
        request = self.request("prepare")
        calls = self.simulate_commands()
        result = self.manager.apply(request)
        self.assertTrue(result["ready"])
        self.assertEqual(result["devices"][0]["volumes"][0]["devno"], "259:1")
        self.assertIn("# rpi-web-control USB", self.manager.fstab.read_text())
        self.assertEqual(calls.count("sfdisk"), 1)
        self.assertEqual(calls.count("mkfs.ext4"), 1)
        self.assertFalse(self.disk()["can_prepare"])
        with self.assertRaises(ValueError):
            self.manager.apply(self.request("prepare"))
        self.assertEqual(calls.count("sfdisk"), 1)

    def test_usb_existing_volume_and_verified_usb_transport(self):
        interface = self.usb()
        self.formatted()
        calls = self.simulate_commands()
        self.tree[0]["tran"] = "sata"
        self.assertEqual(self.manager.status()["devices"], [])
        self.tree[0]["tran"] = "usb"
        (interface / "driver").unlink()
        self.assertEqual(self.manager.status()["devices"], [])
        (self.manager.sysfs / "bus/usb/drivers/uas").mkdir()
        (interface / "driver").symlink_to(self.manager.sysfs / "bus/usb/drivers/uas")
        self.assertTrue(self.manager.apply(self.request("mount"))["ready"])
        self.assertEqual(calls, ["blkid", "e2fsck", "mount", "systemctl"])

    def test_usb_system_disk_blocks_every_sibling_and_late_switch(self):
        self.usb()
        self.formatted()
        sibling = self.block / "sdb2"
        sibling.mkdir()
        (sibling / "dev").write_text("259:2")
        (sibling / "partition").write_text("2")
        (sibling / "holders").mkdir()
        for name in ("class/block/sdb2", "dev/block/259:2"):
            (self.manager.sysfs / name).symlink_to(sibling)
        self.tree[0]["children"].append({"maj:min": "259:2", "type": "part", "name": "sdb2",
                                       "size": 10**8, "ro": False})
        erase, mount = self.request("prepare"), self.request("mount")
        for mount_table in ["1 0 259:2 / / rw - ext4 /dev/root rw\n" + FIRMWARE_LINE,
                            ROOT_LINE + "5 1 259:2 / /boot/firmware rw - vfat /dev/sdb2 rw\n"]:
            self.mountinfo.write_text(mount_table)
            disk = self.disk()
            self.assertIn("system", disk["reason"])
            self.assertFalse(disk["can_prepare"])
            self.assertFalse(any(v["can_mount"] or v["ready"] for v in disk["volumes"]))
            with patch.object(self.manager, "_partition", side_effect=AssertionError), \
                    patch.object(self.manager, "_mount", side_effect=AssertionError):
                for body in (erase, mount, self.request("prepare"), self.request("mount")):
                    with self.assertRaises(ValueError):
                        self.manager.apply(body)

    def test_preparation_is_bound_to_boot_and_bus(self):
        self.usb()
        request = self.request("prepare")
        with patch.object(self.manager, "_partition", side_effect=AssertionError):
            (self.manager.proc / "sys/kernel/random/boot_id").write_text("10000000-5678-4abc-829a-424242424242")
            with self.assertRaisesRegex(ValueError, "changed"):
                self.manager.apply(request)
            # A client cannot select an internal disk using the USB route.
            with self.assertRaisesRegex(ValueError, "changed"):
                self.manager.apply({**request, "device": "/dev/nvme0n1", "confirmation": "/dev/nvme0n1"})
            self.manager.medium = "nvme"
            self.assertEqual(self.manager.status()["devices"], [])
            with self.assertRaisesRegex(ValueError, "changed"):
                self.manager.apply(request)

    def test_usb_boot_and_reserved_partitions_remain_protected_while_unmounted(self):
        self.usb()
        self.formatted()
        for table, fields in (("dos", {"partflags": "0x80"}), ("gpt", {"partflags": "0x4"}),
                              ("gpt", {"partflags": "0x1"}), ("gpt", {"partlabel": "rootfs"})):
            with self.subTest(table=table, fields=fields):
                self.tree[0]["pttype"] = table
                self.node.update(fields)
                self.assertFalse(self.disk()["can_prepare"])
                self.assertFalse(self.disk()["volumes"][0]["can_mount"])
                for key in fields:
                    del self.node[key]

    def test_unmounted_unlabelled_firmware_partition_blocks_every_operation(self):
        self.usb()
        self.node.update(fstype="vfat", parttype="0x0c", label=None)
        approved = self.request("prepare")
        self.mountinfo.write_text(ROOT_LINE)
        disk = self.disk()
        self.assertIn("mount its separate FAT volume at /boot/firmware", disk["reason"])
        self.assertFalse(disk["can_prepare"])
        self.assertFalse(any(v["ready"] or v["can_mount"] for v in disk["volumes"]))
        with patch.object(self.manager, "_partition", side_effect=AssertionError):
            for body in (approved, self.request("prepare")):
                with self.assertRaises(ValueError):
                    self.manager.apply(body)
        for unrelated in ("3 1 179:2 /boot/firmware /boot/firmware rw - ext4 /dev/mmcblk0p2 rw\n",
                          "3 1 179:1 /subdir /boot/firmware rw - vfat /dev/mmcblk0p1 rw\n"):
            self.mountinfo.write_text(ROOT_LINE + unrelated)
            self.assertFalse(self.disk()["can_prepare"])
        self.mountinfo.write_text(ROOT_LINE + "3 1 259:1 / /boot/firmware rw - vfat /dev/sdb1 rw\n")
        self.assertIn("system or a boot filesystem", self.disk()["reason"])

    def test_layered_system_root_and_unknown_storage_fail_closed(self):
        self.usb()
        self.formatted()
        dm = self.manager.sysfs / "devices/virtual/block/dm-0"
        (dm / "slaves").mkdir(parents=True)
        (dm / "slaves/sdb1").symlink_to(self.part)
        (dm / "dev").write_text("253:0")
        (self.manager.sysfs / "dev/block/253:0").symlink_to(dm)
        self.system_tree.append({"maj:min": "253:0", "type": "crypt", "name": "rootcrypt"})
        self.mountinfo.write_text("1 0 253:0 / / rw - ext4 /dev/mapper/rootcrypt rw\n" + FIRMWARE_LINE)
        self.assertIn("contains the running system", self.disk()["reason"])
        (dm / "slaves/sdb1").unlink()
        for mount_table in ["1 0 253:0 / / rw - ext4 /dev/mapper/rootcrypt rw\n",
                            "1 0 0:13 / / rw - overlay overlay rw\n",
                            "1 0 0:15 / / rw - btrfs /dev/unknown rw\n",
                            "1 0 179:2 / /srv rw - ext4 /dev/mmcblk0p2 rw\n"]:
            self.mountinfo.write_text(mount_table + FIRMWARE_LINE)
            disk = self.disk()
            self.assertIn("Cannot verify", disk["reason"])
            self.assertFalse(disk["can_prepare"])
            self.assertFalse(disk["volumes"][0]["can_mount"])

    def test_usb_identity_changes_before_mkfs_and_unlisted_partition_block_erase(self):
        self.usb()
        request = self.request("prepare")
        part = self.block / "sdb9"
        part.mkdir()
        (part / "partition").write_text("9")
        self.assertFalse(self.disk()["can_prepare"])
        with self.assertRaises(ValueError):
            self.manager.apply(request)
        (part / "partition").unlink()
        part.rmdir()
        calls = self.simulate_commands()
        def swap_after_partition(nodes):
            if "sfdisk" in calls:
                (self.block / "diskseq").write_text("829")
        with patch.object(self.manager, "_raw_users", side_effect=swap_after_partition):
            with self.assertRaisesRegex(ValueError, "changed before formatting"):
                self.manager.apply(request)
        self.assertNotIn("mkfs.ext4", calls)
        self.assertNotIn("mount", calls)

    def test_only_explicit_reviewed_request_can_erase(self):
        body = self.request("prepare")
        self.start_patch(patch.object(self.manager, "_partition", side_effect=AssertionError))
        for changes in [{"confirmation": "YES"}, {"extra": True}, {"device": "/dev/nvme0"},
                        {"revision": "stale"}, {"confirmation": []}]:
            with self.assertRaises(ValueError):
                self.manager.apply({**body, **changes})
        (self.block / "diskseq").write_text("8")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.apply(body)

    def test_concurrent_operations_are_refused(self):
        with self.manager.lock.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, "running"):
                self.manager.apply(self.request("prepare"))

    def simulate_commands(self, fail=None):
        calls = []

        def run(argv, data=None, **kwargs):
            calls.append(argv[0])
            if argv[0] == fail:
                raise RuntimeError("Expected command failure")
            if argv[0] == "sfdisk":
                self.node.update(fstype=None, uuid=None, size=127990000000,
                                 partuuid=re.search(r"uuid=([^\s]+)", data)[1])
            if argv[0] == "mkfs.ext4":
                self.node.update(fstype="ext4", uuid=argv[argv.index("-U") + 1])
            if argv[0] == "blkid":
                return "TYPE=ext4\nUUID=" + self.node["uuid"] + "\n"
            if argv[0] == "mount":
                self.assertIn(OPTIONS, argv)
                self.mount()
            if argv[0] == "umount":
                self.mountinfo.write_text(MOUNT_BASE)
            return ""

        self.start_patch(patch("rpi_web_control.nvme._command", side_effect=run))
        self.start_patch(patch.object(self.manager, "_pin", side_effect=lambda *a, **kw: contextlib.nullcontext(49)))
        self.start_patch(patch.object(self.manager, "_working_directory"))
        return calls

    def test_prepare_once_mount_by_uuid_and_no_duplicate_destructive_retry(self):
        body = self.request("prepare")
        calls = self.simulate_commands()
        result = self.manager.apply(body)
        self.assertTrue(result["ready"])
        self.assertTrue(result["devices"][0]["volumes"][0]["ready"])
        self.assertFalse(result["devices"][0]["can_prepare"])
        self.assertTrue(self.manager.fstab.read_text().startswith(self.table))
        self.assertIn(self.manager._managed_line(self.node["uuid"]), self.manager.fstab.read_text())
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.apply(body)
        self.assertEqual(calls.count("mkfs.ext4"), 1)

    def test_mount_existing_ext4_without_partition_or_mkfs(self):
        self.formatted()
        calls = self.simulate_commands()
        self.assertTrue(self.manager.apply(self.request("mount"))["ready"])
        self.assertEqual(calls, ["blkid", "e2fsck", "mount", "systemctl"])

    def test_failure_rolls_back_mount_and_fstab_with_existing_data_preserved(self):
        self.formatted()
        calls = self.simulate_commands(fail="systemctl")
        with self.assertRaisesRegex(RuntimeError, "Expected"):
            self.manager.apply(self.request("mount"))
        self.assertEqual(calls[-1], "umount")
        self.assertEqual(self.manager.fstab.read_text(), self.table)
        self.assertTrue(self.disk()["volumes"][0]["can_mount"])

    def test_dirty_filesystem_does_not_mount(self):
        self.formatted()
        calls = self.simulate_commands(fail="e2fsck")
        with self.assertRaisesRegex(RuntimeError, "Expected"):
            self.manager.apply(self.request("mount"))
        self.assertEqual(calls, ["blkid", "e2fsck"])
        self.assertEqual(self.manager.fstab.read_text(), self.table)

    def test_symlink_recording_directory_cannot_change_external_permissions(self):
        m = self.manager
        m.target.mkdir()
        outside = m.target.parent / "outside"
        outside.mkdir()
        (m.target / "recordings").symlink_to(outside)
        info = m.target.stat()
        number = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
        with patch.object(m, "_account", return_value=(os.getuid(), os.getgid())), self.assertRaises(OSError):
            m._working_directory(number, create=True)
        self.assertEqual(list(outside.iterdir()), [])
        (m.target / "recordings").unlink()
        m.target.chmod(0o700)
        with patch.object(m, "_account", return_value=(os.getuid() + 1, os.getgid() + 1)):
            with self.assertRaisesRegex(ValueError, "traverse"):
                m._working_directory(number, create=True)
        self.assertFalse(m.directory.exists())

    def test_raw_user_blocks_erase_and_reread_stops_stale_identity_before_writing(self):
        process = self.manager.proc / "98765/fd"
        process.mkdir(parents=True)
        descriptor = process / "3"
        descriptor.touch()
        original = Path.stat
        def stat_path(path, *args, **kwargs):
            if path == descriptor:
                return os.stat_result((stat.S_IFBLK, 0, 0, 1, 0, 0, 0, 0, 0, 0),
                                      {"st_rdev": os.makedev(259, 1)})
            return original(path, *args, **kwargs)
        with patch.object(Path, "stat", stat_path), self.assertRaisesRegex(ValueError, "block device open"):
            self.manager._raw_users([self.node])
        calls = self.simulate_commands()
        body = self.request("prepare")
        with patch.object(self.manager, "_raw_users", side_effect=lambda nodes: (self.block / "diskseq").write_text("8")):
            with self.assertRaisesRegex(ValueError, "changed before partitioning"):
                self.manager.apply(body)
        self.assertEqual(calls, [])

    def test_verify_disk_generation_from_opened_block_device(self):
        fd = os.open(os.devnull, os.O_RDONLY)
        try:
            with patch("os.open", return_value=fd), patch("os.close"), patch("os.fstat") as fstat, \
                    patch("fcntl.ioctl", return_value=struct.pack("=Q", 99)):
                fstat.return_value.st_mode = stat.S_IFBLK
                fstat.return_value.st_rdev = os.makedev(259, 1)
                with self.assertRaisesRegex(ValueError, "replaced"), self.manager._pin(self.node, "7"):
                    self.fail("An FD belonging to the replacement disk was accepted.")
        finally:
            os.close(fd)
