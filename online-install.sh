#!/usr/bin/env bash
# Public GitHub Release entry point. Run from a root terminal.
set -Eeuo pipefail
if [[ " ${*} " == *' --help '* || " ${*} " == *' -h '* ]]; then
  echo 'Usage: bash online-install.sh --repo OWNER/REPOSITORY [--version latest|vX.Y.Z] [install|upgrade] [installer options]'
  echo 'Public GitHub releases only. Default: latest stable release, loopback web access.'
  exit 0
fi
[[ "$(uname -s)" == Linux && -d /run/systemd/system ]] || { echo 'Linux with running systemd required.' >&2; exit 1; }
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo 'Supported architectures: x86_64, aarch64.' >&2; exit 1;; esac
[[ $EUID == 0 ]] || { echo 'Run from a root terminal, or use sudo bash online-install.sh ...' >&2; exit 1; }
# No jq, Git, Node, or gh CLI is needed. Python's standard library downloads
# and validates the release before executing its native installer.
if ! command -v python3 >/dev/null 2>&1; then
  source /etc/os-release
  case "${ID:-}" in
    debian|ubuntu)
      apt-get update
      DEBIAN_FRONTEND=noninteractive apt-get install -y python3 ca-certificates
      ;;
    rhel|rocky|almalinux) dnf install -y python3 ca-certificates ;;
    *) echo 'Install Python 3.9+ and ca-certificates first.' >&2; exit 1;;
  esac
fi
python3 - "$@" <<'SESSION_WATCH_ONLINE_PYTHON'
"""Standalone online installer; embedded to keep the entry point one file."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_UNPACKED = 256 * 1024 * 1024


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise ValueError('Refusing a redirect outside HTTPS')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, target, limit):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise ValueError('Downloads require HTTPS')
    opener = urllib.request.build_opener(HTTPSOnlyRedirect())
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Session-Watch-Installer', 'Accept': 'application/vnd.github+json' if '/api.github.com/' in url else '*/*'})
            with opener.open(req, timeout=30) as response, target.open('wb') as stream:
                total = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ValueError('Download exceeds the allowed size')
                    stream.write(chunk)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ValueError('Repository/release/asset not found. Publish a public stable Release first; drafts are not downloadable anonymously.') from exc
            if exc.code in (401, 403, 429):
                raise ValueError('GitHub rejected the request or its anonymous API limit was reached. Retry later; private repositories are not supported by this entry point.') from exc
            if attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(attempt + 1)


def release_tag(metadata, requested):
    if metadata.get('draft') or metadata.get('prerelease'):
        raise ValueError('Only published stable releases are accepted')
    tag = metadata.get('tag_name', '')
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('Release tag must be vX.Y.Z')
    if requested != 'latest' and requested != tag:
        raise ValueError('Returned release does not match the requested version')
    return tag


def check_archive(archive, sums, filename):
    matches = []
    for line in sums.read_text(encoding='ascii').splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64})  (.+)', line)
        if match and match[2] == filename:
            matches.append(match[1].lower())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if len(matches) != 1 or digest != matches[0]:
        raise ValueError('Release archive SHA-256 mismatch; installation stopped')


def extract_release(archive, target):
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > MAX_UNPACKED:
            raise ValueError('Expanded release is too large')
        names = set()
        for member in members:
            p = PurePosixPath(member.name)
            if (not member.isfile() or member.name in names or not p.parts or
                    p.parts[0] != 'session-watch' or len(p.parts) < 2 or
                    any(part in ('', '.', '..') for part in member.name.split('/')) or
                    '\\' in member.name or p.is_absolute()):
                raise ValueError('Unsafe release archive entry')
            names.add(member.name)
        for member in members:
            # Copy only validated regular files. Ignore archive ownership/modes,
            # never extract symlinks, hardlinks, devices or traversal entries.
            path = target.joinpath(*PurePosixPath(member.name).parts)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tar.extractfile(member) as src, path.open('xb') as dest:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    dest.write(chunk)
    if not (target / 'session-watch/install.sh').is_file():
        raise ValueError('Release is missing its native installer')


def main():
    if sys.version_info < (3, 9):
        raise ValueError('The online downloader requires Python 3.9+; the app requires 3.11-3.13')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='Public GitHub OWNER/REPOSITORY')
    parser.add_argument('--version', default='latest')
    args, rest = parser.parse_known_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', args.repo) or args.repo.split('/')[1] in ('.', '..'):
        raise ValueError('--repo must be OWNER/REPOSITORY, without a URL or access token')
    if args.version != 'latest' and not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', args.version):
        raise ValueError('--version must be latest or vX.Y.Z')
    if not rest or rest[0].startswith('--'):
        rest.insert(0, 'install')
    if rest[0] not in ('install', 'upgrade'):
        raise ValueError('Online entry point supports install/upgrade; use the local management command for other operations')
    if '--dry-run' in rest:
        raise ValueError('Online download is not a dry run. For a read-only precheck, download/extract a release and run bash install.sh install --dry-run')
    endpoint = 'latest' if args.version == 'latest' else 'tags/' + args.version
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='session-watch-download-') as directory:
        work = Path(directory)
        metadata = work / 'release.json'
        download(f'https://api.github.com/repos/{args.repo}/releases/{endpoint}', metadata, 2 * 1024 * 1024)
        tag = release_tag(json.loads(metadata.read_text(encoding='utf-8')), args.version)
        filename = f'session-watch-{tag}-linux.tar.gz'
        # Pin every asset to the same resolved tag: latest may change while downloading.
        base = f'https://github.com/{args.repo}/releases/download/{tag}'
        archive, sums = work / filename, work / 'SHA256SUMS'
        print(f'Downloading {args.repo} {tag} ...', flush=True)
        download(base + '/SHA256SUMS', sums, 1024 * 1024)
        download(base + '/' + filename, archive, MAX_ARCHIVE)
        check_archive(archive, sums, filename)
        extract_release(archive, work)
        print('Archive checksum passed. Starting native installer.', flush=True)
        subprocess.run(['bash', str(work / 'session-watch/install.sh'), *rest], check=True, umask=0o022)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
SESSION_WATCH_ONLINE_PYTHON
