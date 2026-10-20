"""Small, secret-free requests to the separately sandboxed source agent."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import time


def managed():
    return bool(os.environ.get('SESSION_WATCH_SOURCE_CONTROL'))


def preferences(settings):
    return {'target': settings.local_target, 'log_mode': settings.log_mode, 'log_path': settings.log_path}


def atomic_json(path, data):
    content = json.dumps(data, ensure_ascii=False).encode()
    temp = path.with_name('.request-' + secrets.token_hex(12))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    try:
        os.chmod(temp, 0o640)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def publish_preferences(settings):
    if managed():
        path = Path(os.environ['SESSION_WATCH_SOURCE_CONTROL']) / 'settings.json'
        data = preferences(settings)
        try:
            if json.loads(path.read_text()) == data:
                return
        except (OSError, ValueError):
            pass
        atomic_json(path, data)


def status():
    path = os.environ.get('SESSION_WATCH_SOURCE_STATUS')
    if not path:
        return None
    try:
        p = Path(path)
        if p.stat().st_size > 65536 or time.time() - p.stat().st_mtime > 25:
            raise ValueError()
        value = json.loads(p.read_text())
        if value.get('format') != 1:
            raise ValueError()
        return value
    except (OSError, ValueError):
        return {'ready': False, 'message': '本机自动检测服务未运行或状态过期，请检查采集服务', 'choices': []}


def effective_log_path(settings):
    if managed():
        return os.environ.get('SESSION_WATCH_MANAGED_LOG', '')
    return settings.log_path


async def preview(settings):
    request = {**preferences(settings), 'request_id': secrets.token_hex(16), 'at': time.time()}
    control = Path(os.environ['SESSION_WATCH_SOURCE_CONTROL'])
    atomic_json(control / 'preview.json', request)
    destination = Path(os.environ['SESSION_WATCH_SOURCE_STATUS']).parent / 'preview.json'
    for _ in range(100):
        try:
            if destination.stat().st_size <= 1024 * 1024:
                result = json.loads(destination.read_text())
                if result.get('request_id') == request['request_id']:
                    return result['result']
        except (OSError, ValueError, KeyError):
            pass
        await asyncio.sleep(0.15)
    return {'ok': False, 'message': '自动检测服务未响应，请检查采集服务后重试',
            'checks': {'inventory': False, 'online': False, 'log': False}, 'client_count': 0, 'online_count': 0, 'log_readable': False}
