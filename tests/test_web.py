"""Run the actual request handlers over connected sockets without a listening port."""

import http.client
import io
import json
import os
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from rpi_web_control.__main__ import configured_token
from rpi_web_control.application import Application
from rpi_web_control.web import Server


class WebTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name).resolve()
        root = self.state / "recordings"
        root.mkdir()
        self.app = Application(self.state, [root], "simulated")
        self.addCleanup(self.app.close)
        self.token = "local-development-test-token"
        token_file = self.state / "token"
        token_file.write_text(self.token + "\n", encoding="ascii")
        with patch.dict(os.environ, {"RPI_CONTROL_REQUIRE_TOKEN": "1", "RPI_CONTROL_TOKEN_FILE": str(token_file)}):
            self.server = Server(("127.0.0.1", 0), self.app, configured_token(self.state, True),
                                 bind_and_activate=False)
        self.addCleanup(self.server.server_close)
        self.cookie = None
        self.client_tls = None

    def request(self, path, body=None, *, method=None, **headers):
        client, worker = socket.socketpair()
        client.settimeout(5)
        self.server.process_request(worker, ("127.0.0.1", 1234))
        if self.client_tls:
            client = self.client_tls.wrap_socket(client, server_hostname="localhost")
        connection = http.client.HTTPConnection("localhost")
        connection.sock = client
        self.addCleanup(connection.close)
        if self.cookie:
            headers.setdefault("Cookie", self.cookie)
        if body is None:
            connection.request(method or "GET", path, headers=headers)
        else:
            headers.setdefault("Content-Type", "application/json")
            connection.request("POST", path, json.dumps(body).encode(), headers)
        response = connection.getresponse()
        self.addCleanup(response.close)
        return response

    def login(self):
        result = self.request("/api/login", {"token": self.token})
        self.assertEqual(result.status, 200)
        self.cookie = result.getheader("Set-Cookie").split(";")[0]
        result.read()
        return result

    def test_direct_access(self):
        # An upgrade may retain a token and its path without setting the new switch.
        with patch.dict(os.environ, {"RPI_CONTROL_TOKEN_FILE": str(self.state / "token")}, clear=True):
            token = configured_token(self.state, False)
        self.server = Server(("127.0.0.1", 0), self.app, token, bind_and_activate=False)
        self.addCleanup(self.server.server_close)
        self.assertFalse(json.load(self.request("/api/status"))["token_required"])
        self.assertEqual(self.request("/api/login", {"token": self.token}).status, 409)
        self.assertEqual(self.request("/api/recording", {"enabled": True}, Origin="http://external").status, 403)
        sync = {"confirm": True, "browser_realtime_ms": time.time_ns() // 1_000_000}
        self.assertEqual(self.request("/api/clock", sync, Origin="http://external").status, 403)
        self.assertEqual(self.request("/api/clock", sync).status, 200)
        for enabled in (True, False):
            self.assertEqual(self.request("/api/recording", {"enabled": enabled}).status, 200)

    def test_expired_session_requires_sign_in_again(self):
        with patch("rpi_web_control.web.SESSION_SECONDS", 0):
            self.login()
        self.assertEqual(self.request("/api/status").status, 401)
        self.assertEqual(self.request("/api/recording", {"enabled": True}).status, 401)
        self.login()
        self.assertTrue(json.load(self.request("/api/status"))["token_required"])

    def test_confirmed_clock_sync_changes_subsequent_status(self):
        target = time.time_ns() // 1_000_000 - 3_600_000
        sync = {"confirm": True, "browser_realtime_ms": target}
        self.assertEqual(self.request("/api/clock", sync).status, 401)
        self.login()
        initial = json.load(self.request("/api/status"))["device_realtime_ms"]
        self.assertIs(type(initial), int)
        self.assertGreater(initial, target + 3_000_000)
        self.assertEqual(self.request("/api/clock", sync, Origin="http://external").status, 403)
        for body in ({}, {"confirm": True}, {"browser_realtime_ms": target},
                     {**sync, "confirm": 1}, {**sync, "extra": True},
                     {**sync, "browser_realtime_ms": True}, {**sync, "browser_realtime_ms": str(target)},
                     {**sync, "browser_realtime_ms": target + 0.5}, {**sync, "browser_realtime_ms": -1}):
            with self.subTest(body=body):
                self.assertEqual(self.request("/api/clock", body).status, 400)

        self.assertEqual(self.request("/api/recording", {"enabled": True}).status, 200)
        self.assertEqual(self.request("/api/clock", sync).status, 409)
        self.assertEqual(self.request("/api/recording", {"enabled": False}).status, 200)
        with patch.object(self.app.clock, "set_time", side_effect=RuntimeError("Clock helper unavailable")):
            self.assertEqual(self.request("/api/clock", sync).status, 503)
        self.assertGreaterEqual(json.load(self.request("/api/status"))["device_realtime_ms"], initial)

        start = time.monotonic_ns()
        response = self.request("/api/clock", sync)
        self.assertEqual(response.status, 200)
        result = json.load(response)["device_realtime_ms"]
        sampled = json.load(self.request("/api/status"))["device_realtime_ms"]
        elapsed_ms = (time.monotonic_ns() - start) // 1_000_000
        self.assertIs(type(result), int)
        self.assertLessEqual(target, result)
        self.assertLessEqual(result, sampled)
        self.assertLessEqual(sampled, target + elapsed_ms + 1)

    def test_operator_can_stop_and_pause_a_gpio_owned_recording(self):
        endpoint = "/api/triggers/pause"
        self.assertEqual(self.request(endpoint, {"paused": True}).status, 401)
        self.login()
        self.assertEqual(self.request(endpoint, {"paused": True}, Origin="http://external").status, 403)
        for invalid in ({}, {"paused": 1}, {"paused": False, "stop_manual": True}, {"paused": True, "pin": 17}):
            self.assertEqual(self.request(endpoint, invalid).status, 400)
        self.app.trigger("Button", True)
        self.assertEqual(self.request(endpoint, {"paused": True, "stop_manual": True}).status, 200)
        status = json.load(self.request("/api/status"))
        self.assertTrue(status["triggers_paused"])
        self.assertEqual(status["recording_sources"], [])
        self.assertFalse(status["camera"]["recording"])
        self.assertEqual(self.request(endpoint, {"paused": False}).status, 200)
        self.assertFalse(json.load(self.request("/api/status"))["triggers_paused"])

    def test_session_and_http_controls(self):
        self.assertEqual(self.request("/api/status").status, 401)
        self.assertEqual(self.request("/api/login", {"token": "incorrect"}).status, 401)
        self.assertEqual(self.request("/api/login", {"token": self.token}, Origin="http://external").status, 403)
        homepage = self.request("/", method="HEAD")
        self.assertEqual(homepage.status, 200)
        self.assertEqual(int(homepage.getheader("Content-Length")), len(self.request("/").read()))
        self.assertEqual(homepage.read(), b"")
        self.login()
        self.assertTrue(json.load(self.request("/api/capabilities"))["available"])
        for path in ("/", "/static/style.css", "/static/app.js", "/static/ui.js", "/static/device.js",
                     "/static/network.js", "/static/wifi_station.js"):
            response = self.request(path)
            self.assertEqual(response.status, 200, path)
            self.assertTrue(response.read())
        self.assertEqual(self.request("/api/config", {"camera": {"fps": "30"}}).status, 400)
        self.assertEqual(self.request("/api/config", {"camera": {"width": 320, "height": 240}}).status, 200)
        self.assertEqual(self.request("/api/recording", {"enabled": True}).status, 200)
        self.assertEqual(self.request("/api/config", {"camera": {"fps": 24}}).status, 409)
        self.assertFalse(json.load(self.request("/api/status"))["camera"]["preview"])
        self.assertEqual(self.request("/api/preview", {"enabled": True}).status, 200)
        stream = self.request("/api/stream.mjpg")
        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.readline(), b"--frame\r\n")
        self.assertEqual(stream.readline(), b"Content-Type: image/jpeg\r\n")
        size = int(stream.readline().split(b":")[1])
        self.assertEqual(stream.readline(), b"\r\n")
        self.assertTrue(stream.read(size).startswith(b"\xff\xd8"))
        self.assertEqual(self.request("/api/preview", {"enabled": False}).status, 200)
        self.assertEqual(json.load(self.request("/api/status"))["recording_sources"], ["manual"])
        self.assertEqual(self.request("/api/recording", {"enabled": False}).status, 200)
        self.assertEqual(self.request("/api/logout", {}).status, 200)
        self.assertEqual(self.request("/api/status").status, 401)

    def test_camera_mode_test_selection_lifecycle_and_capture_ownership(self):
        path = "/api/camera/mode-test"
        start = {"action": "start"}
        cancel = {"action": "cancel"}
        self.assertEqual(self.request(path, start).status, 401)
        self.login()
        self.assertEqual(self.request(path, start, Origin="http://external").status, 403)
        mode = json.load(self.request("/api/capabilities"))["modes"][0]
        selected = {key: mode[key] for key in ("width", "height", "bit_depth")}
        for bad in ({}, {"action": "other"}, {**start, "seconds": 10}, {**start, "mode": {"width": 1}},
                    {**cancel, "mode": selected}):
            with self.subTest(body=bad):
                self.assertEqual(self.request(path, bad).status, 400)
        self.assertEqual(self.request("/api/preview", {"enabled": True}).status, 200)
        self.assertEqual(self.request(path, start).status, 409)
        self.assertEqual(self.request("/api/preview", {"enabled": False}).status, 200)
        self.assertEqual(self.request(path, start).status, 202)
        self.assertEqual(self.request("/api/preview", {"enabled": True}).status, 409)
        self.assertEqual(self.request(path, start).status, 409)
        self.assertEqual(json.load(self.request(path, cancel))["state"], "cancelled")

        initial = self.app.config["camera"].copy()
        response = self.request(path, {**start, "mode": selected})
        self.assertEqual(response.status, 202)
        test = json.load(response)
        deadline = time.monotonic() + 10
        while test["state"] == "running" and time.monotonic() < deadline:
            time.sleep(.05)
            test = json.load(self.request("/api/status"))["camera"]["mode_test"]
        self.assertEqual(test["total"], 1)
        self.assertEqual(len(test["results"]), 1)
        result = test["results"][0]
        self.assertEqual(result["mode"], mode)
        self.assertGreater(result["frames"], 1)
        self.assertGreater(result["achieved_fps"], 0)
        self.assertEqual(self.app.config["camera"], initial)

        self.assertEqual(self.request(path, start).status, 202)
        self.app.trigger("contact", True)
        snapshot = json.load(self.request("/api/status"))
        self.assertEqual(snapshot["camera"]["mode_test"]["state"], "cancelled")
        self.assertTrue(snapshot["camera"]["recording"])
        self.assertEqual(snapshot["recording_sources"], ["trigger:contact"])
        self.assertEqual(self.request(path, start).status, 409)
        self.app.camera.stop_recording()
        self.assertEqual(self.request(path, start).status, 409)
        self.app.trigger("contact", False)

    def test_firmware_stage_and_confirmed_simulated_reboot(self):
        for path in ("/api/firmware", "/api/storage"):
            self.assertEqual(self.request(path).status, 401)
        self.login()
        status = json.load(self.request("/api/firmware"))
        self.assertTrue(status["simulated"])
        self.assertIn("camera_auto_detect", status["config_text"])
        imx577 = next(item for item in status["profiles"] if item["id"] == "imx577")
        self.assertTrue(imx577["installed"])
        cam0 = "camera_auto_detect=0\ndtoverlay=imx577,cam0,4lane,link-frequency=750000000"
        self.assertEqual(imx577["directives"]["cam0"], cam0)
        profile = {"profile": "imx577", "port": "cam0", "revision": status["revision"]}
        staged = json.load(self.request("/api/firmware", profile))
        self.assertIn(cam0 + "\n", staged["config_text"])
        self.assertTrue(staged["pending_reboot"])
        self.assertEqual(self.request("/api/firmware", profile).status, 409)
        reboot = {"revision": staged["revision"], "confirm": True}
        self.assertEqual(self.request("/api/reboot", {**reboot, "confirm": False}).status, 400)
        self.assertEqual(self.request("/api/recording", {"enabled": True}).status, 200)
        self.assertEqual(self.request("/api/reboot", reboot).status, 409)
        self.assertEqual(self.request("/api/recording", {"enabled": False}).status, 200)
        self.assertEqual(self.request("/api/reboot", reboot).status, 202)
        self.assertFalse(json.load(self.request("/api/firmware"))["pending_reboot"])
        self.assertTrue(json.load(self.request("/api/storage"))["devices"])

    def test_live_receiver_override_and_default_peer(self):
        override = {"enabled": True, "host": " 192.168.55.10 "}
        self.assertEqual(self.request("/api/live", override).status, 401)
        self.login()
        self.assertEqual(json.load(self.request("/api/status"))["client_address"], "127.0.0.1")
        self.assertEqual(self.request("/api/live", override, Origin="http://external").status, 403)
        with patch.object(self.app.camera, "set_live") as send:
            for host in ("other-host", "192.0.2.1:5004", "::ffff:224.0.0.1", "::", None, 1234, False):
                with self.subTest(host=host):
                    self.assertEqual(self.request("/api/live", {"enabled": True, "host": host}).status, 400)
            self.assertEqual(self.request("/api/live", {"enabled": True, "port": 500.4}).status, 400)
            send.assert_not_called()
            self.assertEqual(self.request("/api/live", {"enabled": True, "port": 5008},
                                          **{"X-Forwarded-For": "192.0.2.1"}).status, 200)
            send.assert_called_once_with(True, "127.0.0.1", 5008, max_fps=None)
            self.assertEqual(self.request("/api/live", {"enabled": True, "host": " "}).status, 200)
            send.assert_called_with(True, "127.0.0.1", 5004, max_fps=None)
            self.assertEqual(self.request("/api/live", override).status, 200)
            send.assert_called_with(True, "192.168.55.10", 5004, max_fps=None)
            self.assertEqual(self.request("/api/live", {**override, "host": "fd00::12", "port": 6000}).status, 200)
            send.assert_called_with(True, "fd00::12", 6000, max_fps=None)
            self.assertEqual(self.request("/api/live", {**override, "max_fps": 60}).status, 200)
            send.assert_called_with(True, "192.168.55.10", 5004, max_fps=60)
        for invalid in (0, 1001, -1, True, 59.5, "60"):
            with self.subTest(max_fps=invalid):
                self.assertEqual(self.request("/api/live", {"enabled": True, "max_fps": invalid}).status, 400)
        self.assertEqual(self.request("/api/live", {"enabled": False, "max_fps": 0}).status, 400)
        self.assertEqual(self.request("/api/live", {"enabled": False}).status, 200)

    def test_browse_default_while_nvme_is_selected(self):
        self.login()
        directories = json.load(self.request("/api/status"))["recording_directories"]
        self.assertEqual(directories["current"], directories["default"])
        self.assertEqual(self.request("/api/recording", {"enabled": True}).status, 200)
        deadline = time.monotonic() + 3
        while self.app.camera.status()["frames"] < 1 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.request("/api/recording", {"enabled": False}).status, 200)
        disk, = json.load(self.request("/api/nvme"))["devices"]
        volume, = disk["volumes"]
        self.assertEqual(self.request("/api/nvme", {"action": "mount", "device": disk["device"],
                                                   "volume": volume["device"], "revision": disk["revision"]}).status, 200)
        self.assertEqual(self.request("/api/config", {"storage": {"directory": directories["nvme"]}}).status, 200)
        persisted = self.app.store.path.read_bytes()
        page = json.load(self.request("/api/recordings?source=default"))
        self.assertEqual(page["directory"], directories["default"])
        clip, = page["sessions"]
        video = clip["files"][0]
        self.assertIn("source=default", video["url"])
        response = self.request(video["url"])
        self.assertEqual(response.status, 200)
        expected = Path(directories["default"], clip["name"], video["name"]).read_bytes()
        self.assertTrue(expected)
        self.assertEqual(response.read(), expected)
        self.assertEqual(json.load(self.request("/api/recordings"))["sessions"], [])
        self.assertEqual(json.load(self.request("/api/status"))["recording_directories"]["current"], directories["nvme"])
        self.assertEqual(self.app.store.path.read_bytes(), persisted)

    def test_download_desktop_converter(self):
        url = "/api/tools/render_recording.py"
        self.assertEqual(self.request(url).status, 401)
        self.login()
        expected = (Path(__file__).resolve().parents[1] / "tools/render_recording.py").read_bytes()
        response = self.request(url)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="render_recording.py"')
        self.assertEqual(response.read(), expected)
        head = self.request(url, method="HEAD")
        self.assertEqual((head.status, head.getheader("Content-Length"), head.read()), (200, str(len(expected)), b""))

    def test_cleanup_is_confined_to_the_reviewed_external_recording_volume(self):
        endpoint = "/api/recordings/cleanup"
        self.assertEqual(self.request(endpoint, {}).status, 401)
        self.assertEqual(self.request("/api/usb", {}).status, 401)
        self.login()
        self.assertEqual(self.request(endpoint, {}, Origin="http://external").status, 403)
        system = json.load(self.request("/api/recordings"))
        self.assertFalse(system["cleanup_allowed"])
        system_plan = {"action": "plan", "source": "default", "target_id": system["target_id"]}
        self.assertEqual(self.request(endpoint, system_plan).status, 400)
        disk, = json.load(self.request("/api/usb"))["devices"]
        mount = {"action": "mount", "device": disk["device"], "revision": disk["revision"],
                 "volume": disk["volumes"][0]["device"]}
        self.assertEqual(self.request("/api/usb", mount, Origin="http://external").status, 403)
        usb = json.load(self.request("/api/usb", mount))
        self.assertTrue(usb["ready"])
        self.assertEqual(self.request("/api/config", {"storage": {"directory": usb["directory"]}}).status, 200)
        self.assertEqual(self.request("/api/recording", {"enabled": True}).status, 200)
        listing = json.load(self.request("/api/recordings?source=usb"))
        plan = {"action": "plan", "source": "usb", "target_id": listing["target_id"]}
        self.assertEqual(self.request(endpoint, plan).status, 409)
        deadline = time.monotonic() + 3
        while self.app.camera.status()["frames"] < 1 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.request("/api/recording", {"enabled": False}).status, 200)
        listing = json.load(self.request("/api/recordings?source=usb"))
        self.assertTrue(listing["cleanup_allowed"])
        row, = listing["sessions"]
        review = json.load(self.request(endpoint, plan))
        self.assertEqual((review["count"], review["medium"], review["confirmation"]), (1, "usb", "CLEAN"))
        self.assertGreater(review["bytes"], 0)
        delete = {**plan, "action": "delete", "revision": review["revision"], "confirmation": "CLEAN"}
        self.assertEqual(self.request(endpoint, {**delete, "confirmation": "DELETE"}).status, 400)
        self.assertEqual(self.request(endpoint, {**delete, "source": "default"}).status, 400)
        self.assertEqual(self.request(endpoint, {**delete, "target_id": "0" * 64}).status, 409)
        self.assertEqual(self.request(row["archive"]["url"], method="HEAD").status, 200)
        removed = json.load(self.request(endpoint, delete))
        self.assertEqual(removed["removed_count"], 1)
        self.assertEqual(self.request(row["archive"]["url"], method="HEAD").status, 404)

    def test_download_completed_clip_and_sidecars_with_resume(self):
        self.assertEqual(self.request("/api/recordings").status, 401)
        self.assertEqual(self.request("/api/recordings/session/video.y4m").status, 401)
        self.assertEqual(self.request("/api/recordings/session/archive.zip").status, 401)
        self.login()
        default_directory = self.app.status()["config"]["storage"]["directory"]
        inventory = json.load(self.request("/api/nvme"))
        disk, = inventory["devices"]
        prepared = self.request("/api/nvme", {"action": "prepare", "device": disk["device"],
                                              "revision": disk["revision"], "confirmation": disk["device"]})
        self.assertEqual(prepared.status, 200)
        self.assertEqual(self.request("/api/config", {"storage": {"directory": inventory["directory"]}}).status, 200)
        self.assertEqual(self.request("/api/config", {"camera": {"width": 320, "height": 240}}).status, 200)
        self.assertEqual(self.request("/api/recording", {"enabled": True}).status, 200)
        self.assertEqual(json.load(self.request("/api/recordings"))["sessions"], [])
        deadline = time.monotonic() + 3
        directory = Path(self.app.camera.status()["session"])
        lookup = f"/api/recordings?source=nvme&session={directory.name}"
        self.assertEqual(json.load(self.request(lookup))["sessions"], [])
        while self.app.camera.status()["frames"] < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.request("/api/recording", {"enabled": False}).status, 200)
        current_url = json.load(self.request("/api/recordings"))["sessions"][0]["files"][0]["url"]
        self.assertEqual(self.request("/api/config", {"storage": {"directory": default_directory}}).status, 200)
        self.assertEqual(json.load(self.request("/api/recordings"))["sessions"], [])
        self.assertEqual(self.request(current_url).status, 409)
        page = json.load(self.request("/api/recordings?source=nvme"))
        row, = page["sessions"]
        self.assertEqual(json.load(self.request(lookup + "&target_id=" + page["target_id"])), page)
        self.assertEqual(row["name"], directory.name)
        archive = row["archive"]
        self.assertEqual(archive["name"], directory.name + ".zip")
        self.assertIn("source=nvme", archive["url"])
        head = self.request(archive["url"], method="HEAD")
        self.assertEqual((head.status, head.read(), head.getheader("Accept-Ranges")), (200, b"", "none"))
        self.assertEqual(head.getheader("Content-Disposition"), f'attachment; filename="{directory.name}.zip"')
        self.assertEqual(head.getheader("Content-Type"), "application/zip")
        download = self.request(archive["url"], Range="bytes=42-")
        self.assertEqual(download.status, 200)
        with zipfile.ZipFile(io.BytesIO(download.read())) as bundle:
            self.assertEqual(set(bundle.namelist()), {"video.mjpeg", "frames.csv", "session.json"})
            for name in bundle.namelist():
                self.assertEqual(bundle.read(name), (directory / name).read_bytes())
        self.assertEqual(self.request(archive["url"].replace(page["target_id"], "0" * 64)).status, 409)
        for item in row["files"]:
            body = self.request(item["url"])
            self.assertEqual(body.status, 200)
            self.assertEqual(body.getheader("Content-Disposition"), f'attachment; filename="{item["name"]}"')
            self.assertEqual(body.read(), (directory / item["name"]).read_bytes())
        clip = row["files"][0]
        data = (directory / clip["name"]).read_bytes()
        head = self.request(clip["url"], method="HEAD")
        self.assertEqual((head.status, head.read(), head.getheader("Content-Length")), (200, b"", str(len(data))))
        for header, part in (("bytes=1-9", data[1:10]), ("bytes=12-", data[12:]), ("bytes=-7", data[-7:])):
            with self.subTest(header=header):
                resp = self.request(clip["url"], Range=header, **{"If-Range": head.getheader("ETag")})
                self.assertEqual(resp.status, 206)
                self.assertEqual(resp.read(), part)
        miss = self.request(clip["url"], Range=f"bytes={len(data)}-")
        self.assertEqual((miss.status, miss.getheader("Content-Range")), (416, f"bytes */{len(data)}"))
        stale_etag = self.request(clip["url"], Range="bytes=1-9", **{"If-Range": '"old"'})
        self.assertEqual((stale_etag.status, stale_etag.read()), (200, data))
        stale_target = clip["url"].replace(page["target_id"], "0" * 64)
        self.assertEqual(self.request(stale_target).status, 409)
        for path in ("/api/recordings?source=foo", "/api/recordings?directory=/etc",
                     "/api/recordings?cursor=one&cursor=two", "/api/recordings?session=invalid",
                     lookup + "&cursor=" + directory.name):
            self.assertEqual(self.request(path).status, 400)
        for path in ("/api/recordings/../session.json", "/api/recordings/%2Fetc/frames.csv",
                     f"/api/recordings/{row['name']}/something-else"):
            self.assertEqual(self.request(path).status, 404)
        self.assertEqual(self.request("/api/logout", {}).status, 200)
        self.assertEqual(self.request(clip["url"], Range="bytes=0-9").status, 401)

    def test_nvme_authentication_and_cross_origin(self):
        self.assertEqual(self.request("/api/nvme").status, 401)
        self.assertEqual(self.request("/api/nvme", {}).status, 401)
        self.login()
        self.assertEqual(self.request("/api/nvme", {}, Origin="http://external").status, 403)
        self.assertTrue(json.load(self.request("/api/nvme"))["simulated"])

    def test_tls_and_secure_session(self):
        cert, key = self.state / "test.crt", self.state / "test.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj",
                        "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True)
        self.server.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server.tls.load_cert_chain(cert, key)
        self.client_tls = ssl.create_default_context(cafile=str(cert))
        self.assertIn("Secure", self.login().getheader("Set-Cookie"))
        self.assertEqual(self.request("/api/status").status, 200)
        self.assertEqual(self.request("/api/config", {"triggers": []}, Origin="http://localhost").status, 403)
