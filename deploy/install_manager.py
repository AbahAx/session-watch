"""Linux release manager. Standard library only; never mutates a 3x-ui source."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import time

MARKER = "session-watch-managed-v1"
ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = Path("/etc/systemd/system")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def unit_quote(value):
    value = str(value)
    if any(c in value for c in "\n\r\0"):
        raise ValueError("Paths and host names cannot contain control characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def safe_relative(value):
    p = PurePosixPath(value)
    if not value or '\\' in value or p.is_absolute() or any(x in ('', '.', '..') for x in value.split('/')):
        raise ValueError("Unsafe package path")
    return p


def verify_package(root):
    manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or not VERSION_RE.fullmatch(manifest.get('version', '')):
        raise ValueError("Invalid release manifest")
    expected = manifest.get('files', {})
    if not expected or len(expected) > 20000:
        raise ValueError("Invalid release file count")
    actual = set()
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError("Release must not contain symlinks")
        if path.is_file() and path.relative_to(root).as_posix() != 'release-manifest.json':
            actual.add(path.relative_to(root).as_posix())
    if actual != set(expected):
        raise ValueError("Release has missing or extra files; extract a clean package")
    for name, info in expected.items():
        path = root.joinpath(*safe_relative(name).parts)
        if path.stat().st_size != info['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
            raise ValueError(f"Release checksum mismatch: {name}")
    for name in ('app/main.py', 'frontend/dist/index.html', 'requirements-linux.lock.txt', 'deploy/install_manager.py'):
        if name not in expected:
            raise ValueError(f"Incomplete release: {name}")
    return manifest


class Layout:
    def __init__(self, instance=''):
        if instance and not re.fullmatch(r'[a-z][a-z0-9-]{0,11}', instance):
            raise ValueError("Instance must be 1-12 lowercase letters/digits/hyphens, starting with a letter")
        self.instance = instance
        self.name = 'session-watch' + ('-' + instance if instance else '')
        self.home = Path('/opt') / self.name
        self.data = Path('/var/lib') / self.name
        self.local = Path('/var/lib') / (self.name + '-local')
        self.bridge = Path('/var/lib') / (self.name + '-bridge')
        self.control = Path('/var/lib') / (self.name + '-control')
        self.config = Path('/etc') / self.name
        self.backups = Path('/var/backups') / self.name
        self.wrapper = Path('/usr/local/bin') / self.name
        self.marker = self.config / 'install.json'
        self.units = [self.name + suffix for suffix in (
            '.service', '-local.service', '-bridge.service', '-cert.path', '-cert.service')]

    def state(self):
        if not self.marker.exists():
            return None
        state = json.loads(self.marker.read_text())
        if state.get('owner') != MARKER or state.get('name') != self.name:
            raise ValueError("Foreign ownership marker; refusing to modify")
        return state

    def assert_safe_roots(self):
        for path in (self.home, self.data, self.local, self.bridge, self.control, self.config, self.backups):
            if path.is_symlink() or path.resolve() != path or os.path.ismount(path):
                raise ValueError(f"Managed root must be a real directory, not a symlink/mount: {path}")
        if self.wrapper.is_symlink():
            raise ValueError("Management command is an unexpected symlink")


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.tmp-' + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or path.is_symlink():
            raise ValueError('Expected a regular, non-symlink managed data file')
        return stream.read()


def write_state(layout, state):
    write(layout.marker, json.dumps(state, indent=2, ensure_ascii=False) + '\n')


def stamp():
    return time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + secrets.token_hex(3)


def systemctl(*args, check=True):
    result = subprocess.run(['systemctl', *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if check and result.returncode:
        raise RuntimeError('systemctl ' + ' '.join(args) + ': ' + result.stderr[-1500:].strip())
    return result


def stop(layout):
    systemctl('stop', *layout.units, check=False)


def discovered_path(explicit, candidates):
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Explicit source paths must be existing absolute files")
        return str(path.resolve())
    return next((str(Path(p).resolve()) for p in candidates if Path(p).is_file()), '')


def source_paths(args, layout):
    if args.no_collector:
        return dict(database='', config='', binary='', log='')
    sources = {
        'database': discovered_path(args.database, ['/etc/x-ui/x-ui.db', '/usr/local/x-ui/db/x-ui.db']),
        'config': discovered_path(args.config, ['/usr/local/x-ui/bin/config.json', '/etc/x-ui/config.json']),
        'binary': discovered_path(args.binary, ['/usr/local/x-ui/bin/xray-linux-amd64',
            '/usr/local/x-ui/bin/xray-linux-arm64', '/usr/local/bin/xray']),
        'log': discovered_path(args.log, []),
    }
    if not sources['log'] and sources['config']:
        raw = json.loads(Path(sources['config']).read_text()).get('log', {}).get('access', '')
        if raw and raw not in ('none', 'stdout', 'stderr'):
            path = Path(raw)
            if not path.is_absolute() and sources['binary']:
                # Resolve relative logs only using the actual running core cwd.
                processes = Path('/proc').iterdir() if Path('/proc').is_dir() else ()
                for proc in processes:
                    if not proc.name.isdigit():
                        continue
                    try:
                        if (proc / 'exe').resolve() == Path(sources['binary']):
                            candidate = (proc / 'cwd').resolve() / path
                            if candidate.is_file():
                                path = candidate
                                break
                    except OSError:
                        continue
            if path.is_absolute() and path.is_file():
                sources['log'] = str(path.resolve())
    for value in filter(None, sources.values()):
        unit_quote(value)
        if any(Path(value).is_relative_to(root) for root in (layout.home, layout.data, layout.local, layout.bridge, layout.control, layout.config, layout.backups)):
            raise ValueError("Original source files cannot be inside managed directories")
    return sources


def connection(args, old=None):
    old = old or {}
    host = args.host if args.host is not None else old.get('host', '127.0.0.1')
    ip = ipaddress.ip_address(host)
    if ip.version != 4 or host not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError("--host supports 127.0.0.1 or 0.0.0.0")
    port = args.port if args.port is not None else old.get('port', 8765)
    if not 1024 <= port <= 65535:
        raise ValueError("Use an independent unprivileged port, 1024-65535")
    domain = (args.domain if args.domain is not None else old.get('domain', '')).lower()
    if domain and (len(domain) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', domain) or '..' in domain):
        raise ValueError("--domain must be a plain DNS host name without scheme, path or port")
    cert = args.cert if args.cert is not None else old.get('cert', '')
    key = args.key if args.key is not None else old.get('key', '')
    if bool(cert) != bool(key) or (not ip.is_loopback and not (cert and domain)):
        raise ValueError("Public binding requires --domain, --cert and --key (existing HTTPS certificate)")
    if cert:
        # Keep the live certificate symlink, so ACME renewal is observed rather
        # than pinning its current numbered archive target.
        for value in (cert, key):
            if not Path(value).is_absolute() or not Path(value).is_file():
                raise ValueError('Certificate and key must be existing absolute files')
            unit_quote(value)
        cert, key = str(Path(cert)), str(Path(key))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        if not domain:
            raise ValueError("HTTPS requires --domain matching your certificate")
        # Check key/certificate pairing here. The browser verifies chain and name.
    return dict(host=host, port=port, domain=domain, cert=cert, key=key)


def common_unit(user, group, work):
    return (f'User={user}\nGroup={group}\nWorkingDirectory={work}\n'
            'Restart=on-failure\nRestartSec=3\nUMask=0077\nNoNewPrivileges=true\n'
            'PrivateTmp=true\nPrivateDevices=true\nProtectSystem=strict\nProtectHome=true\n'
            'ProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\n'
            'RestrictSUIDSGID=true\nLockPersonality=true\nCapabilityBoundingSet=\n'
            'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n')


def render_units(layout, state):
    name, current = layout.name, layout.home / 'current'
    sources = state['sources']
    c = state['connection']
    python = current / '.venv/bin/python'
    prefix = '[Unit]\nDescription=Session Watch'
    install = '\n[Install]\nWantedBy=multi-user.target\n'
    web = prefix + '\nAfter=network.target\n\n[Service]\n' + common_unit(name, name, current)
    web += f'EnvironmentFile={layout.config / "web.env"}\nReadWritePaths={unit_quote(layout.data)} {unit_quote(layout.control)}\n'
    hidden = {'/etc/x-ui', '/usr/local/x-ui', '/var/log/x-ui', '/etc/shadow', '/etc/gshadow', '/etc/ssh', '/var/lib/docker', '/run/docker.sock', '/var/run/docker.sock'}
    hidden.update(v for v in sources.values() if isinstance(v, str) and v)
    web += 'InaccessiblePaths=' + ' '.join(unit_quote('-' + v) for v in sorted(hidden)) + '\n'
    command = [python, '-m', 'uvicorn', 'app.main:create_app', '--factory', '--host', c['host'], '--port', str(c['port']), '--no-access-log', '--no-proxy-headers']
    if c['cert']:
        web += 'LoadCredential=tls-cert:' + c['cert'].replace('%', '%%') + '\n'
        web += 'LoadCredential=tls-key:' + c['key'].replace('%', '%%') + '\n'
    web += 'ExecStart=' + ' '.join(unit_quote(v) for v in command)
    if c['cert']:
        # %d must be expanded by systemd rather than escaped by unit_quote.
        web += ' --ssl-certfile %d/tls-cert --ssl-keyfile %d/tls-key'
    web += '\n' + install
    units = {name + '.service': web}
    if not state.get('collector_disabled', False):
        collector = prefix + ' read-only local source\nAfter=network.target\n\n[Service]\n' + common_unit('root', name, current)
        collector += f'ReadWritePaths={unit_quote(layout.local)} {unit_quote(layout.bridge)}\nInaccessiblePaths={unit_quote(layout.data)} /etc/shadow /etc/gshadow /etc/ssh\n'
        collector += 'ProtectHome=read-only\nCapabilityBoundingSet=CAP_SYS_ADMIN CAP_SYS_PTRACE CAP_DAC_READ_SEARCH\n'
        collector += 'IPAddressDeny=any\nIPAddressAllow=localhost\n'
        cmd = [python, current / 'deploy/source_agent.py', '--spool', layout.local,
               '--mirror', layout.bridge, '--control', layout.control, '--config', layout.config / 'source.json']
        collector += 'ExecStart=' + ' '.join(unit_quote(v) for v in cmd) + '\n' + install
        units[name + '-local.service'] = collector
    if c['cert']:
        # Watch parent entries too: ACME tools atomically replace live symlinks,
        # while an inotify watch on the file alone follows the old target.
        watched = {c['cert'], c['key'], str(PurePosixPath(c['cert']).parent), str(PurePosixPath(c['key']).parent)}
        units[name + '-cert.path'] = prefix + ' certificate renewal\n\n[Path]\n' + ''.join('PathChanged=' + p.replace('%', '%%') + '\n' for p in sorted(watched)) + '\n[Install]\nWantedBy=multi-user.target\n'
        units[name + '-cert.service'] = prefix + ' reload own HTTPS\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/systemctl try-restart ' + name + '.service\n'
    return units


def environment(layout, state):
    c = state['connection']
    origin = ('https' if c['cert'] else 'http') + '://' + (c['domain'] or c['host']) + ':' + str(c['port'])
    pairs = {'SESSION_WATCH_DATA': str(layout.data), 'SESSION_WATCH_SECURE_COOKIE': '1' if c['cert'] else '0',
             'SESSION_WATCH_ALLOWED_HOSTS': '127.0.0.1,localhost' + (',' + c['domain'] if c['domain'] else ''),
             'SESSION_WATCH_PUBLIC_ORIGIN': origin}
    if not state.get('collector_disabled', False):
        pairs['SESSION_WATCH_BRIDGE_HEARTBEAT'] = str(layout.bridge / 'heartbeat')
        pairs['SESSION_WATCH_SOURCE_CONTROL'] = str(layout.control)
        pairs['SESSION_WATCH_SOURCE_STATUS'] = str(layout.local / 'source.json')
        pairs['SESSION_WATCH_MANAGED_LOG'] = str(layout.bridge / 'access.log')
    return ''.join(k + '=' + unit_quote(v) + '\n' for k, v in pairs.items())


def switch(layout, release):
    release = release.resolve(strict=True)
    if not release.is_relative_to(layout.home / 'releases'):
        raise ValueError("Release is outside the owned release directory")
    temp = layout.home / 'current.next'
    temp.unlink(missing_ok=True)
    temp.symlink_to(release, target_is_directory=True)
    os.replace(temp, layout.home / 'current')


def create_account(layout, owned):
    import pwd
    import grp
    try:
        user = pwd.getpwnam(layout.name)
    except KeyError:
        user = None
    try:
        group = grp.getgrnam(layout.name)
    except KeyError:
        group = None
    if not owned and (user or group):
        raise ValueError("An unowned service account already exists; choose --instance NAME")
    if not group:
        run(['groupadd', '--system', layout.name])
    if not user:
        nologin = shutil.which('nologin') or '/usr/sbin/nologin'
        run(['useradd', '--system', '--gid', layout.name, '--home-dir', layout.data, '--no-create-home', '--shell', nologin, layout.name])
    user = pwd.getpwnam(layout.name)
    if user.pw_uid == 0 or user.pw_gid != grp.getgrnam(layout.name).gr_gid:
        raise ValueError("Unsafe service account")
    return user.pw_uid, user.pw_gid


def private_dirs(layout, uid, gid):
    for path in (layout.config, layout.backups):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    layout.home.mkdir(parents=True, exist_ok=True, mode=0o755)
    layout.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(layout.data, uid, gid)
    os.chmod(layout.data, 0o700)
    layout.control.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(layout.control, uid, gid)
    os.chmod(layout.control, 0o700)
    for path in (layout.local, layout.bridge):
        path.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chown(path, 0, gid)
        os.chmod(path, 0o750)


def prepare_release(layout, args, manifest, package_root):
    release = layout.home / 'releases' / (manifest['version'] + '-' + stamp())
    release.mkdir(parents=True, mode=0o755)
    for relative in [*manifest['files'], 'release-manifest.json']:
        src = package_root.joinpath(*safe_relative(relative).parts)
        target = release / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        os.chmod(target, 0o755 if relative == 'install.sh' else 0o644)
    run([sys.executable, '-m', 'venv', release / '.venv'])
    cmd = [release / '.venv/bin/python', '-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary=:all:', '-r', release / 'requirements-linux.lock.txt']
    if args.wheel_dir:
        cmd += ['--no-index', '--find-links', str(Path(args.wheel_dir).resolve(strict=True))]
    run(cmd, cwd=release)
    run([release / '.venv/bin/python', '-m', 'pip', 'check'], cwd=release)
    run([release / '.venv/bin/python', '-c', 'from app.main import create_app; from app.storage import Store'], cwd=release)
    return release


def backup(layout, state):
    target = layout.backups / stamp()
    target.mkdir(mode=0o700, parents=True)
    # Consistent SQLite online backup; never copy an active WAL database file.
    database = layout.data / 'session-watch.db'
    if database.exists():
        if database.is_symlink() or not stat.S_ISREG(database.stat().st_mode):
            raise ValueError('Refusing to back up a symlink/non-regular database')
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target / 'session-watch.db') as dest:
            src.backup(dest)
        os.chmod(target / 'session-watch.db', 0o600)
    for name in ('secret.key',):
        if (layout.data / name).exists():
            write(target / name, read_regular(layout.data / name))
    for name in ('web.env', 'initial-admin.txt', 'source.json'):
        if (layout.config / name).exists():
            write(target / name, (layout.config / name).read_bytes())
    write(target / 'install.json', json.dumps(state, indent=2))
    for unit in layout.units:
        p = UNIT_DIR / unit
        if p.is_file():
            write(target / 'units' / unit, p.read_bytes())
    print('Backup:', target)
    return target


def validate_backup(layout, target):
    if target.parent != layout.backups or target.is_symlink() or not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[a-f0-9]{6}', target.name):
        raise ValueError("Invalid backup identifier")
    state = json.loads((target / 'install.json').read_text())
    if state.get('owner') != MARKER or state.get('name') != layout.name:
        raise ValueError("Backup belongs to another instance")
    release = Path(state['release']).resolve(strict=True)
    if not release.is_relative_to(layout.home / 'releases') or not (release / '.venv/bin/python').exists():
        raise ValueError("Backup release is not available")
    return state


def restore(layout, target, uid, gid):
    state = validate_backup(layout, target)
    for suffix in ('', '-wal', '-shm'):
        (layout.data / ('session-watch.db' + suffix)).unlink(missing_ok=True)
    for name in ('session-watch.db', 'secret.key'):
        if (target / name).exists():
            write(layout.data / name, (target / name).read_bytes())
            os.chown(layout.data / name, uid, gid)
    for name in ('web.env', 'initial-admin.txt', 'source.json'):
        if (target / name).exists():
            write(layout.config / name, (target / name).read_bytes())
    for unit in layout.units:
        systemctl('disable', unit, check=False)
        (UNIT_DIR / unit).unlink(missing_ok=True)
        if (target / 'units' / unit).is_file():
            write(UNIT_DIR / unit, (target / 'units' / unit).read_bytes(), 0o644)
    switch(layout, Path(state['release']))
    write_state(layout, state)
    systemctl('daemon-reload')
    start(layout)
    return state


def start(layout):
    for unit in layout.units:
        if (UNIT_DIR / unit).exists() and unit != layout.name + '-cert.service':
            systemctl('enable', '--now', unit)


def healthy(state, seconds=35):
    c = state['connection']
    deadline = time.monotonic() + seconds
    while True:
        try:
            if c['cert']:
                conn = http.client.HTTPSConnection('127.0.0.1', c['port'], timeout=2,
                    context=ssl._create_unverified_context())  # Local readiness only, no credentials.
            else:
                conn = http.client.HTTPConnection('127.0.0.1', c['port'], timeout=2)
            conn.request('GET', '/api/health', headers={'Host': c['domain'] or '127.0.0.1'})
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
            if response.status == 200 and data.get('ok') and data.get('version') == state['version']:
                return True
        except (OSError, ValueError, http.client.HTTPException):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def bootstrap_data(layout, state, log_timezone, admin):
    # Secret travels via stdin, never argv, the environment or command logs.
    payload = json.dumps({'data': str(layout.data), 'snapshot': str(layout.local / 'snapshot.json'),
        'log': str(layout.bridge / 'access.log') if state['sources']['log'] else '',
        'timezone': log_timezone, 'username': admin.username if admin else '',
        'password': admin.password if admin else ''})
    result = run(['runuser', '-u', layout.name, '--', layout.home / 'current/.venv/bin/python', '-m', 'app.bootstrap'],
                 input=payload, text=True, capture_output=True, cwd=layout.home / 'current')
    if result.stdout.strip() == 'created':
        detail = ('Password: ' + admin.password) if admin.generated else 'Password: supplied during installation; not stored here.'
        write(layout.config / 'initial-admin.txt', 'Username: ' + admin.username + '\n' + detail + '\nChange credentials later in the web account settings.\n')


def check_listen_port(connection, old=None):
    if old and old.get('installed') and connection['port'] == old['connection']['port']:
        return
    with socket.socket() as probe:
        try:
            probe.bind((connection['host'], connection['port']))
        except OSError as exc:
            raise ValueError(f"面板端口 {connection['port']} 不可用，请更换端口后重试") from exc


def install(layout, args):
    package_root = Path(args.package_dir).resolve(strict=True) if args.package_dir else ROOT
    manifest = verify_package(package_root)
    old = layout.state()
    owned = old is not None
    if old and old.get('installed') and args.command != 'upgrade':
        raise ValueError("Already installed; run upgrade (settings and password are preserved)")
    if args.command == 'upgrade' and not (old and old.get('installed')):
        raise ValueError("No managed installation to upgrade")
    if not owned:
        if any(p.exists() for p in (layout.home, layout.data, layout.local, layout.bridge, layout.control, layout.config, layout.wrapper)) or any((UNIT_DIR / u).exists() for u in layout.units):
            raise ValueError("Unmanaged installation/path exists. Refusing to overwrite; choose --instance NAME")
    # Keep the verified source package clean: importing installer helpers must
    # not create __pycache__ files that invalidate its release manifest.
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from deploy.install_wizard import prepare_preferences
    c, admin = prepare_preferences(args, old, layout.data, connection, check_listen_port, read_regular)
    if args.command == 'upgrade' and not any((args.database, args.config, args.binary, args.log, args.no_collector)):
        sources = old['sources']
    else:
        if args.command == 'upgrade':
            for key in ('database', 'config', 'binary', 'log'):
                if getattr(args, key) is None:
                    setattr(args, key, old['sources'][key] or None)
        sources = source_paths(args, layout)
    plan = {'version': manifest['version'], 'instance': layout.name, 'connection': c, 'sources': sources,
            'data': str(layout.data), 'system_python': sys.executable}
    print(json.dumps(plan, indent=2, ensure_ascii=False))
    if args.dry_run:
        print('DRY RUN: no directories, packages, accounts or services changed.')
        return
    uid, gid = create_account(layout, owned)
    private_dirs(layout, uid, gid)
    if not old:
        write_state(layout, {'owner': MARKER, 'name': layout.name, 'installed': False,
            'version': manifest['version'], 'release': '', 'python': str(Path(sys.executable).resolve()),
            'connection': c, 'sources': sources})
    release = prepare_release(layout, args, manifest, package_root)
    state = {'owner': MARKER, 'name': layout.name, 'installed': True, 'version': manifest['version'],
             'release': str(release), 'python': str(Path(sys.executable).resolve()), 'connection': c, 'sources': sources,
             'collector_disabled': bool(args.no_collector or (args.command == 'upgrade' and old.get('collector_disabled', False)))}
    previous = None
    if old and old.get('installed'):
        stop(layout)
        try:
            previous = backup(layout, old)
        except BaseException:
            start(layout)
            raise
    try:
        switch(layout, release)
        write(layout.config / 'web.env', environment(layout, state))
        write(layout.config / 'source.json', json.dumps(sources))
        units = render_units(layout, state)
        for unit in layout.units:
            systemctl('disable', unit, check=False)
            (UNIT_DIR / unit).unlink(missing_ok=True)
        for unit, text in units.items():
            write(UNIT_DIR / unit, text, 0o644)
        bootstrap_data(layout, state, args.log_timezone or system_timezone(), admin)
        write_state(layout, state)
        wrapper = '#!/bin/sh\nexec ' + shell_quote(str(layout.home / 'current/.venv/bin/python')) + ' ' + shell_quote(str(layout.home / 'current/deploy/install_manager.py')) + ' "$@" --instance ' + shell_quote(layout.instance) + '\n'
        write(layout.wrapper, wrapper, 0o755)
        systemctl('daemon-reload')
        start(layout)
        if not healthy(state):
            raise RuntimeError("New web service failed readiness; inspect journalctl -u " + layout.name)
    except BaseException:
        stop(layout)
        if previous:
            restore(layout, previous, uid, gid)
            print('Upgrade failed; previous code and database restored.', file=sys.stderr)
        else:
            for unit in layout.units:
                systemctl('disable', unit, check=False)
                (UNIT_DIR / unit).unlink(missing_ok=True)
            state['installed'] = False
            write_state(layout, state)
            systemctl('daemon-reload')
            print('Install failed; only this instance was stopped. Data retained; fix the error and run install again.', file=sys.stderr)
        raise
    print('READY:', ('https' if c['cert'] else 'http') + '://' + (c['domain'] or c['host']) + ':' + str(c['port']))
    print('Initial administrator credentials (root only; existing passwords are preserved):', layout.config / 'initial-admin.txt')
    print('Monitoring and Telegram are OFF on a new installation. Configure and enable them in the web UI.')
    print('Manage:', layout.wrapper, 'status | doctor | backup | rollback | uninstall')
    if c['host'].startswith('127.'):
        print(f'For remote access use an SSH tunnel: ssh -L {c["port"]}:127.0.0.1:{c["port"]} root@YOUR_SERVER')
    doctor(layout, state)


def shell_quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


def system_timezone():
    result = subprocess.run(['timedatectl', 'show', '-p', 'Timezone', '--value'], capture_output=True, text=True)
    value = result.stdout.strip()
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value)
        return value
    except (ValueError, KeyError):
        return 'UTC'


def doctor(layout, state):
    print('Web readiness:', 'OK' if healthy(state, seconds=0) else 'FAILED')
    for unit in layout.units:
        if (UNIT_DIR / unit).exists() and unit != layout.name + '-cert.service':
            print(unit + ':', systemctl('is-active', unit, check=False).stdout.strip())
    path = layout.local / 'snapshot.json'
    try:
        snapshot = json.loads(path.read_text())
        fresh = 0 <= time.time() - snapshot['generated_at'] <= 30
        print('Local snapshot:', 'fresh' if fresh else 'stale', '| inventory:', bool(snapshot.get('clients_ok')), '| online query:', bool(snapshot.get('online_ok')))
        if not snapshot.get('online_ok'):
            print('Online API unavailable: existing Xray must support online-user statistics and an existing loopback StatsService. No upstream config was changed.')
    except (OSError, ValueError, KeyError):
        print('Local snapshot: not yet available. Check source paths/collector journal; remote API mode is configurable in the UI.')
    heartbeat = layout.bridge / 'heartbeat'
    print('Log discovery:', 'disabled' if state.get('collector_disabled') else 'continuous (native + Docker)')
    print('Log mirror:', 'fresh' if heartbeat.exists() and time.time() - heartbeat.stat().st_mtime < 15 else 'waiting for a readable source')
    print('Firewall and SELinux were not changed. Public HTTPS needs the chosen TCP port allowed by your firewall/security group.')


def uninstall(layout, state, args):
    stop(layout)
    for unit in layout.units:
        systemctl('disable', unit, check=False)
        (UNIT_DIR / unit).unlink(missing_ok=True)
    systemctl('daemon-reload')
    layout.wrapper.unlink(missing_ok=True)
    if layout.home.exists():
        shutil.rmtree(layout.home)
    state['installed'] = False
    write_state(layout, state)
    if args.purge:
        for path in (layout.data, layout.local, layout.bridge, layout.control, layout.config):
            if path.exists():
                shutil.rmtree(path)
        run(['userdel', layout.name])
        subprocess.run(['groupdel', layout.name], capture_output=True)
    print('Removed this instance only. ' + ('Data and credentials purged.' if args.purge else 'Data, credentials and service account retained for reinstallation.'))
    print('Backups retained:', layout.backups, '(contain credentials; store securely or remove deliberately)')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', nargs='?', default='install', choices=['install', 'upgrade', 'status', 'doctor', 'backup', 'rollback', 'uninstall'])
    p.add_argument('--instance', default='')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--no-install-deps', action='store_true')
    interaction = p.add_mutually_exclusive_group()
    interaction.add_argument('--interactive', dest='interactive', action='store_true', help='Prompt using the controlling SSH terminal')
    interaction.add_argument('--non-interactive', dest='interactive', action='store_false', help='Use flags/defaults without prompting')
    p.set_defaults(interactive=None)
    p.add_argument('--admin-username', help='Initial administrator name; existing administrators are preserved')
    p.add_argument('--admin-password-file', help='Root-owned 600 file with the initial password (never use a password in argv)')
    p.add_argument('--package-dir', help='Clean extracted release directory (required when upgrading via the installed management command)')
    p.add_argument('--host')
    p.add_argument('--port', type=int)
    p.add_argument('--domain')
    p.add_argument('--cert')
    p.add_argument('--key')
    for key in ('database', 'config', 'binary', 'log', 'wheel-dir', 'log-timezone'):
        p.add_argument('--' + key)
    p.add_argument('--no-collector', action='store_true')
    p.add_argument('--backup-id', help='Owned backup ID to roll back to; otherwise the most recent usable backup')
    p.add_argument('--purge', action='store_true', help='Uninstall AND delete this instance data, keys, credentials and service account; retain backups')
    return p


def main():
    args = parser().parse_args()
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'aarch64', 'arm64') or not Path('/run/systemd/system').is_dir():
        raise ValueError('Supported Linux architecture and running systemd required')
    if not ((3, 11) <= sys.version_info[:2] < (3, 14)):
        raise ValueError('Python 3.11-3.13 required')
    if not args.dry_run and os.geteuid() != 0:
        raise ValueError('Run this command with sudo')
    if not args.dry_run:
        # Root login shells may use 077; code/venv must remain traversable by
        # the dedicated service account. Private paths set explicit 700/600.
        os.umask(0o022)
    if args.dry_run and args.command not in ('install', 'upgrade'):
        raise ValueError('--dry-run is available for install and upgrade only')
    layout = Layout(args.instance)
    layout.assert_safe_roots()
    # A backup, upgrade and uninstall must never race one another. Read-only
    # diagnostics and dry runs need no lock or filesystem mutation.
    lock_fd = None
    if not args.dry_run and args.command not in ('status', 'doctor'):
        import fcntl
        lock_path = Path('/run/lock') / (layout.name + '.lock')
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another management command is running for this instance')
    if args.log_timezone:
        from zoneinfo import ZoneInfo
        ZoneInfo(args.log_timezone)
    if args.command in ('install', 'upgrade'):
        install(layout, args)
        return
    state = layout.state()
    if not state or (not state.get('installed') and args.command != 'uninstall'):
        raise ValueError('No active managed installation; unmanaged services are never modified')
    if args.command in ('status', 'doctor'):
        doctor(layout, state)
    elif args.command == 'backup':
        backup(layout, state)
    elif args.command == 'rollback':
        import pwd
        user = pwd.getpwnam(layout.name)
        candidates = [layout.backups / args.backup_id] if args.backup_id else sorted(layout.backups.glob('*'), reverse=True)
        target = next((p for p in candidates if p.is_dir() and (p / 'install.json').exists()), None)
        if not target:
            raise ValueError('No usable backup found')
        validate_backup(layout, target)
        stop(layout)
        try:
            saved = backup(layout, state)
        except BaseException:
            start(layout)
            raise
        try:
            restored = restore(layout, target, user.pw_uid, user.pw_gid)
            if not healthy(restored):
                raise RuntimeError('Rollback readiness failed')
        except BaseException:
            stop(layout)
            restore(layout, saved, user.pw_uid, user.pw_gid)
            raise
        print('Rollback complete. Newer activity remains in:', saved)
    elif args.command == 'uninstall':
        uninstall(layout, state, args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('ERROR:', str(exc), file=sys.stderr)
        sys.exit(1)
