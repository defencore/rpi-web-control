#!/bin/sh
# Run on the Pi, or stream this file to an existing SSH connection with sh -s.
export LC_ALL=C
case "$#:${1:-}" in
    0:) diagnostics=no ;;
    1:--diagnostics) diagnostics=yes ;;
    *) printf 'Usage: sh %s [--diagnostics]\n' "$0" >&2; exit 2 ;;
esac
printf 'Raspberry Pi camera controller inventory\nUTC '
date -u '+%Y-%m-%dT%H:%M:%SZ'

model_file=/proc/device-tree/model
if [ ! -r "$model_file" ]; then
    printf 'Board model unavailable in %s. Run this inventory on the Raspberry Pi.\n' "$model_file"
    [ "$diagnostics" = yes ] || exit 2
    model=unknown
else
    model=$(tr -d '\000' < "$model_file")
fi

inspect() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf '%s is unavailable.\n' "$1"
        return
    fi
    if command -v timeout >/dev/null 2>&1; then
        timeout 20 "$@" 2>&1 || printf '%s did not complete successfully.\n' "$1"
    else
        "$@" 2>&1 || printf '%s did not complete successfully.\n' "$1"
    fi
}

arch=$(dpkg --print-architecture 2>/dev/null) || arch=unknown
printf '\nBoard: %s\nDebian architecture: %s\n' "$model" "$arch"
uname -srm
if [ -r /etc/os-release ]; then
    sed -n '/^PRETTY_NAME=/p; /^VERSION_CODENAME=/p' /etc/os-release
fi

printf '\nInstalled software\n'
for package in rpi-web-control python3-picamera2 rpicam-apps rpicam-apps-lite; do
    version=$(dpkg-query -W -f='${db:Status-Abbrev} ${Version}' "$package" 2>/dev/null) || version=
    case "$version" in
        ii*) printf '%s %s\n' "$package" "$version" ;;
        *) printf '%s is not installed.\n' "$package" ;;
    esac
done

printf '\nController and camera\n'
service_state=$(systemctl is-active rpi-web-control.service 2>/dev/null) || service_state=${service_state:-unknown}
printf 'rpi-web-control.service: %s\n' "$service_state"
case "$service_state" in
    active|activating|reloading|deactivating)
        printf 'Read the detected sensor and supported FPS on the Camera page.\n' ;;
    inactive|failed)
        inspect rpicam-hello --list-cameras ;;
    *)
        if [ "$diagnostics" = yes ]; then
            printf 'Controller state is unknown. Skipping camera enumeration.\n'
        else
            inspect rpicam-hello --list-cameras
        fi ;;
esac

printf '\nStorage and firmware\n'
inspect lsblk -o NAME,TYPE,TRAN,MODEL,SIZE,FSTYPE,MOUNTPOINTS
if command -v findmnt >/dev/null 2>&1; then
    findmnt -n -o TARGET,SOURCE,FSTYPE --mountpoint /boot/firmware ||
        printf '/boot/firmware has no separate mount.\n'
fi
if [ -r /proc/mtd ]; then
    cat /proc/mtd
fi

printf '\nNetwork interfaces\n'
inspect ip -brief address
inspect nmcli -t -f DEVICE,TYPE,STATE device status
inspect nmcli -f WIFI-HW,WIFI radio

printf '\nTemperature and throttling\n'
inspect vcgencmd measure_temp
inspect vcgencmd get_throttled

if [ "$diagnostics" = yes ]; then
    printf '\nController package and dependency health\n'
    inspect dpkg-query -W '-f=${binary:Package} ${Version} ${Status}\nDepends: ${Depends}\nRecommends: ${Recommends}\n' rpi-web-control
    inspect dpkg --audit rpi-web-control
    inspect apt-get -s check

    printf '\nFilesystem space\n'
    set -- /
    for dir in /var/lib/rpi-web-control /boot/firmware /srv/rpi-web-control-nvme /srv/rpi-web-control-usb; do
        if [ -d "$dir" ]; then set -- "$@" "$dir"; fi
    done
    inspect df -hP "$@"

    printf '\nRecording drive faults (current boot, visible to this account)\n'
    if command -v findmnt >/dev/null 2>&1; then
        for dir in /srv/rpi-web-control-nvme /srv/rpi-web-control-usb; do
            findmnt -rn -o SOURCE,TARGET,OPTIONS --mountpoint "$dir" || printf '%s has no separate mount.\n' "$dir"
        done
    fi
    inspect journalctl --no-pager -k -b -n 12 --grep='(?i)(nvme|pcie|usb).*(controller is down|reset|timeout|disabling device|disconnect)'
    inspect journalctl --no-pager -k -b -n 12 --grep='(?i)(ext4|jbd2|buffer I/O|blk_update_request).*(error|read.only|aborted|lost)'

    printf '\nIPv4 routes, DNS and bridge ports\n'
    inspect ip -4 route show
    inspect nmcli -f GENERAL.DEVICE,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS device show
    inspect bridge link show

    printf '\nWi-Fi access point support\n'
    inspect nmcli -f GENERAL.DEVICE,GENERAL.STATE,GENERAL.REASON,WIFI-PROPERTIES.AP device show
    inspect raspi-config nonint get_wifi_country
    inspect dpkg-query -W network-manager python3-dbus dnsmasq-base

    printf '\nWi-Fi scan state (read-only NetworkManager cache)\n'
    inspect python3 - <<'PY'
import time
import dbus

service = "org.freedesktop.NetworkManager"
bus = dbus.SystemBus()
manager = dbus.Interface(bus.get_object(service, "/org/freedesktop/NetworkManager"), service)
radios = 0
for path in manager.GetDevices():
    properties = dbus.Interface(bus.get_object(service, path), "org.freedesktop.DBus.Properties")
    device = service + ".Device"
    if properties.Get(device, "DeviceType") != 2:
        continue
    radios += 1
    name = properties.Get(device, "Interface")
    try:
        wifi = properties.GetAll(device + ".Wireless")
        mode = {0: "unknown", 1: "ad-hoc", 2: "station", 3: "AP", 4: "mesh"}.get(wifi["Mode"], "other")
        last_scan_ms = int(wifi["LastScan"])
        age = "never" if last_scan_ms < 0 else f"{time.clock_gettime(time.CLOCK_BOOTTIME) - last_scan_ms / 1000:.0f} s ago"
        print(f"{name}: mode={mode}, last scan={age}, cached access points={len(wifi['AccessPoints'])}")
    except (dbus.DBusException, KeyError) as exc:
        print(f"{name}: scan state unavailable ({exc})")
if not radios:
    print("NetworkManager reports no Wi-Fi radios.")
PY

    printf '\nWired recovery service and timer\n'
    if [ -r /run/rpi-web-control-recovery/status.json ]; then
        cat /run/rpi-web-control-recovery/status.json
    fi
    inspect systemctl --no-pager show -p Id -p LoadState -p ActiveState -p SubState -p Result \
        -p ExecMainStatus rpi-web-control-recovery.timer rpi-web-control-recovery.service
    inspect journalctl --no-pager -b -n 20 -u rpi-web-control-recovery.timer -u rpi-web-control-recovery.service

    printf '\nSystem clock settings\n'
    inspect timedatectl show -p Timezone -p NTP -p NTPSynchronized

    printf '\nController service and helper sockets\n'
    inspect systemctl --no-pager show \
        -p Id -p LoadState -p ActiveState -p SubState -p Result -p ExecMainStatus -p NRestarts \
        rpi-web-control.service rpi-web-control-network.socket \
        rpi-web-control-firmware.socket rpi-web-control-nvme.socket rpi-web-control-clock.socket
    inspect systemctl --no-pager --plain list-units --all 'rpi-web-control-*@*.service' | head -n 40

    printf '\nController journal (current boot, last 80 entries visible to this account)\n'
    inspect journalctl --no-pager -b -n 80 -u rpi-web-control.service
    for helper in network firmware nvme clock; do
        printf '\n%s helper journal (current boot, last 20 entries)\n' "$helper"
        inspect journalctl --no-pager -b -n 20 -u "rpi-web-control-$helper.socket" \
            -u "rpi-web-control-$helper@*.service"
    done

    printf '\nCamera kernel messages (current boot, last 30 matching entries)\n'
    inspect journalctl --no-pager -b -k -n 30 --case-sensitive=no \
        --grep='camera|csi[-0-9]|rp1-cfe|pisp|bcm2835-isp|unicam|imx[0-9]|ov[0-9]'
fi

case "$model:$arch" in
    'Raspberry Pi 5 Model B '*:arm64|'Raspberry Pi Compute Module 5 '*:arm64)
        printf '\nBoard family and Debian architecture match the arm64 package target.\n' ;;
    *)
        printf '\nCould not confirm a Pi 5 or CM5 with arm64 userspace. Review the model and architecture above.\n'
        exit 2 ;;
esac
