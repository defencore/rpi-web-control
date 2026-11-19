#!/bin/sh
# Invoked on the Pi by the workstation's install.sh, inside its staging directory.
set -eu
export LC_ALL=C
version=$1
digest=$2
archive="$PWD/rpi-web-control_${version}_arm64.deb"
unit=rpi-web-control.service

printf '%s  %s\n' "$digest" "$archive" | sha256sum -c -
if [ "$(dpkg-deb -f "$archive" Package)" != rpi-web-control ] ||
   [ "$(dpkg-deb -f "$archive" Architecture)" != arm64 ] ||
   [ "$(dpkg-deb -f "$archive" Version)" != "$version" ]; then
    printf 'The package metadata does not match the selected archive.\n' >&2
    exit 1
fi

package_state() {
    dpkg-query -W -f='${db:Status-Status} ${Version} ${Architecture}\n' rpi-web-control
}
previous=$(package_state 2>/dev/null) || previous=
case "$previous" in
    "installed "*)
        installed=${previous#installed }
        installed=${installed% *}
        if dpkg --compare-versions "$installed" gt "$version"; then
            printf 'Installed version %s is newer than local version %s.\n' "$installed" "$version" >&2
            exit 1
        fi ;;
esac
before=$(systemctl is-active "$unit") || :
if [ "$previous" = "installed $version arm64" ]; then
    printf 'Version %s is already installed.\n' "$version"
else
    # Let APT's download user read this archive. Only the owner can write here.
    chmod 0711 .
    chmod 0644 "$archive"
    set -- apt-get -y --no-remove -o Dpkg::Options::=--force-confold install "$archive"
    if [ "$(id -u)" = 0 ]; then
        "$@"
    elif [ -t 0 ]; then
        sudo -- "$@"
    else
        sudo -n -- "$@"
    fi
fi

actual=$(package_state)
printf 'Package: %s\n' "$actual"
if [ "$actual" != "installed $version arm64" ]; then
    printf 'Expected installed version %s (arm64).\n' "$version" >&2
    exit 1
fi
state=$(systemctl is-active "$unit") || :
if [ "$state" = inactive ] && [ "$before" = inactive ]; then
    case "$previous" in
        "installed "*) printf '%s was stopped before the upgrade and remains stopped.\n' "$unit"; exit 0 ;;
    esac
fi
# Give the service time to start on a first installation or an active upgrade.
attempt=0
while [ "$state" != active ] && [ "$attempt" -lt 10 ]; do
    sleep 1
    state=$(systemctl is-active "$unit") || :
    attempt=$((attempt + 1))
done
printf '%s: %s\n' "$unit" "$state"
[ "$state" = active ]
