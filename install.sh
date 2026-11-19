#!/usr/bin/env bash
# Run on the workstation from a checkout of rpi-web-control.
set -euo pipefail
export LC_ALL=C
umask 077

usage() {
    cat <<'EOF'
Usage: ./install.sh [--diagnose] [user@]host
Install the latest local arm64 package over SSH. Finish any active recording first.
Use --diagnose to collect a board report without installing a package.
Reports are saved under dist/install-reports/ in this checkout.
EOF
}

mode=install
case ${1:-} in
    -h|--help) usage; exit 0 ;;
    --diagnose) mode=diagnose; shift ;;
esac
if [[ $# != 1 || -z ${1:-} || $1 == -* || $1 == *[!a-zA-Z0-9_.@:%-]* ]]; then
    usage >&2
    exit 2
fi
target=$1
root=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$root"
mkdir -p dist/install-reports
report=$(mktemp -d "$root/dist/install-reports/$target-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
printf 'Report directory: %s\n' "$report"

# Share host-key verification and SSH authentication across all steps.
connection=$(mktemp -d /tmp/rpi-web-control-ssh.XXXXXXXX)
ssh_options=(-o ControlMaster=auto -o ControlPersist=60 -o "ControlPath=$connection/socket"
             -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2)
remote_dir=
connected=false
on_board() {
    local terminal=$1
    shift
    ssh "${ssh_options[@]}" "$terminal" -- "$target" "$@"
}
diagnostics() {
    printf 'Collecting diagnostics in %s/diagnostics.txt\n' "$report"
    on_board -T 'sh -s -- --diagnostics' < tools/inspect-board.sh 2>&1 | tee "$report/diagnostics.txt"
}
finish() {
    local result=$?
    trap - EXIT
    set +e
    if [[ $mode == install && $connected == true && $result != 0 && $result != 130 && $result != 143 ]]; then
        diagnostics || printf 'Diagnostics incomplete. See %s/diagnostics.txt\n' "$report" >&2
    fi
    if [[ -n $remote_dir ]]; then
        on_board -T "rm -rf -- '$remote_dir'" </dev/null || printf 'Could not remove %s on %s\n' "$remote_dir" "$target" >&2
    fi
    if [[ -S $connection/socket ]]; then
        ssh "${ssh_options[@]}" -O exit -- "$target" >/dev/null 2>&1
    fi
    rmdir "$connection"
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ $mode == diagnose ]]; then
    diagnostics
    exit 0
fi

release=$(awk '$1 == "Version:" {print $2}' packaging/control)
if [[ ! -f dist/rpi-web-control_${release}_arm64.deb ]]; then
    make deb 2>&1 | tee "$report/build.log"
fi
versions=()
for file in dist/rpi-web-control_*_arm64.deb; do
    if [[ -f $file && $file =~ ^dist/rpi-web-control_([0-9][0-9A-Za-z.+:~-]*)_arm64\.deb$ ]]; then
        versions+=("${BASH_REMATCH[1]}")
    fi
done
if [[ ${#versions[@]} == 0 ]]; then
    printf 'No rpi-web-control arm64 packages found in dist/.\n' >&2
    exit 1
fi

printf 'Checking board and collecting inventory…\n'
# Exit 2 from inspect-board means the board or userspace is incompatible.
if on_board -T 'sh -s' < tools/inspect-board.sh 2>&1 | tee "$report/inventory.txt"; then
    connected=true
else
    result=$?
    if [[ $result == 2 ]]; then connected=true; fi
    exit "$result"
fi

# Use the Pi's Debian comparator, including on workstations without dpkg.
version=$(printf '%s\n' "${versions[@]}" | on_board -T '
    set -e
    latest=
    while IFS= read -r candidate; do
        if [ -z "$latest" ] || dpkg --compare-versions "$candidate" gt "$latest"; then
            latest=$candidate
        fi
    done
    printf "%s\n" "$latest"')
if [[ ! $version =~ ^[0-9][0-9A-Za-z.+:~-]*$ ]]; then
    printf 'Unexpected package version returned by the board.\n' >&2
    exit 1
fi
archive="rpi-web-control_${version}_arm64.deb"
printf 'Selected %s for %s\n' "$archive" "$target" | tee "$report/install.log"
python3 packaging/build.py --verify --output "dist/$archive" 2>&1 | tee -a "$report/install.log"
digest=$(python3 -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "dist/$archive")
printf 'SHA-256: %s\n' "$digest" | tee -a "$report/install.log"

staging=$(on_board -T 'mktemp -d /tmp/rpi-web-control-install.XXXXXXXX' </dev/null)
if [[ ! $staging =~ ^/tmp/rpi-web-control-install\.[a-zA-Z0-9]{8}$ ]]; then
    printf 'Unexpected staging directory returned by the board.\n' >&2
    exit 1
fi
remote_dir=$staging
COPYFILE_DISABLE=1 tar -cf - -C "$root/tools" install-package.sh -C "$root/dist" "$archive" |
    on_board -T "tar -xf - -C '$remote_dir'" 2>&1 | tee -a "$report/install.log"
apply="cd '$remote_dir' && sh ./install-package.sh '$version' '$digest'"
if [[ -t 0 ]]; then
    on_board -t "$apply" 2>&1 | tee -a "$report/install.log"
else
    on_board -T "$apply" </dev/null 2>&1 | tee -a "$report/install.log"
fi
