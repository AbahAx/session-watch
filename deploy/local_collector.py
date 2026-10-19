"""Read-only local 3x-ui inventory and Xray online-status collector.

Run separately from the unprivileged web app. Never exports panel credentials,
proxy UUIDs/passwords, subscription IDs, or the original database/configuration.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.identity import MAX_ONLINE_NAME, normalize_online_names


def inventory(database: Path):
    # mode=ro plus a systemd read-only mount protects the original database.
    # Do not use immutable=1: a running panel may have uncheckpointed WAL data.
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}

        def add(email, enabled, tag=None):
            if not isinstance(email, str) or not email.strip() or len(email) > 256:
                return
            item = result.setdefault(email, {'email': email, 'enabled': False, 'inbounds': []})
            item['enabled'] = item['enabled'] or bool(enabled)
            if isinstance(tag, str) and tag and tag not in item['inbounds']:
                item['inbounds'].append(tag[:256])

        if 'clients' in tables and 'client_inbounds' in tables:
            # Explicit column allowlist: no SELECT * from secret-bearing tables.
            for row in db.execute('SELECT email,enable FROM clients'):
                add(row['email'], row['enable'])
            for row in db.execute('''SELECT c.email,c.enable,i.tag FROM clients c
                JOIN client_inbounds ci ON ci.client_id=c.id
                JOIN inbounds i ON i.id=ci.inbound_id'''):
                add(row['email'], row['enable'], row['tag'])
        elif 'inbounds' in tables:
            # Older panels embed clients in inbound settings. Parse in this
            # privileged process, emit only the same small public allowlist.
            for row in db.execute('SELECT tag,settings FROM inbounds'):
                data = json.loads(row['settings'])
                for client in data.get('clients', []):
                    add(client.get('email'), client.get('enable', True), row['tag'])
        else:
            raise ValueError('Unsupported panel schema')
        return sorted(result.values(), key=lambda c: c['email'])
    finally:
        db.close()


def api_address(config: dict):
    api = config.get('api', {})
    levels = config.get('policy', {}).get('levels', {})
    if ('StatsService' not in api.get('services', []) or 'stats' not in config or
            not any(p.get('statsUserOnline') is True for p in levels.values())):
        raise ValueError('Online statistics not enabled')
    # Use only the existing loopback listener; never change Xray's settings.
    for inbound in config.get('inbounds', []):
        if inbound.get('tag') == api.get('tag', 'api'):
            host = inbound.get('listen', '')
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError('API must be loopback')
            port = int(inbound['port'])
            if not 1 <= port <= 65535:
                raise ValueError('Invalid API port')
            return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    raise ValueError('No supported local API listener')


def online_users(binary: Path, config: Path):
    address = api_address(json.loads(config.read_text()))
    # This command has no reset flag and does not modify counters or users.
    proc = subprocess.run([str(binary), 'api', 'statsgetallonlineusers',
                           '--server=' + address, '--timeout=3'],
                          capture_output=True, timeout=5, check=True)
    data = json.loads(proc.stdout)
    if not isinstance(data, dict) or set(data) - {'users'}:
        raise ValueError('Unexpected API format')
    users = data.get('users', [])  # protobuf JSON omits an empty repeated field.
    if not isinstance(users, list) or any(not isinstance(u, str) or not u.strip() or len(u) > MAX_ONLINE_NAME for u in users):
        raise ValueError('Unexpected online list')
    return sorted(set(users))


def collect(database, config, binary):
    data = {'version': 1, 'generated_at': time.time(), 'clients_ok': False,
            'online_ok': False, 'clients': [], 'online': []}
    try:
        data['clients'] = inventory(database)
        data['clients_ok'] = True
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        data['clients_error'] = '本地客户端数据库不可读或版本不兼容'
    try:
        if not data['clients_ok']:
            raise ValueError('No authoritative inventory')
        data['online'] = sorted(normalize_online_names(online_users(binary, config),
                                                     (client['email'] for client in data['clients'])))
        data['online_ok'] = True
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        data['online_error'] = '本地 Xray 在线查询失败；请检查已有统计接口与核心版本'
    # Timestamp at completion, after bounded source operations.
    data['generated_at'] = time.time()
    return data


def publish(directory: Path, data):
    directory.mkdir(parents=True, mode=0o750, exist_ok=True)
    temp = directory / '.snapshot.tmp'
    with temp.open('w', encoding='utf-8') as stream:
        os.chmod(temp, 0o640)
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, directory / 'snapshot.json')


def discover(explicit, candidates):
    if explicit:
        return explicit.resolve(strict=True)
    for path in candidates:
        if Path(path).is_file():
            return Path(path)
    raise ValueError('Local 3x-ui files not found; specify paths explicitly')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--spool', type=Path, default=Path('/var/lib/session-watch-local'))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    database = discover(args.database, ['/etc/x-ui/x-ui.db', '/usr/local/x-ui/db/x-ui.db'])
    config = discover(args.config, ['/usr/local/x-ui/bin/config.json', '/etc/x-ui/config.json'])
    binary = discover(args.binary, ['/usr/local/x-ui/bin/xray-linux-amd64', '/usr/local/x-ui/bin/xray-linux-arm64', '/usr/local/bin/xray'])
    if any(path.resolve().is_relative_to(args.spool.resolve()) for path in (database, config, binary)):
        raise ValueError('Source files must be outside the writable spool')
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        publish(args.spool, collect(database, config, binary))
        if args.once:
            break
        # Short waits allow prompt shutdown, even between snapshots.
        for _ in range(100):
            if not running:
                break
            time.sleep(0.1)


if __name__ == '__main__':
    main()
