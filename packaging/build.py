"""Build a root-owned arm64 Debian archive with the Python standard library."""

import argparse
import gzip
import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
CONTROL = (PACKAGING / "control").read_text(encoding="ascii")
FIELDS = Parser().parsestr(CONTROL)
NAME = FIELDS["Package"]
HELPERS = ("network", "firmware", "nvme", "clock")
DIST = ROOT / "dist" / f"{NAME}_{FIELDS['Version']}_{FIELDS['Architecture']}.deb"
MEMBERS = ("debian-binary", "control.tar.gz", "data.tar.gz")


def package_files():
    """Return the complete map of installed regular files and their modes."""
    base = f"usr/lib/{NAME}"
    data = {
        f"etc/default/{NAME}": (PACKAGING / "defaults", 0o644),
        f"{base}/tools/check-camera-modes.py": (ROOT / "tools/check-camera-modes.py", 0o644),
        f"{base}/tools/render_recording.py": (ROOT / "tools/render_recording.py", 0o644),
        f"usr/lib/systemd/system/{NAME}.service": (PACKAGING / f"{NAME}.service", 0o644),
        f"usr/share/doc/{NAME}/README.md": (ROOT / "README.md", 0o644),
    }
    for helper in HELPERS:
        directory = PACKAGING / helper
        script = f"{helper}-helper.py"
        data[f"{base}/{script}"] = (directory / script, 0o644)
        for suffix in (".socket", "@.service"):
            unit = f"{NAME}-{helper}{suffix}"
            data[f"usr/lib/systemd/system/{unit}"] = (directory / unit, 0o644)
    data[f"{base}/recovery-helper.py"] = (PACKAGING / "network/recovery-helper.py", 0o644)
    for suffix in (".service", ".timer"):
        unit = f"{NAME}-recovery{suffix}"
        data[f"usr/lib/systemd/system/{unit}"] = (PACKAGING / "network" / unit, 0o644)
    docs = [ROOT / "AGENTS.md", ROOT / "tools/render_recording.py", ROOT / "tools/inspect-board.sh",
            ROOT / "tools/check-camera-modes.py", *(ROOT / "docs").glob("*.md"),
            *(ROOT / "docs/hardware-results").glob("*.md"),
            ROOT / "docs/assets/gpio-header-40pin.svg"]
    docs.extend(image for image in (ROOT / "docs/assets/screenshots").glob("*.png")
                if image.is_file() and not image.is_symlink() and not image.name.startswith("."))
    docs.extend(file for file in (ROOT / "skills").rglob("*") if file.suffix in {".md", ".py"})
    for doc in docs:
        data[f"usr/share/doc/{NAME}/{doc.relative_to(ROOT)}"] = (doc, 0o644)
    runtime = ROOT / "rpi_web_control"
    for file in runtime.glob("*.py"):
        if file.is_file() and not file.is_symlink() and not file.name.startswith("."):
            data[f"{base}/{file.relative_to(ROOT)}"] = (file, 0o644)
    for name in ("index.html", "style.css", "app.js", "device.js", "ui.js", "storage.js",
                 "network.js", "wifi_station.js"):
        file = runtime / "static" / name
        if not file.is_file() or file.is_symlink():
            raise RuntimeError(f"Missing static asset: {name}")
        data[f"{base}/{file.relative_to(ROOT)}"] = (file, 0o644)
    for file in ("__main__.py", "camera.py", "triggers.py", "network.py", "wifi_scan.py",
                 "network_profiles.py", "network_transaction.py",
                 "network_sim.py", "network_recovery.py", "recovery_arp.py", "recovery_config.py",
                 "live.py", "firmware.py", "sensor_profiles.py", "storage.py",
                 "nvme.py", "nvme_client.py"):
        if f"{base}/rpi_web_control/{file}" not in data:
            raise RuntimeError(f"Missing runtime file: {file}")
    return data


def compressed_tar(files, epoch):
    """Create a reproducible GNU tar with explicit directory ownership and modes."""
    directories = {parent.as_posix() for name in files for parent in Path(name).parents}
    result = io.BytesIO()
    with gzip.GzipFile(fileobj=result, mode="wb", mtime=epoch, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for name in sorted(directories | files.keys()):
                info = tarfile.TarInfo("." if name == "." else f"./{name}")
                info.uname = info.gname = "root"
                info.mtime = epoch
                if name in directories:
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    archive.addfile(info)
                else:
                    content, info.mode = files[name]
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
    return result.getvalue()


def write_member(stream, name, data, epoch):
    # deb(5) uses short, fixed-width ASCII headers and even byte boundaries.
    header = f"{name:<16}{epoch:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
    stream.write(header.encode("ascii"))
    stream.write(data)
    if len(data) % 2:
        stream.write(b"\n")


def build(output, epoch):
    payload = {name: (src.read_bytes(), mode) for name, (src, mode) in package_files().items()}
    sums = "".join(
        f"{hashlib.md5(content, usedforsecurity=False).hexdigest()}  {name}\n"
        for name, (content, _) in sorted(payload.items())
    )
    installed_kib = sum((len(content) + 1023) // 1024 for content, _ in payload.values())
    control = {
        "control": (f"{CONTROL}Installed-Size: {installed_kib}\n".encode(), 0o644),
        "conffiles": (f"/etc/default/{NAME}\n".encode(), 0o644),
        "md5sums": (sums.encode(), 0o644),
    }
    for script in ("postinst", "prerm", "postrm"):
        control[script] = ((PACKAGING / script).read_bytes(), 0o755)
    members = (b"2.0\n", compressed_tar(control, epoch), compressed_tar(payload, epoch))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        candidate = Path(tmp) / output.name
        with candidate.open("wb") as stream:
            stream.write(b"!<arch>\n")
            for name, content in zip(MEMBERS, members):
                write_member(stream, name, content, epoch)
        candidate.chmod(0o644)
        verify(candidate)
        candidate.replace(output)


def verify(archive):
    """Use the platform ar reader and dpkg-deb, when installed, as independent readers."""
    def member(name):
        return subprocess.check_output(["ar", "-p", archive, name])

    names = subprocess.check_output(["ar", "-t", archive], text=True).splitlines()
    if tuple(n.rstrip("/") for n in names) != MEMBERS or member("debian-binary") != b"2.0\n":
        raise ValueError("Invalid Debian archive members")
    with tarfile.open(fileobj=io.BytesIO(member("control.tar.gz"))) as control:
        expected = control.extractfile("./md5sums").read().decode().splitlines()
        for script in ("postinst", "prerm", "postrm"):
            entry = control.getmember("./" + script)
            if entry.mode != 0o755:
                raise ValueError(f"Invalid script mode: {script}")
            subprocess.run(["sh", "-n"], input=control.extractfile(entry).read(), check=True)
    with tarfile.open(fileobj=io.BytesIO(member("data.tar.gz"))) as data:
        actual = []
        for entry in data:
            if entry.uid or entry.gid or entry.mode & 0o022:
                raise ValueError(f"Unexpected permissions: {entry.name}")
            if entry.isfile():
                digest = hashlib.md5(data.extractfile(entry).read(), usedforsecurity=False).hexdigest()
                actual.append(f"{digest}  {entry.name.removeprefix('./')}")
        if sorted(expected) != sorted(actual):
            raise ValueError("Payload checksum mismatch")
    if shutil.which("dpkg-deb"):
        for flag in ("--info", "--contents"):
            subprocess.run(["dpkg-deb", flag, archive], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DIST)
    parser.add_argument("--verify", action="store_true", help="check an existing package")
    args = parser.parse_args()
    if args.verify:
        verify(args.output)
    else:
        build(args.output, int(os.getenv("SOURCE_DATE_EPOCH", "0")))
    print(args.output)
