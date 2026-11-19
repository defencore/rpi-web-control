"""Exercise the Pi-side installation decisions in a disposable staging directory."""

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path


HELPER = Path(__file__).resolve().parents[1] / "tools" / "install-package.sh"
VERSION = "1.2-1"


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.stage = Path(temp.name).resolve()
        self.bin = self.stage / "bin"
        self.bin.mkdir()
        self.archive = self.stage / f"rpi-web-control_{VERSION}_arm64.deb"
        self.archive.write_bytes(b"test package")
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.state = self.stage / "installed"
        self.apt_args = self.stage / "apt-args"

        self.command("sha256sum", 'IFS= read -r line && [ "$line" = "$DIGEST  $ARCHIVE" ]')
        self.command("dpkg-deb", '''
            [ "$1" = -f ] && [ "$2" = "$ARCHIVE" ] || exit 1
            case "$3" in
                Package) echo "$PACKAGE" ;;
                Architecture) echo "$ARCH" ;;
                Version) echo "$DEB_VERSION" ;;
                *) exit 1 ;;
            esac''')
        self.command("dpkg-query", 'IFS= read -r state < installed && echo "$state"')
        self.command("dpkg", '[ "$*" = "--compare-versions 2.0-1 gt $VERSION" ]')
        self.command("systemctl", 'echo active')
        self.command("chmod", 'exit 0')
        self.command("id", 'echo 1000')
        self.command("sudo", '[ "$1 $2" = "-n --" ] || exit 1\nshift 2\nexec "$@"')
        self.command("apt-get", '''
            printf '%s\n' "$@" > apt-args
            [ "$APT_EXIT" = 0 ] || exit "$APT_EXIT"
            echo "installed $VERSION arm64" > installed''')

    def command(self, name, script):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)

    def install(self, digest=None, **overrides):
        env = {"PATH": str(self.bin), "ARCHIVE": str(self.archive), "DIGEST": self.digest,
               "PACKAGE": "rpi-web-control", "ARCH": "arm64", "DEB_VERSION": VERSION,
               "VERSION": VERSION, "APT_EXIT": "0", **overrides}
        return subprocess.run(["/bin/sh", HELPER, VERSION, digest or self.digest],
                              cwd=self.stage, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=5)

    def test_refuses_invalid_archive_before_install(self):
        for invalid in ({"digest": "0" * 64}, {"PACKAGE": "other"}, {"ARCH": "armhf"},
                        {"DEB_VERSION": "1.1-1"}):
            with self.subTest(invalid=invalid):
                self.assertNotEqual(self.install(**invalid).returncode, 0)
                self.assertFalse(self.apt_args.exists())

    def test_installed_version(self):
        for version, expected in (("2.0-1", 1), (VERSION, 0)):
            with self.subTest(version=version):
                self.state.write_text(f"installed {version} arm64\n", encoding="ascii")
                self.assertEqual(self.install().returncode, expected)
                self.assertFalse(self.apt_args.exists())

    def test_install_and_apt_failure(self):
        result = self.install(APT_EXIT="42")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertFalse(self.state.exists())
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install", self.apt_args.read_text().splitlines())
        self.assertEqual(self.apt_args.read_text().splitlines()[-1], str(self.archive))
        self.assertEqual(self.state.read_text().strip(), f"installed {VERSION} arm64")
