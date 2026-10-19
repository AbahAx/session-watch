#!/usr/bin/env bash
# Install a verified, extracted release. Never pipe an unreviewed script to root.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export LC_ALL=C.UTF-8
command_name="${1:-install}"
dry_run=0
for arg in "$@"; do [[ "$arg" == "--dry-run" ]] && dry_run=1; done
if [[ "$(uname -s)" != Linux ]]; then echo 'Linux is required.' >&2; exit 1; fi
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo 'Supported architectures: x86_64, aarch64.' >&2; exit 1;; esac
if [[ ! -d /run/systemd/system ]]; then echo 'A running systemd host is required (not a Docker container).' >&2; exit 1; fi
if [[ $EUID != 0 && $dry_run != 1 ]]; then echo 'Run with sudo bash install.sh ...' >&2; exit 1; fi
find_python() {
  local candidate
  for candidate in "${SESSION_WATCH_PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [[ -n "$candidate" ]] || continue
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys,venv,sqlite3; assert (3,11) <= sys.version_info[:2] < (3,14)' 2>/dev/null; then
      PYTHON="$(command -v "$candidate")"; return 0
    fi
  done
  return 1
}
if [[ "$command_name" == install || "$command_name" == upgrade ]]; then
  if [[ $dry_run == 0 && " ${*} " != *' --no-install-deps '* ]]; then
    # Use distribution repositories only; do not replace system Python or add PPAs.
    source /etc/os-release
    case "${ID:-}" in
      debian|ubuntu)
        major="${VERSION_ID%%.*}"
        if { [[ "$ID" == debian && ( "$major" == 12 || "$major" == 13 ) ]] || [[ "$ID" == ubuntu && "$major" == 24 ]]; }; then
          export DEBIAN_FRONTEND=noninteractive
          apt-get update
          apt-get install -y python3 python3-venv ca-certificates util-linux passwd tar
        else
          echo 'This OS needs an existing Python 3.11-3.13 with venv. Use --no-install-deps.' >&2; exit 1
        fi
        ;;
      rhel|rocky|almalinux)
        [[ "${VERSION_ID%%.*}" == 9 ]] || { echo 'RHEL family 9 required (or prepare Python and use --no-install-deps).' >&2; exit 1; }
        dnf install -y python3.11 python3.11-pip ca-certificates util-linux shadow-utils tar
        ;;
      *) echo 'Automatic dependency installation supports Debian 12/13, Ubuntu 24, RHEL family 9. For other systemd/glibc hosts, prepare Python 3.11-3.13 and use --no-install-deps.' >&2; exit 1;;
    esac
  fi
fi
find_python || { echo 'Python 3.11-3.13 with venv and sqlite3 is required.' >&2; exit 1; }
exec "$PYTHON" deploy/install_manager.py "$@"
