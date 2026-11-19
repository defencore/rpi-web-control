# Debian package

The package targets Raspberry Pi OS 64-bit Bookworm or newer on Raspberry Pi 5 and Compute Module 5. The target needs the Raspberry Pi OS repositories for the matched Picamera2 and libcamera packages. Network configuration uses NetworkManager and python3-dbus. Shared IPv4 connections use dnsmasq-base to serve DHCP and forward DNS.

## Build and inspect

From the project root, run `make deb` and `make verify-deb`. The builder needs Python 3.11 or newer, Make, a POSIX shell and the standard `ar` tool. It writes `dist/rpi-web-control_0.11.0-1_arm64.deb`. The same sources, toolchain and `SOURCE_DATE_EPOCH` produce the same bytes. The default archive timestamp is zero. The builder verifies the control scripts, member order, payload hashes, file modes and ownership. Where installed, it also runs `dpkg-deb --info` and `dpkg-deb --contents`.

Download the desktop export utility from **Recordings → Convert to MP4**. It is also included in `/usr/share/doc/rpi-web-control/tools/render_recording.py` and in the project checkout. On the workstation it accepts a downloaded session ZIP or a completed recording directory. See the [desktop export guide](post-processing.md) for requirements and commands.

## Deploy from a workstation

Stop any active recording before upgrading. In a terminal at the root of the checkout, run the installer with the board's SSH account and address.

```sh
./install.sh ubuntu@192.168.55.100
```

The script builds the release named in `packaging/control` if its arm64 archive is missing from `dist/`. It inventories the target and requires a Pi 5 or CM5 with arm64 Debian userspace. It selects the highest Debian version among local `dist/rpi-web-control_*_arm64.deb` archives using the Pi's `dpkg --compare-versions`. It verifies the archive locally, transfers it over SSH, then checks its SHA-256, package name, architecture and version on the Pi.

The script reuses one SSH connection and honors normal host key verification and SSH authentication. APT runs as root and keeps locally edited package configuration files. For a non-root login, run the installer from a terminal so sudo can prompt on the Pi. An unattended run needs working SSH authentication and noninteractive sudo access to the install command.

The installer verifies the installed version and service state. A fresh install starts the service, and an upgrade restarts a running service. A previously installed, intentionally stopped service stays stopped. If the selected version is already installed, the installer checks its state and skips the APT step. If the installed version is newer, it reports the version mismatch and exits before APT runs. For manual installation, copy the archive to the Pi and run `sudo apt install ./rpi-web-control_0.11.0-1_arm64.deb` there.

### Collect diagnostics

Run the following before installation or whenever you need a fresh report.

```sh
./install.sh --diagnose ubuntu@192.168.55.100
```

Each invocation prints a new private directory under `dist/install-reports/` on the workstation. The files reflect the steps reached during that run.

| File | Contents |
| --- | --- |
| `inventory.txt` | Install preflight with UTC time, board model, Debian architecture, kernel and OS, camera packages and controller state, storage and boot mount, network interfaces, Wi-Fi hardware and software switch states, temperature and throttling. Camera enumeration is skipped while the controller is running. |
| `build.log` | Local `make deb` output when an install needs to build the checkout release. |
| `install.log` | Selected archive and target, local archive verification, transfer output, checksum, APT output when invoked, installed version and service check. |
| `diagnostics.txt` | Inventory plus installed package status and declared dependencies, `dpkg --audit`, simulated APT dependency check, filesystem space, managed storage mount options, recent kernel drive and filesystem errors, IPv4 routes and DNS, bridge ports, Wi-Fi device state and reason, AP capability, active Wi-Fi mode, age of the last completed scan, cached access-point count, configured WLAN country, installed NetworkManager, Python D-Bus and dnsmasq-base versions, time zone and NTP status, controller and helper socket states, loaded helper instances and recent journal entries. Written by `--diagnose`. An install also attempts collection if the target fails the compatibility check or a later installation step fails. |

Diagnostics run as the SSH account. For the current boot the report requests up to 80 controller journal entries, 20 per helper (network, firmware, NVMe and clock), 20 from the wired recovery service and timer together, and 30 camera-related kernel entries. The report also includes the last observed recovery address, interface, status and explanation, along with the systemd unit states. Journal permissions can limit the entries returned. Missing utilities and inaccessible logs are reported alongside the sections that could be collected. An unrecognized board or architecture returns a nonzero status even when a diagnostic report was saved.

The installed inventory tool is also available on the Pi at `/usr/share/doc/rpi-web-control/tools/inspect-board.sh`. See the [hardware validation guide](hardware-validation.md) for board-specific checks.

## Access and credentials

Open `https://<hostname>.local:8080` where multicast DNS is available. The console opens directly by default. Anyone who can reach the service can control the camera, download recordings and use device administration controls. To restrict these actions, enable token sign-in as described below.

Verify the certificate on your first connection. On the board or through trusted SSH, run the following command and compare the SHA-256 fingerprint with the browser's certificate details. The generated certificate is self-signed, expires after 825 days, and covers the short hostname, its `.local` name, localhost and 127.0.0.1. To connect by LAN address with normal browser trust, use a trusted certificate that includes that address.

```sh
sudo openssl x509 -in /etc/rpi-web-control/tls.crt -noout -fingerprint -sha256
```

To deploy a certificate issued by your own CA, set `RPI_CONTROL_TLS_CERT` and `RPI_CONTROL_TLS_KEY` in `/etc/default/rpi-web-control`. Grant the service group read access to the key and traversal of its parent directories. Restart the service after a credential change or certificate renewal.

### Set the Pi clock offline

Open the console from a computer or phone with the correct time. It compares both clocks in Unix epoch milliseconds, so different displayed time zones work normally. On each status refresh the console checks for a difference of more than five seconds, allowing for the time taken by the request. The warning displays both times in the browser's time zone.

To correct the Pi, stop recording and release any trigger claims. Selecting **Set Pi time** confirms the correction and immediately sets the Pi's clock from the browser. Choose **Not now** to dismiss the warning until you reload the page or sign in again. The Pi keeps its configured time zone and NTP settings. If NTP is enabled it can adjust the system clock again when a time source is reachable.

This action follows the console's access setting. With direct access, anyone who can reach the console can set the clock. With token sign-in enabled, it requires an authenticated request. The server applies the same origin checks as for other control requests.

### Enable token sign-in

1. Set `RPI_CONTROL_REQUIRE_TOKEN=1` in `/etc/default/rpi-web-control`.
2. Stop any active recording and run `sudo systemctl restart rpi-web-control`.
3. Read the device token with `sudo cat /etc/rpi-web-control/token` in a trusted terminal. Open the console and enter the token when prompted.

To restore direct access, set the value to `0` and restart the service after recording has stopped. Upgrades preserve an explicitly enabled token requirement. An installation upgraded from a release without this setting uses direct access.

### Rotate the access token

After stopping any active recording, run the following on the Pi through trusted SSH. This replaces the token at the package's default path and restarts the service to expire existing sessions. If you configured a different token path, use its directory and filename below.

```sh
sudo sh -e <<'ROTATE'
cd /etc/rpi-web-control
tmp=$(mktemp .token.XXXXXXXX)
trap 'rm -f "$tmp"' EXIT
openssl rand -hex 32 > "$tmp"
chown root:rpi-web-control "$tmp"
chmod 0640 "$tmp"
mv -f "$tmp" token
systemctl restart rpi-web-control.service
ROTATE
```

Read the new token in your private SSH terminal and sign in again.

## Installed paths

| Path | Purpose |
| --- | --- |
| `/etc/default/rpi-web-control` | Listener address, port, TLS files, token requirement and file, state and recording roots |
| `/etc/rpi-web-control/` | Generated access token, initial certificate and private key |
| `/var/lib/rpi-web-control/` | Persistent application settings and default recording directory |
| `/usr/lib/rpi-web-control/` | Application code, local web assets and privileged helpers for networking, firmware, external disks and system time |
| `/usr/share/doc/rpi-web-control/` | Operator and desktop-export documentation |
| `/run/rpi-web-control-clock.sock` | Local group-restricted system clock helper socket |
| `/run/rpi-web-control-network.sock` | Local group-restricted NetworkManager helper socket |
| `/run/rpi-web-control-recovery/status.json` | Current status of the independent wired recovery service |
| `/run/rpi-web-control-nvme.sock` | Local group-restricted socket for PCIe NVMe and USB drive management |
| `/srv/rpi-web-control-nvme/recordings` | Recording directory on the managed ext4 SSD |
| `/srv/rpi-web-control-usb/recordings` | Recording directory on the managed ext4 USB drive |
| `/run/rpi-web-control-firmware.sock` | Local group-restricted firmware helper socket |
| `/var/lib/rpi-web-control-firmware/` | Root-owned firmware recovery and operation state |

The web service runs as the system account `rpi-web-control`. The installer adds it to the existing video, render, gpio and dialout groups for device access. Hardware availability still depends on carrier routing and the target OS device permissions. Package upgrades preserve settings, credentials and recordings. Removing the package retains data. Purging it also removes the generated credentials. The system account, recorded sessions, firmware settings and firmware recovery data survive purge. The [firmware guide](firmware.md) describes profile changes, reboot and rollback.

## Network setup

After installation, open **Network settings → Join an existing Wi-Fi network** to scan for a router and connect the Pi while retaining the wired management address. Open **Network settings → Host a Wi-Fi hotspot** to host a network for a phone or laptop. The Pi can serve the console and assign client addresses offline. [Join Wi-Fi](network.md#join-an-existing-wi-fi-network) · [Access point setup](network.md#create-a-wi-fi-access-point)

For recovery without DHCP, connect the workstation directly by Ethernet, set it to `192.168.55.10/24`, and open `https://192.168.55.100:8080`. The recovery timer starts automatically on installation and at boot. You can reserve another address or disable it in `/etc/default/rpi-web-control`. [Network and recovery guide](network.md#connect-by-ethernet-when-the-main-address-is-unknown)

## External storage

In **Recording drives**, choose **Use USB** or **Use NVMe** for a ready drive, then check the active directory. For a new drive, expand **Set up a USB drive** or **Set up a PCIe NVMe drive** to mount an existing ext4 volume or explicitly erase and prepare the disk. A managed volume uses its filesystem UUID for mounting across reboots. Its recordings and its entry in `/etc/fstab` survive package removal and purge. [Recording destinations, downloads and cleanup](storage.md)

Mount the destination at a stable path, such as `/srv/camera-ssd`. Create a directory on that mounted filesystem writable by the service account. Add its absolute path to `RPI_CONTROL_STORAGE_ROOTS` in the defaults file along with any other allowed roots, separated by colons. Set `RPI_CONTROL_REQUIRED_MOUNTS=/srv/camera-ssd` for this volume, specifying its actual mountpoint. Use colons for additional mountpoints. The controller refuses recordings beneath a required volume when it is absent. Restart the service and select the desired directory in the web interface. The service's filesystem protections make home directories read-only.

If the device must wait for that drive on every boot, configure its mount in `/etc/fstab` and create a service override with `sudo systemctl edit rpi-web-control.service`:

```ini
[Unit]
RequiresMountsFor=/srv/camera-ssd
AssertPathIsMountPoint=/srv/camera-ssd
```

Restart the service to apply the override. The [storage guide](storage.md) covers device discovery, volume identity and reconnecting removable media.
