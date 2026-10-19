import hashlib
import json
from pathlib import Path
import sqlite3
import tarfile
import zipfile

import pytest

from deploy import build_release as builder
from deploy import install_manager as manager


@pytest.fixture
def package(tmp_path):
    root = tmp_path / 'source'
    contents = {'app/__init__.py': '__version__ = "1.2.3"', 'app/main.py': '# app',
                'frontend/dist/index.html': '<html>Built frontend</html>',
                'requirements-linux.lock.txt': 'fastapi==0.146.0\n', 'install.sh': '#!/bin/bash\n',
                'deploy/install_manager.py': '# manager'}
    for name, text in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _, files = builder.files(root)
    (root / 'release-manifest.json').write_bytes(files['release-manifest.json'])
    return root


def test_package_tamper_and_extra_files_rejected(package):
    assert manager.verify_package(package)['version'] == '1.2.3'
    (package / 'app/main.py').write_text('# changed')
    with pytest.raises(ValueError, match='checksum'):
        manager.verify_package(package)
    (package / 'app/main.py').write_text('# app')
    (package / 'unexpected.secret').touch()
    with pytest.raises(ValueError, match='extra files'):
        manager.verify_package(package)


@pytest.mark.parametrize('value', ['/etc/shadow', '../outside', 'app/../../outside', 'app\\main.py', 'app//main.py', './app', ''])
def test_manifest_cannot_escape_root(value):
    with pytest.raises(ValueError):
        manager.safe_relative(value)


@pytest.mark.parametrize('instance', ['../../x-ui', 'x' * 13, 'Root', '--all', ''])
def test_instance_names(instance):
    if instance:
        with pytest.raises(ValueError):
            manager.Layout(instance)
    else:
        assert manager.Layout(instance).name == 'session-watch'


def test_public_bind_requires_tls_and_independent_port():
    args = manager.parser().parse_args(['--host', '0.0.0.0'])
    with pytest.raises(ValueError, match='Public binding'):
        manager.connection(args)
    args = manager.parser().parse_args(['--port', '443'])
    with pytest.raises(ValueError, match='independent'):
        manager.connection(args)
    args = manager.parser().parse_args([])
    assert manager.connection(args)['host'] == '127.0.0.1'


def test_units_keep_sources_readonly_and_web_unprivileged():
    layout = manager.Layout('test')
    args = manager.parser().parse_args([])
    state = {'connection': manager.connection(args), 'python': '/usr/bin/python3.11',
             'sources': {'database': '/etc/x-ui/x-ui.db', 'config': '/usr/local/x-ui/bin/config.json',
                         'binary': '/usr/local/x-ui/bin/xray-linux-amd64', 'log': '/var/log/x-ui/access.log'}}
    units = manager.render_units(layout, state)
    web = units[layout.name + '.service']
    collector = units[layout.name + '-local.service']
    bridge = units[layout.name + '-bridge.service']
    assert 'User=session-watch-test\n' in web
    assert 'User=root\n' in collector
    assert 'ProtectSystem=strict' in collector and 'IPAddressDeny=any' in collector
    assert 'RestrictAddressFamilies=\nRestrictAddressFamilies=AF_UNIX' in bridge
    assert 'InaccessiblePaths=' in web and 'x-ui.db' in web
    for unit in units.values():
        for line in unit.splitlines():
            if line.startswith('ReadWritePaths='):
                assert '/etc/x-ui' not in line and '/usr/local/x-ui' not in line and '/var/log/x-ui' not in line
    assert 'statsget' not in collector  # Delegated to the tested read-only collector.


def test_systemd_quoting_blocks_newline_injection():
    with pytest.raises(ValueError):
        manager.unit_quote('/tmp/log\nExecStart=/bin/evil')
    assert manager.unit_quote('/x/%n/file') == '"/x/%%n/file"'
    assert manager.unit_quote('/x/has space') == '"/x/has space"'


def test_certificate_renewal_watches_live_symlink_parent():
    layout = manager.Layout('test')
    state = {'connection': {'host': '127.0.0.1', 'port': 8443, 'domain': 'watch.example.com',
        'cert': '/etc/ssl/live/watch/fullchain.pem', 'key': '/etc/ssl/live/watch/privkey.pem'},
        'python': '/usr/bin/python3.11', 'sources': dict(database='', config='', binary='', log='')}
    units = manager.render_units(layout, state)
    assert 'PathChanged=/etc/ssl/live/watch\n' in units[layout.name + '-cert.path']
    assert 'LoadCredential=tls-cert:/etc/ssl/live/watch/fullchain.pem' in units[layout.name + '.service']
    assert '--ssl-certfile %d/tls-cert' in units[layout.name + '.service']


def test_atomic_write_replaces_destination_instead_of_following_it(tmp_path):
    destination = tmp_path / 'managed-file'
    original = tmp_path / 'original-file'
    original.write_text('untouched')
    try:
        destination.symlink_to(original)
    except OSError:
        pytest.skip('Symlink creation unavailable')
    manager.write(destination, 'managed update')
    assert not destination.is_symlink()
    assert original.read_text() == 'untouched'
    assert destination.read_text() == 'managed update'


def test_release_allowlist_excludes_private_runtime_and_dependencies(package, tmp_path):
    for name in ('data/secret.key', '.env', '.private/credentials.json', 'frontend/node_modules/token',
                 'app/__pycache__/cached.pyc', 'frontend/src/__pycache__/cached.pyc'):
        p = package / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('must not ship')
    (package / 'frontend/package.json').write_text('{"name":"source"}')
    runtime, source = builder.build(tmp_path / 'output', package)
    with tarfile.open(runtime) as archive:
        names = archive.getnames()
        assert 'session-watch/frontend/dist/index.html' in names
        assert not any('node_modules' in n or '__pycache__' in n or 'secret.key' in n or '/.env' in n or '.private' in n for n in names)
        assert 'session-watch/frontend/package.json' not in names
    with zipfile.ZipFile(source) as archive:
        assert 'session-watch/frontend/package.json' in archive.namelist()
    checksums = (tmp_path / 'output/SHA256SUMS').read_text()
    assert hashlib.sha256(runtime.read_bytes()).hexdigest() in checksums


def test_sqlite_backup_includes_live_wal_without_touching_source(tmp_path, monkeypatch):
    layout = manager.Layout('test')
    layout.data = tmp_path / 'data'
    layout.config = tmp_path / 'config'
    layout.backups = tmp_path / 'backups'
    layout.data.mkdir()
    layout.config.mkdir()
    monkeypatch.setattr(manager, 'UNIT_DIR', tmp_path / 'units')
    database = layout.data / 'session-watch.db'
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE test(value)')
        db.execute("INSERT INTO test VALUES ('committed')")
        db.commit()
        (layout.data / 'secret.key').write_text('private key fixture')
        copied = manager.backup(layout, {'owner': manager.MARKER, 'name': layout.name})
        with sqlite3.connect(copied / 'session-watch.db') as result:
            assert result.execute('SELECT value FROM test').fetchone()[0] == 'committed'
        assert db.execute('SELECT count(*) FROM test').fetchone()[0] == 1
        assert (copied / 'secret.key').read_text() == 'private key fixture'


def test_relative_access_log_not_guessed_from_config_directory(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'log': {'access': './access.log'}}))
    (tmp_path / 'access.log').write_text('not necessarily the actual log')
    fake_binary = tmp_path / 'not-the-running-xray'
    fake_binary.touch()
    args = manager.parser().parse_args(['--config', str(config), '--binary', str(fake_binary)])
    result = manager.source_paths(args, manager.Layout('test'))
    assert result['log'] == ''


def test_disabled_log_is_not_created(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'log': {'access': 'none'}}))
    args = manager.parser().parse_args(['--config', str(config)])
    before = sorted(tmp_path.iterdir())
    assert manager.source_paths(args, manager.Layout('test'))['log'] == ''
    assert sorted(tmp_path.iterdir()) == before
