"""Privileged read-only discovery + mirror; web never receives Docker access."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploy.source_discovery import candidates, select_source, log_source, collect_source
from deploy.local_collector import publish
from deploy.log_bridge import Bridge
from app.source_control import atomic_json


def read_request(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError('Invalid source request')
        value = json.load(stream)
    if (not isinstance(value, dict) or set(value) - {'target', 'log_mode', 'log_path', 'request_id', 'at'} or
            value.get('log_mode') not in ('auto', 'manual') or
            not isinstance(value.get('target'), str) or not re.fullmatch(r'auto|native|docker:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value['target']) or
            not isinstance(value.get('log_path'), str) or len(value['log_path']) > 1000):
        raise ValueError('Invalid source request')
    if 'request_id' in value and not re.fullmatch(r'[a-f0-9]{32}', str(value['request_id'])):
        raise ValueError('Invalid preview request')
    return value


class Agent:
    def __init__(self, hints, control, spool, mirror):
        self.hints, self.control, self.spool, self.mirror = hints, control, spool, mirror
        self.bridge = None
        self.identity = None
        self.preference = {'target': 'auto', 'log_mode': 'auto', 'log_path': ''}
        self.last_collect = 0
        self.last_preview = None
        self.choices = []

    def inspect(self, preference, choices):
        import sqlite3
        result = {'ok': False, 'message': '', 'checks': {'inventory': False, 'online': False, 'log': False},
                  'client_count': 0, 'online_count': 0, 'log_readable': False}
        source = select_source(choices, preference['target'])
        data = collect_source(source)
        result['checks'].update(inventory=data['clients_ok'], online=data['online_ok'])
        result.update(client_count=len(data['clients']), online_count=len(data['online']))
        try:
            _, result['configured_path'] = log_source(source, preference, self.hints)
            result['checks']['log'] = result['log_readable'] = True
            result['message'] = '实例、在线状态及访问日志检查通过'
        except (OSError, ValueError) as exc:
            result['message'] = str(exc) if isinstance(exc, ValueError) else '访问日志当前不可读；将自动重试'
        result['ok'] = all(result['checks'].values())
        if not data['clients_ok']:
            result['message'] = '客户端数据库不可读；Docker 数据库请使用 bind mount 或 volume 持久化'
        elif not data['online_ok']:
            result['message'] = data.get('online_error', '已有 Xray 在线统计接口不可用')
        return result

    def refresh(self, preference):
        self.choices = candidates(self.hints)
        metadata = {'format': 1, 'generated_at': time.time(), 'ready': False,
                    'choices': [{'id': c['id'], 'label': c['label'], 'kind': c['kind']} for c in self.choices]}
        data = dict(version=1, generated_at=time.time(), clients_ok=False, online_ok=False, clients=[], online=[])
        try:
            source = select_source(self.choices, preference['target'])
            metadata.update(selected=source['id'], kind=source['kind'])
            data = collect_source(source)
            path, configured = log_source(source, preference, self.hints)
            identity = (source['id'], str(path))
            if identity != self.identity:
                if self.bridge:
                    self.bridge.close()
                self.mirror.mkdir(parents=True, mode=0o750, exist_ok=True)
                # Source switches create a new inode and skip historical bytes.
                # Only mirror/checkpoint files belonging to this agent change.
                checkpoint = self.mirror / 'cursor.json'
                try:
                    same = json.loads(checkpoint.read_text()).get('source') == str(path)
                except (OSError, ValueError):
                    same = False
                if not same:
                    checkpoint.unlink(missing_ok=True)
                    old = self.mirror / 'access.log'
                    if old.exists():
                        os.replace(old, self.mirror / 'access.log.1')
                self.bridge = Bridge(path, self.mirror)
                self.identity = identity
            self.bridge.step()
            generation = '\0'.join((source['id'], configured, str((source.get('process') or {}).get('pid', ''))))
            metadata['generation'] = hashlib.sha256(generation.encode()).hexdigest()
            info = self.bridge.output.stat()
            metadata['mirror_identity'] = [info.st_dev, info.st_ino]
            metadata.update(ready=True, configured_path=configured, message='访问日志已接入，持续自动检测中')
        except (OSError, ValueError, KeyError) as exc:
            if self.bridge:
                self.bridge.close()
            self.bridge = None
            self.identity = None
            (self.mirror / 'heartbeat').unlink(missing_ok=True)
            metadata['message'] = str(exc) if isinstance(exc, ValueError) else '来源暂时不可读；将自动重新检测'
        data['source_generation'] = metadata.get('generation')
        data['source_id'] = metadata.get('selected')
        publish(self.spool, data)
        atomic_json(self.spool / 'source.json', metadata)
        self.last_collect = time.monotonic()

    def step(self):
        try:
            preference = read_request(self.control / 'settings.json')
        except (OSError, ValueError):
            preference = self.preference
        if preference != self.preference or time.monotonic() - self.last_collect >= 5:
            self.preference = preference
            self.refresh(preference)
        if self.bridge:
            try:
                self.bridge.step()
            except (OSError, ValueError):
                self.last_collect = 0
                (self.mirror / 'heartbeat').unlink(missing_ok=True)
        try:
            request = read_request(self.control / 'preview.json')
            identifier = request['request_id']
            if identifier != self.last_preview and abs(time.time() - request['at']) < 30:
                try:
                    result = self.inspect(request, candidates(self.hints))
                except (OSError, ValueError):
                    result = {'ok': False, 'message': '未找到指定实例或来源不唯一，请选择实例后重试',
                              'checks': {'inventory': False, 'online': False, 'log': False},
                              'client_count': 0, 'online_count': 0, 'log_readable': False}
                atomic_json(self.spool / 'preview.json', {'request_id': identifier, 'result': result})
                self.last_preview = identifier
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def close(self):
        if self.bridge:
            self.bridge.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'control', 'spool', 'mirror'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    agent = Agent(json.loads(args.config.read_text()), args.control, args.spool, args.mirror)
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while running:
            agent.step()
            time.sleep(0.5)
    finally:
        agent.close()


if __name__ == '__main__':
    main()
