"""Inspect boot settings and stage an allowlisted CSI overlay for the next boot."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .sensor_profiles import IMX577_SPEC, PROFILES, parse_profiles

FIRMWARE = Path("/boot/firmware")
STATE = Path("/var/lib/rpi-web-control-firmware")
SOCKET = "/run/rpi-web-control-firmware.sock"
CATALOG = Path("/etc/rpi-web-control/sensor-profiles.json")
MAX_CONFIG = 64 * 1024
MAX_REPLY = MAX_CONFIG * 8
BEGIN = "# BEGIN rpi-web-control camera profile"
END = "# END rpi-web-control camera profile"
BACKUP = "config.txt.rpi-web-control.bak"
CAMERA_OVERLAY = re.compile(r"(?:imx\d|ov\d|ar0\d|arducam|camera|tc358743)")
PORTS = ("cam0", "cam1")


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def read_file(path, *, root_owned=False):
    """Read a small, regular boot file without following a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if root_owned and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ValueError("The sensor catalog must be root-owned without group or public write access.")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Expected a regular firmware configuration file.")
        raw = stream.read(MAX_CONFIG + 1)
    if len(raw) > MAX_CONFIG:
        raise ValueError("Firmware configuration exceeds 64 KiB.")
    return raw.decode("utf-8")


def atomic_text(path, value, mode):
    fd, temporary = tempfile.mkstemp(prefix=".rpi-web-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(fd, mode)
            stream.write(value.encode("utf-8"))
            stream.flush()
            os.fsync(fd)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def overlay_profile(value, profiles):
    parts = [item.strip() for item in value.split(",")]
    if sum(item in PORTS for item in parts[1:]) > 1:
        return None
    port = "cam0" if "cam0" in parts[1:] else "cam1"
    params = sorted(item for item in parts[1:] if item not in PORTS)
    for name, (_, overlay) in profiles.items():
        if not overlay:
            continue
        expected, *options = overlay.split(",")
        if expected == parts[0] and sorted(options) == params:
            return {"id": name, "port": port}
    return None


def directives(profile, port, profiles):
    if profile == "auto":
        return ["camera_auto_detect=1"]
    overlay = profiles[profile][1]
    if port == "cam0":
        if overlay == IMX577_SPEC:
            name, params = overlay.split(",", 1)
            overlay = f"{name},cam0,{params}"
        else:
            overlay += ",cam0"
    return ["camera_auto_detect=0", "dtoverlay=" + overlay]


def overlay_files(spec):
    name = spec.partition(",")[0]
    # The Raspberry Pi OS overlay map resolves imx500 to its BCM2712 variant
    # on both supported boards, Pi 5 and CM5.
    if name == "imx500":
        return ("overlay_map.dtb", "imx500-pi5.dtbo")
    return (name + ".dtbo",)


def recognize_block(lines, profiles):
    if lines == ["camera_auto_detect=1"]:
        return {"id": "auto", "port": "cam1"}
    if len(lines) == 2 and lines[0] == "camera_auto_detect=0" and lines[1].startswith("dtoverlay="):
        return overlay_profile(lines[1].removeprefix("dtoverlay="), profiles)
    return None


def inspect_config(root, content, profiles):
    """Adopt plain manual definitions when they all describe the same connector."""
    names = {spec.split(",")[0] for _, spec in profiles.values() if spec}
    issues = []
    remove = set()
    manual = []
    auto = None
    managed = None
    visited = set()

    def scan(text, filename, top):
        nonlocal managed, auto
        section = "[all]"
        block = None
        for index, original in enumerate(text.splitlines()):
            stripped = original.strip()
            line = stripped.split("#", 1)[0].strip()
            where = f"{filename}:{index + 1}"
            if top and stripped == BEGIN:
                if managed or block is not None or section != "[all]":
                    issues.append(f"{where}: conflicting managed camera block.")
                block = []
                remove.add(index)
                continue
            if top and stripped == END:
                if block is None:
                    issues.append(f"{where}: unexpected end of managed camera block.")
                    continue
                parsed = recognize_block(block, profiles)
                if parsed:
                    managed = parsed
                else:
                    issues.append(f"{where}: unrecognized managed camera block contents.")
                block = None
                remove.add(index)
                continue
            if block is not None:
                remove.add(index)
                block.append(stripped)
                continue
            if not line:
                continue
            if line.startswith("["):
                section = line.lower()
                continue
            if section == "[none]":
                continue
            include = re.fullmatch(r"include\s+(.+)", line, flags=re.I)
            if include:
                path = Path(include[1].strip().lstrip("/"))
                if not path.parts or ".." in path.parts:
                    issues.append(f"{where}: unsupported firmware include path.")
                    continue
                if path in visited:
                    continue
                visited.add(path)
                if len(visited) > 32:
                    issues.append(f"{where}: firmware include limit exceeded.")
                    continue
                target = root / path
                try:
                    if target.resolve() != target:
                        raise ValueError("non-canonical include")
                    included = read_file(target)
                except FileNotFoundError:
                    # An optional include can be absent on the installed firmware volume.
                    continue
                except (OSError, ValueError):
                    issues.append(f"{where}: cannot inspect firmware include {path}.")
                else:
                    scan(included, str(path), False)
                continue
            key, _, value = line.partition("=")
            key, value = key.strip().lower(), value.strip()
            if ((key == "os_prefix" and value)
                    or (key == "overlay_prefix" and value != "overlays/")):
                issues.append(f"{where}: a custom firmware prefix requires manual overlay selection.")
            if key == "camera_auto_detect":
                if managed and value != ("1" if managed["id"] == "auto" else "0"):
                    issues.append(f"{where}: a later camera_auto_detect setting overrides the managed profile.")
                if top and section == "[all]":
                    auto = value == "1"
                    remove.add(index)
                else:
                    auto = None
            overlay_name = Path(value.split(",")[0]).stem
            if key in ("dtoverlay", "device_tree_overlay") and (overlay_name in names or CAMERA_OVERLAY.match(overlay_name)):
                profile = overlay_profile(value, profiles)
                if top and section == "[all]" and profile:
                    manual.append(profile)
                    remove.add(index)
                else:
                    issues.append(f"{where}: resolve camera overlay '{value}' before changing profiles.")
        if block is not None:
            issues.append(f"{filename}: unterminated managed camera block.")

    scan(content, "config.txt", True)
    ports = {item["port"] for item in manual}
    if len(ports) > 1 or (managed and manual):
        issues.append("Multiple camera definitions need individual port management.")
    selection_port = manual[0]["port"] if len(manual) > 1 and not issues else None
    inferred = manual[0] if len(manual) == 1 else None
    if not manual and auto:
        inferred = {"id": "auto", "port": "cam1"}
    return managed or inferred, bool(managed), remove, issues, selection_port


def render(content, remove, name, port, profiles):
    lines = []
    for n, line in enumerate(content.splitlines(keepends=True)):
        if n not in remove:
            lines.append(line)
        elif line.partition("#")[0].strip() and "#" in line:
            # Retain an operator's trailing comment when moving its directive.
            lines.append(line[line.index("#"):])
    newline = "\r\n" if "\r\n" in content else "\n"
    base = "".join(lines)
    if base and not base.endswith(("\n", "\r")):
        base += newline
    last = next((line.split("#", 1)[0].strip().lower() for line in reversed(lines)
                 if line.split("#", 1)[0].strip()), "")
    if last != "[all]":
        base += "[all]" + newline
    return base + newline.join([BEGIN, *directives(name, port, profiles), END, ""])


class BootConfig:
    def __init__(self, root, state, current_boot, reboot, catalog=CATALOG):
        self.root = Path(root).resolve()
        self.path = self.root / "config.txt"
        self.state = Path(state)
        self.current_boot = current_boot
        self.reboot = reboot
        self.catalog = catalog

    def history(self):
        try:
            return json.loads((self.state / "transaction.json").read_text())
        except FileNotFoundError:
            return {}

    def read_catalog(self):
        try:
            raw = read_file(self.catalog, root_owned=self.catalog == CATALOG)
        except FileNotFoundError:
            return PROFILES, "", None
        except (OSError, ValueError) as exc:
            return PROFILES, "unreadable", f"Sensor profile catalog: {exc}"
        fingerprint = digest(raw)
        try:
            return parse_profiles(raw), fingerprint, None
        except (ValueError, RecursionError) as exc:
            return PROFILES, fingerprint, f"Invalid sensor profile catalog: {exc}"

    def inspect(self, text, catalog):
        profiles, fingerprint, error = catalog
        profile, managed, remove, conflicts, selection_port = inspect_config(self.root, text, profiles)
        if error:
            conflicts.insert(0, error)
        revision = digest(text)
        stage = self.history().get(revision, {})
        current_boot = self.current_boot()
        result = {
            "available": True, "simulated": self.root != FIRMWARE,
            "path": str(FIRMWARE / "config.txt"), "config_text": text,
            "revision": digest(revision + fingerprint) if fingerprint else revision,
            "profiles": [{"id": name, "label": label,
                          "directives": {port: "\n".join(directives(name, port, profiles)) for port in PORTS},
                          "installed": spec is None or all((self.root / "overlays" / filename).is_file()
                                                           for filename in overlay_files(spec))}
                         for name, (label, spec) in profiles.items()],
            "profile": profile, "managed": managed, "conflicts": conflicts,
            "selection_required_port": selection_port if not conflicts else None,
            "pending_reboot": stage.get("boot_id") == current_boot and stage["boot_revision"] != revision,
            "backup_available": "before" in stage,
            "message": "Restart to load a staged profile. The detected camera is reported separately.",
        }
        return result, remove, stage

    def handle(self, action, body):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state / "lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            content = read_file(self.path)
            catalog = self.read_catalog()
            profiles = catalog[0]
            status, remove, previous = self.inspect(content, catalog)
            if action == "status" and body == {}:
                return status
            revision = digest(content)
            if not isinstance(body, dict) or body.get("revision") != status["revision"]:
                raise FirmwareConflict("The firmware configuration changed. Refresh it and review the settings.")
            if action == "reboot":
                if body.keys() != {"confirm", "revision"} or body["confirm"] is not True:
                    raise ValueError("Explicit reboot confirmation is required.")
                self.reboot()
                return {"scheduled": True, "simulated": self.root != FIRMWARE}
            if action != "apply" or body.keys() != {"profile", "port", "revision"}:
                raise ValueError("Expected a sensor profile, CSI port and configuration revision.")
            name, port = body["profile"], body["port"]
            if not isinstance(name, str) or port not in PORTS:
                raise ValueError("Select a supported sensor profile and CSI port.")
            if name == "restore":
                if "before" not in previous:
                    raise ValueError("No matching previous configuration is available to restore.")
                updated = previous["before"]
            else:
                if status["conflicts"]:
                    raise FirmwareConflict(status["conflicts"][0])
                if name not in profiles:
                    raise ValueError("Unknown sensor profile.")
                if not next(p["installed"] for p in status["profiles"] if p["id"] == name):
                    raise ValueError("The selected sensor overlay is missing from the firmware overlays directory.")
                updated = render(content, remove, name, port, profiles)
            if updated == content:
                return status
            if len(updated.encode()) > MAX_CONFIG:
                raise ValueError("The new firmware configuration exceeds 64 KiB.")
            current_boot = self.current_boot()
            original = previous.get("boot_revision") if previous.get("boot_id") == current_boot else revision
            entry = {"boot_id": current_boot, "boot_revision": original}
            if name != "restore":
                entry["before"] = content
            # Select the journal entry by the actual file digest. Both versions remain
            # recognizable across interruption between the journal and FAT renames.
            journal = {revision: previous, digest(updated): entry}
            if read_file(self.path) != content or self.read_catalog()[1] != catalog[1]:
                raise FirmwareConflict("The firmware configuration changed during editing. Refresh and retry.")
            if name != "restore":
                atomic_text(self.root / BACKUP, content, stat.S_IMODE(self.path.stat().st_mode))
            atomic_text(self.state / "transaction.json", json.dumps(journal), 0o600)
            atomic_text(self.path, updated, stat.S_IMODE(self.path.stat().st_mode))
            return self.inspect(updated, catalog)[0]


class FirmwareConflict(RuntimeError):
    """The reviewed firmware state can no longer be applied."""


class Firmware:
    """The web service can request only the three defined helper operations."""

    def __init__(self, simulation_dir=None):
        self.simulator = None
        if simulation_dir is not None:
            directory = Path(simulation_dir).resolve()
            root = directory / "boot"
            root.mkdir(parents=True, exist_ok=True)
            overlays = root / "overlays"
            overlays.mkdir(exist_ok=True)
            for _, spec in PROFILES.values():
                if spec:
                    for filename in overlay_files(spec):
                        (overlays / filename).touch(exist_ok=True)
            if not (root / "config.txt").exists():
                (root / "config.txt").write_text("# Simulated firmware\n[all]\ncamera_auto_detect=1\n")
            marker = directory / "boot-id"
            if not marker.exists():
                marker.write_text(uuid.uuid4().hex)
            self.simulator = BootConfig(root, directory / "state", lambda: marker.read_text().strip(),
                                        lambda: atomic_text(marker, uuid.uuid4().hex, 0o600),
                                        directory / "sensor-profiles.json")

    def _call(self, action, body):
        if self.simulator is not None:
            return self.simulator.handle(action, body)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(12)
                client.connect(SOCKET)
                client.sendall(json.dumps({"action": action, "body": body}).encode() + b"\n")
                with client.makefile("rb") as stream:
                    raw = stream.readline(MAX_REPLY + 1)
                if len(raw) > MAX_REPLY or not raw.endswith(b"\n"):
                    raise ValueError("Incomplete firmware helper response.")
                reply = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise RuntimeError("The firmware helper is unavailable.") from exc
        if "result" in reply:
            return reply["result"]
        errors = {"conflict": FirmwareConflict, "invalid": ValueError}
        raise errors.get(reply.get("kind"), RuntimeError)(reply.get("error", "Firmware operation failed."))

    def status(self):
        try:
            return self._call("status", {})
        except (OSError, RuntimeError, ValueError) as exc:
            return {"available": False, "simulated": self.simulator is not None, "path": str(FIRMWARE / "config.txt"),
                    "revision": None, "config_text": "", "profiles": [], "profile": None, "managed": False,
                    "selection_required_port": None,
                    "pending_reboot": False, "backup_available": False, "conflicts": [], "message": str(exc)}

    def apply(self, body):
        return self._call("apply", body)

    def reboot(self, body):
        return self._call("reboot", body)


def verify_target(root=FIRMWARE):
    model = Path("/proc/device-tree/model").read_text().rstrip("\0\n")
    if not model.startswith(("Raspberry Pi 5 ", "Raspberry Pi Compute Module 5")):
        raise RuntimeError("Firmware profiles require a Raspberry Pi 5 or Compute Module 5.")
    # ReadWritePaths in the helper unit can create a bind mount. Compare device
    # IDs to distinguish the real Raspberry Pi OS boot partition from its empty
    # underlying directory even inside that private mount namespace.
    if root.stat().st_dev == root.parent.stat().st_dev:
        raise RuntimeError("Mount the boot partition at /boot/firmware before inspecting or staging a profile.")


def request_reboot():
    result = subprocess.run(["/usr/bin/systemctl", "--no-block", "reboot"], capture_output=True, timeout=8)
    if result.returncode:
        raise RuntimeError("systemd declined the reboot request.")


def helper_main():
    try:
        raw = sys.stdin.buffer.readline(2049)
        if len(raw) > 2048 or not raw.endswith(b"\n"):
            raise ValueError("Invalid firmware request size.")
        request = json.loads(raw)
        if not isinstance(request, dict) or request.keys() != {"action", "body"}:
            raise ValueError("Invalid firmware request.")
        if request["action"] not in ("status", "apply", "reboot"):
            raise ValueError("Unknown firmware operation.")
        verify_target()
        editor = BootConfig(FIRMWARE, STATE, boot_id, request_reboot)
        reply = {"result": editor.handle(request["action"], request["body"])}
    except FirmwareConflict as exc:
        reply = {"kind": "conflict", "error": str(exc)}
    except (ValueError, RecursionError) as exc:
        reply = {"kind": "invalid", "error": str(exc)}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        reply = {"kind": "unavailable", "error": str(exc)}
    print(json.dumps(reply), flush=True)
