import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rpi_web_control.nvme_sim import DEVICE, PARTITION, SimulatedNvme


class SimulatedNvmeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name).resolve() / "state"
        self.nvme = SimulatedNvme(self.state)
        self.initial = self.nvme.status()["devices"][0]
        self.prepare = {"action": "prepare", "device": DEVICE, "revision": self.initial["revision"],
                        "confirmation": DEVICE}
        self.mount = {"action": "mount", "device": DEVICE, "revision": self.initial["revision"],
                      "volume": PARTITION}

    def test_mount_retains_existing_files_and_persists(self):
        self.assertFalse(self.nvme.ready)
        self.assertFalse(self.nvme.directory.exists())
        existing = self.state / "nvme-sim" / "existing" / "recordings"
        (existing / "previous-clip").write_bytes(b"previous")
        result = self.nvme.apply(self.mount)
        self.assertTrue(result["ready"])
        self.assertEqual(result["devices"][0]["volumes"][0]["uuid"], self.initial["volumes"][0]["uuid"])
        self.assertEqual((self.nvme.directory / "previous-clip").read_bytes(), b"previous")
        other = SimulatedNvme(self.state)
        self.assertEqual(result, other.status())
        self.assertFalse(result["devices"][0]["can_prepare"])

    def test_preparation_produces_new_empty_volume_and_rejects_replay(self):
        existing = self.state / "nvme-sim" / "existing" / "recordings"
        (existing / "old-data").write_bytes(b"original")
        status = self.nvme.apply(self.prepare)
        self.assertTrue(status["simulated"])
        self.assertTrue(status["ready"])
        self.assertNotEqual(status["devices"][0]["volumes"][0]["uuid"], self.initial["volumes"][0]["uuid"])
        self.assertEqual(list(self.nvme.directory.iterdir()), [])
        self.assertEqual((existing / "old-data").read_bytes(), b"original")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.nvme.apply(self.prepare)
        with self.assertRaisesRegex(ValueError, "already mounted"):
            self.nvme.apply({**self.prepare, "revision": status["devices"][0]["revision"]})

    def test_usb_and_nvme_operate_independently(self):
        usb = SimulatedNvme(self.state, medium="usb")
        disk, = usb.status()["devices"]
        self.assertEqual((disk["device"], disk["volumes"][0]["device"]), ("/dev/sda", "/dev/sda1"))
        self.assertNotEqual(disk["revision"], self.initial["revision"])
        usb_mount = {"action": "mount", "device": "/dev/sda", "volume": "/dev/sda1", "revision": disk["revision"]}
        for bad in [self.mount, {**usb_mount, "volume": PARTITION},
                    {**usb_mount, "revision": self.initial["revision"]}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                usb.apply(bad)
        existing = self.state / "usb-sim" / "existing" / "recordings" / "old-clip"
        existing.write_bytes(b"from usb")
        mounted = usb.apply(usb_mount)
        self.assertTrue(mounted["ready"])
        self.assertEqual(usb.directory, self.state / "usb-sim" / "volume" / "recordings")
        self.assertFalse(self.nvme.ready)
        self.nvme.apply(self.prepare)
        self.assertEqual(list(self.nvme.directory.iterdir()), [])
        self.assertEqual((usb.directory / existing.name).read_bytes(), b"from usb")
        self.assertEqual(SimulatedNvme(self.state, medium="usb").status(), mounted)

    def test_usb_erase_requires_its_own_device_and_revision(self):
        usb = SimulatedNvme(self.state, medium="usb")
        disk, = usb.status()["devices"]
        request = {"action": "prepare", "device": disk["device"], "revision": disk["revision"],
                   "confirmation": disk["device"]}
        for bad in [self.prepare, {**request, "confirmation": DEVICE}, {**request, "revision": self.initial["revision"]}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                usb.apply(bad)
        prepared = usb.apply(request)
        self.assertTrue(prepared["ready"])
        self.assertNotEqual(prepared["devices"][0]["volumes"][0]["uuid"], disk["volumes"][0]["uuid"])
        self.assertEqual(self.nvme.status()["devices"][0], self.initial)
        with self.assertRaisesRegex(ValueError, "changed"):
            usb.apply(request)

    def test_rejects_foreign_device_volume_unconfirmed_erase_and_extra_fields(self):
        for body in [None, {**self.prepare, "confirmation": "yes"}, {**self.prepare, "revision": "outdated"},
                     {**self.mount, "volume": "/dev/sda1"}, {**self.prepare, "device": "/dev/sda"},
                     {**self.mount, "mountpoint": "/tmp"}, {"action": "unmount"}]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.nvme.apply(body)
        self.assertEqual(self.nvme.status()["devices"][0], self.initial)
        self.assertFalse(self.nvme.directory.exists())

    def test_concurrent_plans_have_one_winner(self):
        other = SimulatedNvme(self.state)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.nvme.apply, self.mount)
            second = pool.submit(other.apply, self.prepare)
            futures = (first, second)
            self.assertEqual(sum(future.exception() is None for future in futures), 1)
            failed = next(future.exception() for future in futures if future.exception())
            self.assertIsInstance(failed, ValueError)

    def test_symlinked_backing_volume_cannot_write_outside_state(self):
        outside = self.state.parent / "outside"
        outside.mkdir()
        self.nvme.mountpoint.symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.nvme.status()["available"])
        with self.assertRaises(OSError):
            self.nvme.apply(self.prepare)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
