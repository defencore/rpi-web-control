"""Check that installed socket, service and documentation paths agree."""

import configparser
import io
import json
import runpy
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_board_diagnostics_continue_on_partial_install_without_opening_busy_camera(self):
        script = (ROOT / "tools/inspect-board.sh").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            tools = Path(tmp)
            for name in ("date", "uname", "sed", "tr", "head", "cat"):
                (tools / name).symlink_to(shutil.which(name))
            for name, body in {
                "systemctl": 'if [ "$1" = is-active ]; then echo active; else echo "$@"; fi',
                "journalctl": 'echo "$@"; exit 1',
                "rpicam-hello": 'echo "CAMERA PROBED"',
            }.items():
                program = tools / name
                program.write_text(f"#!/bin/sh\n{body}\n")
                program.chmod(0o755)
            result = subprocess.run(["sh", "-s", "--", "--diagnostics"], executable=shutil.which("sh"),
                                    input=script, capture_output=True, env={"PATH": tmp}, timeout=10)
        # The mock has no dpkg, so the inventory cannot confirm an arm64 target.
        self.assertEqual(result.returncode, 2, result.stderr)
        report = result.stdout.decode()
        self.assertNotIn("CAMERA PROBED", report)
        self.assertIn("apt-get is unavailable.", report)
        self.assertIn("Filesystem space", report)
        self.assertIn("-n 80 -u rpi-web-control.service", report)
        self.assertIn("System clock settings", report)
        for name in ("network", "firmware", "nvme", "clock"):
            self.assertIn(f"-u rpi-web-control-{name}.socket -u rpi-web-control-{name}@*.service", report)
        self.assertIn("Camera kernel messages", report)

    def test_archived_helpers_and_workstation_tool(self):
        builder = runpy.run_path(str(ROOT / "packaging" / "build.py"))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "controller.deb"
            builder["build"](output, 0)
            control = subprocess.check_output(["ar", "-p", output, "control.tar.gz"])
            with tarfile.open(fileobj=io.BytesIO(control)) as metadata:
                conffiles = metadata.extractfile("./conffiles").read().decode().splitlines()
            payload = subprocess.check_output(["ar", "-p", output, "data.tar.gz"])
            with tarfile.open(fileobj=io.BytesIO(payload)) as data:
                def unit(name):
                    config = configparser.ConfigParser(interpolation=None, strict=False)
                    path = f"./usr/lib/systemd/system/rpi-web-control{name}"
                    config.read_string(data.extractfile(path).read().decode())
                    return config

                main = unit(".service")
                self.assertEqual(main["Service"]["User"], "rpi-web-control")
                environment_file = main["Service"]["EnvironmentFile"]
                self.assertIn(environment_file, conffiles)
                defaults = data.extractfile("." + environment_file).read().decode()
                env = dict(item.split("=", 1) for item in shlex.split(defaults, comments=True))
                self.assertEqual(env["RPI_CONTROL_REQUIRE_TOKEN"], "0")
                self.assertEqual(main["Service"]["CapabilityBoundingSet"], "")
                for name in ("network", "firmware", "nvme", "clock"):
                    socket_name = f"rpi-web-control-{name}.socket"
                    self.assertIn(socket_name, main["Unit"]["Wants"].split())
                    self.assertIn(socket_name, main["Unit"]["After"].split())
                    listener = unit(f"-{name}.socket")["Socket"]
                    self.assertEqual(listener["SocketGroup"], main["Service"]["Group"])
                    self.assertEqual(listener["SocketMode"], "0660")
                    command = shlex.split(unit(f"-{name}@.service")["Service"]["ExecStart"])
                    self.assertEqual(command[:2], ["/usr/bin/python3", "-I"])
                    self.assertTrue(data.getmember("." + command[2]).isfile())
                clock_unit = unit("-clock@.service")["Service"]
                self.assertEqual(clock_unit["CapabilityBoundingSet"], "CAP_SYS_TIME")
                self.assertEqual(clock_unit["AmbientCapabilities"], "CAP_SYS_TIME")
                self.assertEqual(clock_unit["PrivateNetwork"], "yes")
                self.assertEqual(clock_unit["RestrictAddressFamilies"], "AF_UNIX")
                network_unit = unit("-network@.service")["Service"]
                self.assertEqual(network_unit["Group"], main["Service"]["Group"])
                self.assertEqual(network_unit["RuntimeDirectoryMode"], "0710")
                self.assertEqual(network_unit["UMask"], "0077")
                self.assertEqual(network_unit["RuntimeDirectoryPreserve"], "yes")
                recovery = unit("-recovery.service")["Service"]
                self.assertEqual(recovery["CapabilityBoundingSet"].split(), ["CAP_NET_ADMIN", "CAP_NET_RAW"])
                self.assertEqual(recovery["RuntimeDirectory"], "rpi-web-control-recovery")
                self.assertEqual(unit("-recovery.timer")["Install"]["WantedBy"], "timers.target")
                self.assertTrue(data.getmember("." + shlex.split(recovery["ExecStart"])[2]).isfile())
                self.assertEqual(env["RPI_CONTROL_RECOVERY_ADDRESS"], "192.168.55.100/24")

                # Mounts created in a filesystem sandbox would disappear when
                # the socket instance exits. Exercise the isolated-Python entry
                # point with the archived modules and a harmless invalid request.
                nvme_unit = unit("-nvme@.service")["Service"]
                for key in ("PrivateDevices", "PrivateMounts", "PrivateTmp", "ProtectSystem",
                            "ProtectHome", "ReadOnlyPaths", "ReadWritePaths"):
                    self.assertNotIn(key, nvme_unit)
                library = "./usr/lib/rpi-web-control/"
                for member in data.getmembers():
                    if member.isfile() and member.name.startswith(library):
                        path = Path(tmp) / member.name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data.extractfile(member).read())
                for name in ("nvme", "clock", "network"):
                    helper = Path(tmp) / library / f"{name}-helper.py"
                    client, peer = socket.socketpair()
                    with client, peer:
                        client.settimeout(2)
                        client.sendall(b'{"action":"invalid"}\n')
                        subprocess.run([sys.executable, "-I", helper], stdin=peer, stdout=peer,
                                       check=True, timeout=6, cwd=tmp)
                        with client.makefile("rb") as reply:
                            result = json.loads(reply.readline(2048))
                            self.assertIn("error", result)
                            if name in ("nvme", "network"):
                                self.assertEqual(result["kind"], "invalid")

                docs = "./usr/share/doc/rpi-web-control"
                inventory = data.extractfile(f"{docs}/tools/inspect-board.sh").read()
                self.assertEqual(inventory, (ROOT / "tools/inspect-board.sh").read_bytes())
                subprocess.run(["sh", "-n"], input=inventory, check=True)
                checker = data.extractfile(f"{docs}/tools/check-camera-modes.py").read()
                self.assertEqual(checker, (ROOT / "tools/check-camera-modes.py").read_bytes())
                self.assertEqual(data.extractfile(library + "tools/check-camera-modes.py").read(), checker)
                converter = (ROOT / "tools/render_recording.py").read_bytes()
                self.assertEqual(data.extractfile(f"{docs}/tools/render_recording.py").read(), converter)
                self.assertEqual(data.extractfile(library + "tools/render_recording.py").read(), converter)
                self.assertTrue(data.getmember(f"{docs}/docs/post-processing.md").isfile())
