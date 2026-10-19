import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import types

import pytest


@pytest.fixture
def online():
    source = (Path(__file__).resolve().parent.parent / 'online-install.sh').read_text(encoding='utf-8')
    python = source.split("<<'SESSION_WATCH_ONLINE_PYTHON'\n", 1)[1].rsplit('\nSESSION_WATCH_ONLINE_PYTHON', 1)[0]
    module = types.ModuleType('online_installer_test')
    exec(compile(python, 'online-install.sh:python', 'exec'), module.__dict__)
    return module


@pytest.mark.parametrize('metadata,requested', [({'tag_name': '../invalid'}, 'latest'),
    ({'tag_name': 'v1.2.3', 'draft': True}, 'latest'),
    ({'tag_name': 'v1.2.3', 'prerelease': True}, 'latest'),
    ({'tag_name': 'v1.2.3'}, 'v1.2.2')])
def test_release_must_be_published_stable_and_match_request(online, metadata, requested):
    with pytest.raises(ValueError):
        online.release_tag(metadata, requested)
    assert online.release_tag({'tag_name': 'v1.2.3'}, 'latest') == 'v1.2.3'


def test_online_checksum_catches_tamper_and_ambiguous_entries(online, tmp_path):
    archive = tmp_path / 'archive.tar.gz'
    archive.write_bytes(b'fixture')
    sums = tmp_path / 'SHA256SUMS'
    line = hashlib.sha256(b'fixture').hexdigest() + '  archive.tar.gz\n'
    sums.write_text(line)
    online.check_archive(archive, sums, archive.name)
    archive.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='SHA-256'):
        online.check_archive(archive, sums, archive.name)
    archive.write_bytes(b'fixture')
    sums.write_text(line * 2)
    with pytest.raises(ValueError, match='SHA-256'):
        online.check_archive(archive, sums, archive.name)


@pytest.mark.parametrize('name,kind', [('../outside', tarfile.REGTYPE),
    ('/session-watch/absolute', tarfile.REGTYPE), ('session-watch/../outside', tarfile.REGTYPE),
    ('session-watch/link', tarfile.SYMTYPE), ('session-watch/link', tarfile.LNKTYPE),
    ('session-watch/device', tarfile.CHRTYPE)])
def test_downloaded_tar_cannot_escape_or_extract_special_files(online, tmp_path, name, kind):
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = '/etc/shadow' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
        tar.addfile(member)
    target = tmp_path / 'extracted'
    with pytest.raises(ValueError, match='Unsafe'):
        online.extract_release(archive, target)
    assert not target.exists()


def test_online_flow_pins_asset_urls_and_forwards_installer_flags(online, tmp_path, monkeypatch):
    source_archive = tmp_path / 'payload.tar.gz'
    with tarfile.open(source_archive, 'w:gz') as tar:
        content = b'#!/bin/bash\nexit 0\n'
        member = tarfile.TarInfo('session-watch/install.sh')
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    archive_bytes = source_archive.read_bytes()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    calls = []

    def download(url, target, limit):
        calls.append(url)
        if '/api.github.com/' in url:
            target.write_text(json.dumps({'tag_name': 'v1.2.3', 'draft': False, 'prerelease': False}))
        elif url.endswith('/SHA256SUMS'):
            target.write_text(digest + '  session-watch-v1.2.3-linux.tar.gz\n')
        else:
            target.write_bytes(archive_bytes)

    executed = []
    monkeypatch.setattr(online, 'download', download)
    monkeypatch.setattr(online.sys, 'argv', ['online-install.sh', '--repo', 'owner/repository', 'upgrade', '--instance', 'test', '--port', '18765'])
    monkeypatch.setattr(online.subprocess, 'run', lambda args, **kwargs: executed.append((args, kwargs)))
    old_umask = os.umask(0o022)
    try:
        online.main()
    finally:
        os.umask(old_umask)
    assert calls == ['https://api.github.com/repos/owner/repository/releases/latest',
        'https://github.com/owner/repository/releases/download/v1.2.3/SHA256SUMS',
        'https://github.com/owner/repository/releases/download/v1.2.3/session-watch-v1.2.3-linux.tar.gz']
    assert executed[0][0][2:] == ['upgrade', '--instance', 'test', '--port', '18765']
    assert executed[0][1]['umask'] == 0o022
    assert not Path(executed[0][0][1]).exists()  # Temporary extraction cleaned after completion.


def test_https_downgrade_is_rejected(online, tmp_path):
    with pytest.raises(ValueError, match='HTTPS'):
        online.download('http://example.com/archive', tmp_path / 'download', 1000)
    handler = online.HTTPSOnlyRedirect()
    with pytest.raises(ValueError, match='HTTPS'):
        handler.redirect_request(None, None, 302, '', {}, 'http://example.com/archive')
