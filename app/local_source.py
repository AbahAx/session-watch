import json
import time
from pathlib import Path

from app.integrations import IntegrationError
from app.identity import MAX_ONLINE_NAME, normalize_online_names


def read_snapshot(settings):
    try:
        path = Path(settings.local_snapshot_path)
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
        stamp = data['generated_at']
        if (isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or
                not 0 <= time.time() - stamp <= max(25, settings.poll_seconds * 2.5)):
            raise ValueError()
        if data['version'] != 1 or not isinstance(data['clients_ok'], bool) or not isinstance(data['online_ok'], bool):
            raise ValueError()
        clients, online = data['clients'], data['online']
        if not isinstance(clients, list) or not isinstance(online, list):
            raise ValueError()
        seen = set()
        for client in clients:
            if (set(client) != {'email', 'enabled', 'inbounds'} or
                    not isinstance(client['email'], str) or not client['email'].strip() or len(client['email']) > 256 or
                    client['email'] in seen or not isinstance(client['enabled'], bool) or
                    not isinstance(client['inbounds'], list) or
                    any(not isinstance(tag, str) or len(tag) > 256 for tag in client['inbounds'])):
                raise ValueError()
            seen.add(client['email'])
        if any(not isinstance(email, str) or not email.strip() or len(email) > MAX_ONLINE_NAME for email in online):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError, KeyError):
        raise IntegrationError('本地采集快照缺失、过期或格式异常，请检查本地采集服务') from None


def sync_clients(db, data):
    if not data['clients_ok']:
        return
    db.execute('UPDATE source_clients SET present=0')
    for client in data['clients']:
        db.execute('INSERT OR IGNORE INTO users(email) VALUES (?)', (client['email'],))
        db.execute('''INSERT INTO source_clients(email,enabled,inbounds,present,synced_at) VALUES (?,?,?,1,?)
            ON CONFLICT(email) DO UPDATE SET enabled=excluded.enabled,inbounds=excluded.inbounds,
            present=1,synced_at=excluded.synced_at''',
                   (client['email'], int(client['enabled']), json.dumps(client['inbounds'], ensure_ascii=False), data['generated_at']))


def online_set(data):
    if not data['online_ok'] or not data['clients_ok']:
        raise IntegrationError('本地客户端或 Xray 在线查询异常，本轮状态记为未知')
    try:
        # Also normalize here for older collector snapshots during an upgrade.
        return normalize_online_names(data['online'], (client['email'] for client in data['clients']))
    except ValueError:
        raise IntegrationError('Xray 在线标识无法与客户端清单对应，本轮状态记为未知') from None
