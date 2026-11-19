import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from rpi_web_control import sensor_profiles
from rpi_web_control.firmware import BACKUP, BEGIN, END, Firmware, FirmwareConflict, atomic_text, verify_target


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        self.firmware = Firmware(self.directory)
        self.boot = self.directory / "boot"
        self.config = self.boot / "config.txt"

    def apply(self, name, port="cam1", revision=None):
        return self.firmware.apply({"profile": name, "port": port,
                                    "revision": revision or self.firmware.status()["revision"]})

    def test_choose_between_manual_cam0_profiles_then_restore_both(self):
        imx577 = "dtoverlay=imx577,cam0,4lane,link-frequency=750000000"
        ov9281 = "dtoverlay=ov9281,cam0"
        original = f"# profile OV9281\n{ov9281}\n\n# profile IMX577\n{imx577}\n".encode()
        self.config.write_bytes(original)
        for name, overlay in (("imx577", imx577), ("ov9281", ov9281)):
            with self.subTest(name=name):
                status = self.firmware.status()
                self.assertEqual(status["conflicts"], [])
                self.assertEqual(status["selection_required_port"], "cam0")
                self.assertIsNone(status["profile"])
                preset = next(p for p in status["profiles"] if p["id"] == name)
                self.assertTrue(preset["installed"])
                self.assertEqual(preset["directives"]["cam0"], "camera_auto_detect=0\n" + overlay)
                self.assertEqual(preset["directives"]["cam1"], "camera_auto_detect=0\n" + overlay.replace(",cam0", ""))
                staged = self.apply(name, "cam0", status["revision"])
                self.assertIsNone(staged["selection_required_port"])
                self.assertEqual(staged["profile"], {"id": name, "port": "cam0"})
                self.assertEqual([line for line in staged["config_text"].splitlines() if line.startswith("dtoverlay=")],
                                 [overlay])
                self.assertIn("# profile OV9281\n\n# profile IMX577\n", staged["config_text"])
                self.assertEqual((self.boot / BACKUP).read_bytes(), original)
                self.apply("restore")
                self.assertEqual(self.config.read_bytes(), original)

    def test_ai_camera_requires_pi5_overlay_and_map(self):
        directory = self.boot / "overlays"
        (directory / "imx500.dtbo").touch()
        for filename in ("imx500-pi5.dtbo", "overlay_map.dtb"):
            with self.subTest(missing=filename):
                (directory / filename).unlink()
                status = self.firmware.status()
                preset = next(p for p in status["profiles"] if p["id"] == "imx500")
                self.assertFalse(preset["installed"])
                with self.assertRaisesRegex(ValueError, "missing"):
                    self.apply("imx500", "cam0", status["revision"])
                (directory / filename).touch()
        self.assertIn("dtoverlay=imx500,cam0\n", self.apply("imx500", "cam0")["config_text"])

    def test_arducam_variants_stage_the_overlay_for_the_selected_module(self):
        presets = {"imx519": "imx519", "imx519-fixed": "imx519,vcm=off",
                   "arducam-64mp": "arducam-64mp", "ov64a40-360mhz": "ov64a40,link-frequency=360000000",
                   "ov64a40-456mhz": "ov64a40,link-frequency=456000000", "arducam-pivariety": "arducam-pivariety"}
        for profile, expected in presets.items():
            with self.subTest(profile=profile):
                staged = self.apply(profile, "cam0")
                self.assertIn("dtoverlay=" + expected + ",cam0\n", staged["config_text"])
                self.assertEqual(self.firmware.status()["profile"], {"id": profile, "port": "cam0"})

    def test_same_port_requires_only_known_unconditional_manual_definitions(self):
        base = "camera_auto_detect=1\ndtoverlay=ov9281,cam0\ndtoverlay=imx577,cam0,4lane,link-frequency=750000000\n"
        self.config.write_text(base)
        self.assertIsNone(self.firmware.status()["profile"])
        for extra in ("dtoverlay=imx708\n", "dtoverlay=imx999,cam0\n", "[cm5]\ndtoverlay=imx219,cam0\n",
                      f"{BEGIN}\ncamera_auto_detect=1\n{END}\n"):
            with self.subTest(extra=extra):
                self.config.write_text(base + extra)
                status = self.firmware.status()
                self.assertTrue(status["conflicts"])
                self.assertIsNone(status["selection_required_port"])
                with self.assertRaises(FirmwareConflict):
                    self.apply("imx577", "cam0")

    def test_imx577_catalog_upgrade_preserves_operator_ids_and_meanings(self):
        catalog = self.directory / "sensor-profiles.json"
        existing = {"id": "imx577", "label": "Vendor IMX577", "overlay": "imx577", "params": ["clock=24"]}
        fallback = "imx577-4lane-750mhz"
        taken = {"id": fallback, "label": "Vendor board", "overlay": "vendor-board"}
        alias = {"id": "csi-fast", "label": "Vendor four-lane sensor", "overlay": "imx577",
                 "params": ["link-frequency=750000000", "4lane"]}
        original = "dtoverlay=imx577,clock=24,cam0 # preserve this note\r\n"
        for entries, expected in (([existing], fallback), ([existing, taken], fallback + "-2"),
                                  ([alias], "csi-fast"), ([existing, alias], "csi-fast")):
            with self.subTest(entries=entries):
                self.config.write_bytes(original.encode() if existing in entries else b"[all]\n")
                catalog.write_text(json.dumps(entries))
                status = self.firmware.status()
                self.assertFalse(status["conflicts"])
                profiles = {p["id"]: p for p in status["profiles"]}
                standard = [p["id"] for p in profiles.values() if "link-frequency=750000000" in p["directives"]["cam0"]]
                self.assertEqual(standard, [expected])
                for entry in entries:
                    self.assertEqual(profiles[entry["id"]]["label"], entry["label"])
                if existing in entries:
                    self.assertEqual(status["profile"], {"id": "imx577", "port": "cam0"})
                    self.assertIn("imx577,clock=24,cam0", profiles["imx577"]["directives"]["cam0"])
                staged = self.apply(expected, "cam0")
                self.assertIn("dtoverlay=imx577,cam0,4lane,link-frequency=750000000", staged["config_text"])
                self.assertEqual(staged["profile"], {"id": expected, "port": "cam0"})
                if existing in entries:
                    self.assertIn("# preserve this note\r\n", staged["config_text"])
                    self.apply("restore")
                    self.assertEqual(self.config.read_bytes(), original.encode())

    def test_new_presets_preserve_vendor_definitions_and_existing_fallbacks(self):
        vendor = [
            {"id": "imx577", "label": "Existing IMX577", "overlay": "imx577", "params": ["4lane"]},
            {"id": "site-camera", "label": "Site camera", "overlay": "site-overlay", "params": ["flip", "lanes=2"]},
            {"id": "later-camera", "label": "Existing ID", "overlay": "other-camera"},
            {"id": "later-camera-builtin", "label": "Existing fallback ID", "overlay": "custom-camera"},
        ]
        (self.directory / "sensor-profiles.json").write_text(json.dumps(vendor))
        fallback = "imx577-4lane-750mhz"
        self.apply(fallback, "cam0")
        additions = {fallback: ("New sensor", "another-overlay"),
                     "later-camera": ("Later sensor", "later-overlay"),
                     "site-preset": ("Equivalent upstream profile", "site-overlay,lanes=2,flip")}
        with patch.dict(sensor_profiles.ADDITIONAL_PROFILES, additions):
            status = self.firmware.status()
            self.assertEqual(status["conflicts"], [])
            self.assertEqual(status["profile"], {"id": fallback, "port": "cam0"})
            profiles = {p["id"]: p for p in status["profiles"]}
            vendor_ids = [row["id"] for row in vendor]
            self.assertEqual([uid for uid in profiles if uid in vendor_ids], vendor_ids)
            self.assertNotIn("site-preset", profiles)
            self.assertEqual(profiles["later-camera"]["label"], "Existing ID")
            self.assertEqual(profiles["later-camera-builtin-2"]["label"], "Later sensor")
            self.assertEqual(profiles[fallback + "-builtin"]["label"], "New sensor")

            original = "dtoverlay=site-overlay,cam0,lanes=2,flip\n"
            self.config.write_text(original)
            self.assertEqual(self.firmware.status()["profile"], {"id": "site-camera", "port": "cam0"})
            (self.boot / "overlays/site-overlay.dtbo").touch()
            result = self.apply("site-camera", "cam0")
            self.assertIn("dtoverlay=site-overlay,flip,lanes=2,cam0\n", result["config_text"])
            self.assertEqual(self.apply("restore")["config_text"], original)

    def test_reserved_ids_and_ambiguous_vendor_definitions_still_rejected(self):
        row = {"id": "site-camera", "overlay": "test-overlay", "label": "Site camera", "params": ["a=1", "b=2"]}
        for entries in ([dict(row, id="imx219")], [dict(row, overlay="imx219", params=[])],
                        [row, dict(row, id="alias", params=["b=2", "a=1"])],
                        [row, dict(row, overlay="other")]):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                sensor_profiles.parse_profiles(json.dumps(entries))

    def test_switch_profile_restore_and_reboot(self):
        original = b"# Front camera\r\ncamera_auto_detect=0\r\ndtoverlay=imx219,cam0\r\n[cm5]\r\ndtparam=pciex1"
        self.config.write_bytes(original)
        status = self.firmware.status()
        self.assertEqual(status["profile"], {"id": "imx219", "port": "cam0"})
        self.assertFalse(status["managed"])
        staged = self.apply("imx708", "cam1")
        self.assertIn("dtparam=pciex1\r\n[all]\r\n", staged["config_text"])
        self.assertIn("dtoverlay=imx708\r\n", staged["config_text"])
        self.assertNotIn("dtoverlay=imx219", staged["config_text"])
        self.assertTrue(staged["pending_reboot"])
        self.assertTrue(staged["managed"])
        self.assertEqual((self.boot / BACKUP).read_bytes(), original)
        self.assertEqual(self.apply("imx708", "cam1"), staged)
        wrong = dict(confirm=False, revision=staged["revision"])
        with self.assertRaisesRegex(ValueError, "confirmation"):
            self.firmware.reboot(wrong)
        with self.assertRaises(FirmwareConflict):
            self.apply("imx477", revision=status["revision"])
        reboot = self.firmware.reboot({"confirm": True, "revision": staged["revision"]})
        self.assertEqual(reboot, {"scheduled": True, "simulated": True})
        self.assertFalse(self.firmware.status()["pending_reboot"])
        restored = self.apply("restore")
        self.assertTrue(restored["pending_reboot"])
        self.assertFalse(restored["backup_available"])
        self.assertEqual(self.config.read_bytes(), original)

    def test_display_respects_external_scalar_and_guards_managed_override(self):
        (self.boot / "camera.txt").write_text("camera_auto_detect=0\n")
        self.config.write_text("camera_auto_detect=1\ninclude camera.txt\n")
        self.assertIsNone(self.firmware.status()["profile"])
        result = self.apply("imx219")
        self.assertIn("include camera.txt\n[all]\n" + BEGIN, result["config_text"])
        with self.config.open("a") as stream:
            stream.write("camera_auto_detect=1\n")
        self.assertTrue(self.firmware.status()["conflicts"])

    def test_undo_before_boot_clears_pending(self):
        current = self.firmware.status()
        result = self.apply("imx477", "cam0")
        self.assertIn("dtoverlay=imx477,cam0", result["config_text"])
        restored = self.apply("restore")
        self.assertFalse(restored["pending_reboot"])
        self.assertEqual(current["config_text"], restored["config_text"])

    def test_preserve_non_camera_overlays_and_conditional_scopes(self):
        (self.boot / "additional.txt").write_text("dtparam=i2c_arm=on\n")
        self.config.write_text("include additional.txt\ninclude optional.txt\n"
                               "[none]\ndtoverlay=imx219\n[cm5]\ndtoverlay=spi0-1cs\n")
        staged = self.apply("imx477", "cam0")
        self.assertIn("[cm5]\ndtoverlay=spi0-1cs\n[all]\n", staged["config_text"])
        self.assertEqual(staged["profile"], {"id": "imx477", "port": "cam0"})
        switched = self.apply("auto")
        self.assertEqual(switched["config_text"].count(BEGIN), 1)
        self.assertIn("camera_auto_detect=1", switched["config_text"])
        self.assertNotIn("dtoverlay=imx477", switched["config_text"])
        self.assertTrue(self.apply("restore")["pending_reboot"])
        self.assertEqual(self.config.read_text(), staged["config_text"])

    def test_block_ambiguous_or_uninspectable_camera_overlays(self):
        (self.boot / "camera.txt").write_text("dtoverlay=imx219\n")
        for text in ["include camera.txt\n", "[cm5]\ndtoverlay=imx219\n", "dtoverlay=imx999\n",
                     "dtoverlay=imx219\ndtoverlay=imx708,cam0\n", BEGIN + "\ndtparam=spi=on\n",
                     "dtoverlay=/vendor/imx219.dtbo\n", "os_prefix=other/\n", BEGIN + "\nstart_x=1\n" + END + "\n"]:
            with self.subTest(text=text):
                self.config.write_text(text)
                before = self.firmware.status()
                self.assertTrue(before["conflicts"])
                with self.assertRaises(FirmwareConflict):
                    self.apply("imx708")
                self.assertEqual(self.config.read_text(), text)

    def test_refuse_unknown_profile_missing_overlay_and_external_revision(self):
        revision = self.firmware.status()["revision"]
        for profile, port in (("imx708\ndtparam=spi=on", "cam1"), ("imx219", "cam2")):
            with self.assertRaises(ValueError):
                self.apply(profile, port)
        (self.boot / "overlays/imx708.dtbo").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.apply("imx708")
        self.apply("imx219")
        self.config.write_text(self.config.read_text() + "# Admin edit\n")
        self.assertFalse(self.firmware.status()["backup_available"])
        with self.assertRaises(FirmwareConflict):
            self.apply("restore", revision=revision)
        with self.assertRaisesRegex(ValueError, "No matching"):
            self.apply("restore")

    def test_concurrent_writers_have_one_winner(self):
        revision = self.firmware.status()["revision"]
        results = []
        def change(profile):
            try:
                self.apply(profile, revision=revision)
                results.append("saved")
            except FirmwareConflict:
                results.append("stale")
        threads = [threading.Thread(target=change, args=(p,)) for p in ("imx477", "imx219")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(2)
        self.assertCountEqual(results, ["saved", "stale"])

    def test_failed_publication_keeps_old_configuration_and_undo(self):
        original = self.config.read_bytes()
        staged = self.apply("imx219")
        def write(path, value, mode):
            if path == self.config:
                raise OSError("volume became read-only")
            atomic_text(path, value, mode)
        with patch("rpi_web_control.firmware.atomic_text", side_effect=write):
            with self.assertRaises(OSError):
                self.apply("imx477")
        self.assertEqual(self.firmware.status(), staged)
        self.apply("restore")
        self.assertEqual(self.config.read_bytes(), original)

    def test_custom_profile_from_manual_overlay_to_managed_and_back(self):
        catalog = self.directory / "sensor-profiles.json"
        custom = {"id": "vendor-fast", "label": "Vendor high-speed camera", "overlay": "vendor-module",
                  "params": ["rotation=180", "fast-mode"]}
        catalog.write_text(json.dumps([custom]))
        self.assertFalse(next(p for p in self.firmware.status()["profiles"] if p["id"] == "vendor-fast")["installed"])
        with self.assertRaisesRegex(ValueError, "missing"):
            self.apply("vendor-fast")
        (self.boot / "overlays/vendor-module.dtbo").touch()
        original = "dtoverlay=vendor-module,fast-mode,cam1,rotation=180\n"
        self.config.write_text(original)
        self.assertEqual(self.firmware.status()["profile"], {"id": "vendor-fast", "port": "cam1"})
        staged = self.apply("vendor-fast", "cam0")
        self.assertTrue(staged["managed"])
        self.assertEqual(staged["config_text"].count("vendor-module"), 1)
        self.assertIn("dtoverlay=vendor-module,rotation=180,fast-mode,cam0", staged["config_text"])
        self.assertEqual(staged["profile"], {"id": "vendor-fast", "port": "cam0"})
        updated = self.apply("restore")
        self.assertEqual(updated["config_text"], original)
        self.assertFalse(updated["pending_reboot"])

    def test_stale_catalog_and_unlisted_parameters(self):
        catalog = self.directory / "sensor-profiles.json"
        status = self.firmware.status()
        entry = {"id": "custom", "label": "Custom camera", "overlay": "vendor"}
        catalog.write_text(json.dumps([entry]))
        with self.assertRaises(FirmwareConflict):
            self.apply("imx219", revision=status["revision"])
        self.config.write_text("dtoverlay=vendor,extra-option\n")
        self.assertTrue(self.firmware.status()["conflicts"])
        with self.assertRaises(FirmwareConflict):
            self.apply("imx477")
        self.config.write_text("[all]\n")
        with self.assertRaisesRegex(ValueError, "Unknown sensor profile"):
            self.apply("vendor")

    def test_reject_malformed_catalog_and_keep_config_readable(self):
        catalog = self.directory / "sensor-profiles.json"
        base = {"id": "vendor", "label": "Vendor", "overlay": "vendor"}
        invalid = [{"profiles": []}, [dict(base, id="restore")], [dict(base, overlay="../vendor")],
                   [dict(base, params=["cam0"])], [dict(base, params=["x=1", "x=2"])],
                   [dict(base, params=["foo\ndtoverlay=other"])],
                   [dict(base, overlay="imx219")], [dict(base, unexpected=True)]]
        for payload in invalid:
            with self.subTest(payload=payload):
                catalog.write_text(json.dumps(payload))
                before = self.firmware.status()
                self.assertTrue(before["available"])
                self.assertIn("Invalid sensor profile catalog", before["conflicts"][0])
                with self.assertRaises(FirmwareConflict):
                    self.apply("imx219")
                self.assertEqual(before["config_text"], self.config.read_text())
        catalog.unlink()
        recovered = self.apply("imx219")
        self.assertTrue(recovered["managed"])

    def test_target_check_rejects_an_unmounted_boot_directory(self):
        with patch("pathlib.Path.read_text", return_value="Raspberry Pi Compute Module 5 Rev 1.0"):
            with self.assertRaisesRegex(RuntimeError, "Mount the boot partition"):
                verify_target(self.boot)

    def test_missing_socket_is_visible(self):
        with patch("rpi_web_control.firmware.SOCKET", str(self.directory / "absent.sock")):
            status = Firmware().status()
            self.assertFalse(status["available"])
            self.assertFalse(status["simulated"])
            self.assertIn("unavailable", status["message"])

    def test_refuse_symlink_outside_boot_in_root_and_includes(self):
        self.config.write_text("include external.txt\n")
        file = self.directory / "outside.txt"
        file.write_text("secret=yes\n")
        (self.boot / "external.txt").symlink_to(file)
        status = self.firmware.status()
        self.assertTrue(status["conflicts"])
        self.assertNotIn("secret=yes", json.dumps(status))
        self.config.unlink()
        self.config.symlink_to(file)
        self.assertFalse(self.firmware.status()["available"])
