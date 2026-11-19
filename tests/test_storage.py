import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rpi_web_control.recording import Recording
from rpi_web_control.storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.proc = self.base / "proc"
        self.sysfs = self.base / "sys"
        self.dev = self.base / "dev"
        self.drive = self.base / "camera usb"
        self.root = self.drive / "recordings"
        self.root.mkdir(parents=True)
        (self.proc / "self").mkdir(parents=True)
        (self.sysfs / "class/block").mkdir(parents=True)
        (self.sysfs / "dev/block").mkdir(parents=True)
        self.mount_file = self.proc / "self/mountinfo"
        escaped = str(self.drive).replace(" ", r"\040")
        self.system_line = "1 0 179:2 / / rw - ext4 /dev/root rw\n"
        self.usb_line = f"7 1 8:1 / {escaped} rw - ext4 /dev/sda1 rw\n"
        self.mount_file.write_text(self.system_line + self.usb_line)
        self.fstab = self.base / "fstab"
        self.fstab.write_text("")
        self.sd = self.block("platform/mmc_host/mmc0/mmc0:0001/block/mmcblk0")
        (self.sd / "device").mkdir()
        (self.sd / "device/type").write_text("SD")
        self.block("platform/mmc_host/mmc0/mmc0:0001/block/mmcblk0/mmcblk0p2", "179:2", True)
        self.usb = self.block("platform/usb1/1-1/host0/target0/block/sda")
        self.block("platform/usb1/1-1/host0/target0/block/sda/sda1", "8:1", True)
        uuid = self.dev / "disk/by-uuid"
        uuid.mkdir(parents=True)
        (uuid / "CARD").symlink_to("../../mmcblk0p2")
        (uuid / "USB-123").symlink_to("../../sda1")

    def block(self, location, number=None, partition=False):
        node = self.sysfs / "devices" / location
        node.mkdir(parents=True, exist_ok=True)
        (self.sysfs / "class/block" / node.name).symlink_to(node)
        if number:
            (self.sysfs / "dev/block" / number).symlink_to(node)
        if partition:
            (node / "partition").write_text("1")
        return node

    def store(self, **options):
        return Storage([self.root], proc=self.proc, sysfs=self.sysfs, dev=self.dev,
                       fstab=self.fstab, linux=True, **options)

    def test_managed_nvme_checks_the_actual_mount_before_using_any_files(self):
        disk = self.store(managed_root=self.root)
        # An existing directory on a different mounted drive is insufficient.
        with self.assertRaisesRegex(ValueError, "NVMe volume"):
            disk.identity(self.root)
        self.mount_file.write_text(self.system_line)
        with self.assertRaises(ValueError):
            with disk.open_target(self.root / "new"):
                self.fail("The missing NVMe volume must never redirect writes to the system card.")
        self.assertFalse((self.root / "new").exists())
        self.block("platform/pci/nvme/nvme0/nvme0n1")
        self.block("platform/pci/nvme/nvme0/nvme0n1/nvme0n1p1", "259:1", True)
        nvme_mount = self.usb_line.replace("8:1", "259:1").replace("sda1", "nvme0n1p1")
        self.mount_file.write_text(self.system_line + nvme_mount)
        self.assertEqual(disk.identity(self.root)["mountpoint"], str(self.drive))
        self.root.rmdir()
        self.root.symlink_to(self.base)
        with self.assertRaisesRegex(ValueError, "outside the volume"):
            self.store(managed_root=self.root).identity(self.root)

    def test_root_card_mounted_usb_and_unmounted_nvme_emmc(self):
        self.block("platform/pci/nvme/nvme0/nvme0n1")
        emmc = self.block("platform/mmc_host/mmc1/mmc1:0001/block/mmcblk1")
        (emmc / "device").mkdir()
        (emmc / "device/type").write_text("MMC")
        self.block("platform/mmc_host/mmc1/mmc1:0001/block/mmcblk1boot0")
        rows = {r["device"]: r for r in self.store().inventory()}
        self.assertTrue(rows["/dev/root"]["system"])
        self.assertEqual(rows["/dev/root"]["kind"], "microSD")
        self.assertEqual(rows["/dev/sda1"]["mountpoint"], str(self.drive))
        self.assertEqual(rows["/dev/sda1"]["kind"], "USB")
        self.assertEqual(rows["/dev/sda1"]["roots"], [str(self.root)])
        self.assertEqual(rows["/dev/nvme0n1"]["kind"], "NVMe")
        self.assertIsNone(rows["/dev/nvme0n1"]["mountpoint"])
        self.assertEqual(rows["/dev/mmcblk1"]["kind"], "eMMC")
        self.assertNotIn("/dev/mmcblk1boot0", rows)

    def test_fuse_mount_source_resolves_usb_transport(self):
        line = self.usb_line.replace("8:1", "0:43").replace("ext4 /dev/sda1", "fuseblk /dev/sda1")
        self.mount_file.write_text(self.system_line + line)
        disk = self.store()
        devices = [r for r in disk.inventory() if r["device"] == "/dev/sda1"]
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["kind"], "USB")
        self.assertEqual(devices[0]["filesystem"], "fuseblk")
        self.assertEqual(disk.identity(self.root)["volume"], "by-uuid/USB-123")

    def test_spi_mtd_and_mounted_ubi(self):
        mtd = self.sysfs / "devices/platform/spi_master/spi0/spi0.1/mtd/mtd3"
        mtd.mkdir(parents=True)
        (mtd / "size").write_text("134217728")
        (mtd / "name").write_text("capture-data")
        (self.sysfs / "class/mtd").mkdir()
        (self.sysfs / "class/mtd/mtd3").symlink_to(mtd)
        ubi = self.sysfs / "class/ubi/ubi0"
        ubi.mkdir(parents=True)
        (ubi / "mtd_num").write_text("3")
        line = self.usb_line.replace("8:1", "0:20").replace("ext4 /dev/sda1", "ubifs ubi0:data")
        self.mount_file.write_text(self.system_line + line)
        rows = {r["device"]: r for r in self.store().inventory()}
        self.assertTrue(rows["/dev/mtd3"]["raw"])
        self.assertEqual(rows["/dev/mtd3"]["kind"], "SPI flash")
        self.assertEqual(rows["/dev/mtd3"]["roots"], [])
        self.assertEqual(rows["ubi0:data"]["kind"], "SPI flash")
        self.assertEqual(rows["ubi0:data"]["roots"], [str(self.root)])

    def test_identity_detects_replacement_and_allows_uuid_remount(self):
        disk = self.store()
        saved = disk.identity(self.root / "future")
        self.assertEqual(saved["volume"], "by-uuid/USB-123")
        self.mount_file.write_text(self.system_line + self.usb_line.replace("7 1", "33 1"))
        self.assertEqual(disk.identity(self.root, expected=saved), saved)
        (self.dev / "disk/by-uuid/USB-123").rename(self.dev / "disk/by-uuid/OTHER")
        with self.assertRaisesRegex(ValueError, "changed"):
            disk.identity(self.root, expected=saved)
        self.mount_file.write_text(self.system_line)
        with self.assertRaisesRegex(ValueError, "changed"):
            disk.identity(self.root, expected=saved)

    def test_absent_required_mount_before_first_configuration(self):
        escaped = str(self.drive).replace(" ", r"\040")
        self.fstab.write_text(f"UUID=USB-123 {escaped} ext4 defaults,nofail 0 2\n")
        disk = self.store()
        self.mount_file.write_text(self.system_line)
        with self.assertRaisesRegex(ValueError, "required recording mount"):
            disk.identity(self.root / "new")
        absent = next(r for r in disk.inventory() if r["kind"] == "Unavailable mount")
        self.assertEqual(absent["label"], str(self.drive))
        self.assertFalse(any(r["roots"] for r in disk.inventory()))

    def test_rejects_read_only_mount_and_raw_device_destinations(self):
        disk = self.store()
        self.mount_file.write_text(self.system_line + self.usb_line.replace("rw -", "ro -"))
        with self.assertRaisesRegex(ValueError, "read-only"):
            disk.identity(self.root)
        with self.assertRaisesRegex(ValueError, "allowed storage root"):
            disk.identity("/dev/mtd3")

    def test_ext4_failure_flags_refuse_new_sessions_and_preserve_read_access(self):
        disk = self.store()
        saved = disk.identity(self.root)
        for flags in ("rw,errors=remount-ro", "ro", "rw,emergency_ro", "rw,shutdown",
                      "rw,emergency_ro,shutdown"):
            with self.subTest(flags=flags):
                self.mount_file.write_text(self.system_line + self.usb_line.replace("/dev/sda1 rw",
                                                                                    "/dev/sda1 " + flags))
                writable = flags == "rw,errors=remount-ro"
                self.assertEqual(disk.identity(self.root, saved, writable=False), saved)
                disk.invalidate_inventory()
                volume = next(row for row in disk.inventory() if row["device"] == "/dev/sda1")
                self.assertEqual(volume["read_only"], not writable)
                if writable:
                    self.assertEqual(disk.identity(self.root, saved), saved)
                else:
                    with self.assertRaisesRegex(ValueError, "read-only or shut down"):
                        with disk.open_target(self.root / "new", saved):
                            self.fail("An unhealthy volume accepted a recording target.")
                    self.assertFalse((self.root / "new").exists())

    def test_recording_files_remain_on_open_directory_after_path_replacement(self):
        disk = Storage([self.root], linux=False)
        with disk.open_target(self.root / "sub" / "day") as target:
            old_name = target.path
            moved = old_name.with_name("moved")
            old_name.rename(moved)
            old_name.mkdir()
            recording = Recording(target, {"fps": 30, "osd": False}, "simulated", "Camera", "manual")
        recording.close()
        self.assertEqual(list(old_name.iterdir()), [])
        data = json.loads((moved / recording.path.name / "session.json").read_text())
        self.assertEqual(data["status"], "incomplete")
        self.assertEqual(recording.path.parent, old_name)
        self.assertEqual(disk.inventory()[0]["kind"], "Local filesystem")

    def test_verify_fd_mount_id_before_creating_directory(self):
        # An unmount may expose a directory with the same name after the first lookup.
        disk = self.store()
        child = self.root / "must-not-be-created"
        real_open = os.open

        def open_dir(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            info = self.proc / ("self/fdinfo/" + str(fd))
            info.parent.mkdir(parents=True, exist_ok=True)
            info.write_text("mnt_id:\t1\n")
            return fd

        with patch("os.open", side_effect=open_dir), patch.object(disk, "_verify_location"):
            with self.assertRaisesRegex(ValueError, "changed"):
                with disk.open_target(child):
                    self.fail("Unexpected recording target")
        self.assertFalse(child.exists())
