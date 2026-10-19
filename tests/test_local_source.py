import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from app.integrations import IntegrationError
from app.local_source import read_snapshot, sync_clients, online_set
from app.models import Settings
from app.runtime import Runtime
from app.storage import Store
from deploy.local_collector import inventory, api_address, online_users, collect, publish
from app.identity import normalize_online_names


def snapshot(**changes):
    return {'version': 1, 'generated_at': time.time(), 'clients_ok': True, 'online_ok': True,
            'clients': [{'email': 'lin', 'enabled': True, 'inbounds': ['in-a']}], 'online': [], **changes}


def test_modern_inventory_includes_disabled_and_unassigned_without_secrets(tmp_path):
    path = tmp_path / 'panel.db'
    db = sqlite3.connect(path)
    db.executescript('''CREATE TABLE clients(id INTEGER,email TEXT,enable INTEGER,uuid TEXT,password TEXT);
        CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER);
        CREATE TABLE inbounds(id INTEGER,tag TEXT);
        INSERT INTO clients VALUES (1,'lin',1,'PRIVATE-UUID','PRIVATE-PASSWORD'),(2,'disabled',0,'x','y');
        INSERT INTO inbounds VALUES (1,'in-a'),(2,'in-b');
        INSERT INTO client_inbounds VALUES (1,1),(1,2);''')
    db.close()
    before = path.read_bytes()
    result = inventory(path)
    assert result == [{'email': 'disabled', 'enabled': False, 'inbounds': []},
                      {'email': 'lin', 'enabled': True, 'inbounds': ['in-a', 'in-b']}]
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['panel.db']
    assert 'PRIVATE' not in json.dumps(result)


def test_legacy_inventory_and_uncheckpointed_wal_are_read_consistently(tmp_path):
    path = tmp_path / 'panel.db'
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE inbounds(tag TEXT, settings TEXT)')
    db.execute('INSERT INTO inbounds VALUES (?,?)', ('in-a', json.dumps({'clients': [
        {'email': 'lin', 'enable': True, 'id': 'PRIVATE'}, {'email': 'disabled', 'enable': False}]})))
    db.commit()
    assert [c['email'] for c in inventory(path)] == ['disabled', 'lin']
    assert db.execute('SELECT COUNT(*) FROM inbounds').fetchone()[0] == 1
    db.close()


def config():
    return {'api': {'services': ['StatsService'], 'tag': 'api'}, 'stats': {},
            'policy': {'levels': {'0': {'statsUserOnline': True}}},
            'inbounds': [{'tag': 'api', 'listen': '127.0.0.1', 'port': 62789}]}


def test_local_api_uses_existing_loopback_and_no_mutation_commands(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config()))
    def run(args, **kwargs):
        assert args == [str(Path('/opt/xray')), 'api', 'statsgetallonlineusers', '--server=127.0.0.1:62789', '--timeout=3']
        return subprocess.CompletedProcess(args, 0, b'{}')
    monkeypatch.setattr('deploy.local_collector.subprocess.run', run)
    assert online_users(Path('/opt/xray'), path) == []
    data = config()
    data['inbounds'][0]['listen'] = '0.0.0.0'
    with pytest.raises(ValueError):
        api_address(data)
    data = config()
    data['policy']['levels']['0']['statsUserOnline'] = False
    with pytest.raises(ValueError):
        api_address(data)


def test_failed_cli_is_unknown_never_empty_online_list(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config()))
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'xray', stderr=b'SECRET')
    monkeypatch.setattr('deploy.local_collector.subprocess.run', fail)
    data = collect(tmp_path / 'absent.db', path, Path('/opt/xray'))
    assert not data['online_ok'] and not data['clients_ok']
    assert not (tmp_path / 'absent.db').exists()
    assert 'SECRET' not in json.dumps(data)
    with pytest.raises(IntegrationError):
        online_set(data)


@pytest.mark.parametrize('changes', [{'generated_at': time.time()-1000}, {'generated_at': float('nan')},
    {'online': 'lin'}, {'online_ok': 1}, {'clients': [{'email': 'lin', 'enabled': True, 'inbounds': [], 'password': 'secret'}]}])
def test_bad_or_stale_snapshot_cannot_mark_anyone_offline(tmp_path, changes):
    publish(tmp_path, snapshot(**changes))
    with pytest.raises(IntegrationError):
        read_snapshot(Settings(local_snapshot_path=str(tmp_path / 'snapshot.json')))


def test_inventory_sync_preserves_alias_monitor_preferences_and_removal_history(tmp_path):
    store = Store(tmp_path)
    with store.transaction() as db:
        sync_clients(db, snapshot())
        db.execute("UPDATE users SET alias='我的电脑',monitored=0 WHERE email='lin'")
        sync_clients(db, snapshot(clients=[]))
    assert store.rows('SELECT alias,monitored FROM users')[0] == {'alias': '我的电脑', 'monitored': 0}
    assert store.rows('SELECT present FROM source_clients')[0]['present'] == 0
    with store.transaction() as db:
        sync_clients(db, snapshot(clients_ok=False))
    assert store.rows('SELECT present FROM source_clients')[0]['present'] == 0
    store.close()


def test_local_runtime_imports_while_paused_then_handles_outage_without_false_offline(tmp_path):
    spool = tmp_path / 'spool'
    publish(spool, snapshot())
    store = Store(tmp_path / 'app')
    settings = Settings(local_snapshot_path=str(spool / 'snapshot.json'))
    store.save_settings(settings)
    runtime = Runtime(store)
    asyncio.run(runtime.tick())
    assert store.rows('SELECT email FROM users') == [{'email': 'lin'}]
    assert store.rows('SELECT * FROM sessions') == []
    log = tmp_path / 'access.log'
    log.touch()
    settings.monitor_enabled = True
    settings.log_path = str(log)
    store.save_settings(settings)
    publish(spool, snapshot(online=['lin']))
    asyncio.run(runtime.tick())
    assert runtime.health['panel'] == 'ok'
    publish(spool, snapshot(online_ok=False))
    asyncio.run(runtime.tick())
    session = store.rows('SELECT state,offline_since FROM sessions')[0]
    assert session == {'state': 'unknown', 'offline_since': None}
    assert store.rows('SELECT * FROM outbox') == []
    asyncio.run(runtime.stop())
    store.close()


def test_legacy_demo_setting_is_ignored_and_saved_without_mode(tmp_path):
    store = Store(tmp_path)
    store.set('settings', {'mode': 'demo'})
    settings = store.settings()
    assert settings.source_type == 'local' and not settings.monitor_enabled
    store.save_settings(settings)
    assert 'mode' not in store.get('settings')
    store.close()


def test_xray_statistics_keys_map_to_existing_client_without_phantom_user(tmp_path, monkeypatch):
    monkeypatch.setattr('deploy.local_collector.inventory', lambda _: [
        {'email': 'Lite', 'enabled': True, 'inbounds': ['in-a']}])
    monkeypatch.setattr('deploy.local_collector.online_users', lambda *_: ['user>>>Lite>>>online'])
    data = collect(Path('db'), Path('config'), Path('xray'))
    assert data['online_ok'] and data['online'] == ['Lite']
    spool = tmp_path / 'spool'
    publish(spool, data)
    log = tmp_path / 'access.log'
    log.touch()
    store = Store(tmp_path / 'app')
    store.save_settings(Settings(monitor_enabled=True, local_snapshot_path=str(spool / 'snapshot.json'), log_path=str(log)))
    runtime = Runtime(store)
    asyncio.run(runtime.tick())
    assert store.rows('SELECT email FROM users') == [{'email': 'Lite'}]
    assert store.rows('SELECT email,state FROM sessions') == [{'email': 'Lite', 'state': 'online'}]
    asyncio.run(runtime.stop())
    store.close()


def test_legacy_raw_snapshot_is_also_normalized_by_web_reader():
    data = snapshot(clients=[{'email': 'Lite', 'enabled': True, 'inbounds': []}], online=['user>>>Lite>>>online'])
    assert online_set(data) == {'Lite'}


@pytest.mark.parametrize('names', [['user>>>missing>>>online'], ['user>>>Lite>>>traffic>>>uplink'], ['unknown']])
def test_unknown_online_names_fail_instead_of_creating_user_or_false_offline(names):
    with pytest.raises(IntegrationError):
        online_set(snapshot(clients=[{'email': 'Lite', 'enabled': True, 'inbounds': []}], online=names))


def test_normalization_supports_plain_names_delimiters_max_length_and_duplicates():
    emails = {'Lite', 'a>>>b', 'x' * 256}
    names = ['Lite', 'user>>>Lite>>>online', 'user>>>a>>>b>>>online', 'user>>>' + 'x' * 256 + '>>>online']
    assert normalize_online_names(names, emails) == emails
    with pytest.raises(ValueError):
        normalize_online_names(['user>>>Lite>>>online'], {'Lite', 'user>>>Lite>>>online'})


def test_unknown_core_name_invalidates_collector_snapshot(monkeypatch):
    monkeypatch.setattr('deploy.local_collector.inventory', lambda _: [{'email': 'Lite', 'enabled': True, 'inbounds': []}])
    monkeypatch.setattr('deploy.local_collector.online_users', lambda *_: ['user>>>missing>>>online'])
    data = collect(Path('db'), Path('config'), Path('xray'))
    assert data['clients_ok'] and not data['online_ok']
    assert data['online'] == []


def test_raw_online_name_and_access_log_share_one_session_and_message(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    now = [datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr('app.local_source.time.time', lambda: now[0])
    spool = tmp_path / 'spool'
    client = {'email': 'Lite', 'enabled': True, 'inbounds': ['in-a']}
    publish(spool, snapshot(clients=[client]))
    log = tmp_path / 'access.log'
    log.touch()
    store = Store(tmp_path / 'app')
    store.save_settings(Settings(monitor_enabled=True, telegram_enabled=True,
        local_snapshot_path=str(spool / 'snapshot.json'), log_path=str(log), log_timezone='Etc/UTC'))
    runtime = Runtime(store)
    asyncio.run(runtime.tick())
    now[0] += 10
    log.write_text('2026/10/19 06:00:10 from 203.0.113.1:1234 accepted tcp:example.com:443 [in-a -> direct] email: Lite\n')
    publish(spool, snapshot(clients=[client], online=['user>>>Lite>>>online']))
    asyncio.run(runtime.tick())
    assert store.rows('SELECT email FROM users') == [{'email': 'Lite'}]
    session = store.rows('SELECT email,state,requests,first_ip,first_target FROM sessions')[0]
    assert session == {'email': 'Lite', 'state': 'online', 'requests': 1,
                       'first_ip': '203.0.113.1', 'first_target': 'example.com'}
    message = store.rows('SELECT text,status FROM outbox')[0]
    assert message['status'] == 'pending'
    assert 'Lite 上线' in message['text'] and 'example.com' in message['text'] and '203.0.113.1' in message['text']
    assert 'user>>>' not in message['text']
    asyncio.run(runtime.stop())
    store.close()
