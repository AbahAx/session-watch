import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Settings
from app.runtime import Runtime
from app.storage import Store
from deploy.local_collector import publish

HEADERS={'X-Requested-With':'session-watch'}


@pytest.fixture
def connected(tmp_path, monkeypatch):
    monkeypatch.delenv('SESSION_WATCH_BRIDGE_HEARTBEAT',raising=False)
    spool=tmp_path/'spool'
    publish(spool,{'version':1,'generated_at':time.time(),'clients_ok':True,'online_ok':True,
                   'clients':[{'email':'client-a','enabled':True,'inbounds':['in-a']}],'online':['client-a']})
    log=tmp_path/'access.log'
    log.touch()
    app=create_app(tmp_path/'app',background=False)
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.post('/api/auth/setup',headers=HEADERS,json={'username':'admin','password':'local-test-password-123'})
        settings=client.get('/api/settings').json()
        settings.update(local_snapshot_path=str(spool/'snapshot.json'),log_path=str(log))
        assert client.put('/api/settings',headers=HEADERS,json=settings).status_code==200
        assert client.post('/api/monitor',headers=HEADERS,json={'enabled':True}).status_code==200
        yield client,log


def test_hot_save_keeps_collector_and_pending_offline_timer(connected):
    client,_=connected
    store=client.app.state.store
    with store.transaction() as db:
        db.execute("INSERT OR IGNORE INTO users(email) VALUES ('client-a')")
        db.execute("INSERT INTO sessions(email,state,started_at,last_online,offline_since) VALUES ('client-a','pending',1,2,3)")
    revision=client.get('/api/settings').json()['settings_revision']
    response=client.patch('/api/settings',headers=HEADERS,json={'source_name':'我的节点','top_targets':8,
        'ip_location_enabled':True,'settings_revision':revision})
    assert response.status_code==200 and response.json()['monitor_enabled']
    assert response.json()['source_name']=='我的节点'
    assert store.rows('SELECT state,offline_since FROM sessions')[0]=={'state':'pending','offline_since':3}


def test_stale_patch_cannot_overwrite_later_changes(connected):
    client,_=connected
    revision=client.get('/api/settings').json()['settings_revision']
    assert client.patch('/api/settings',headers=HEADERS,json={'source_name':'新名称','settings_revision':revision}).status_code==200
    result=client.patch('/api/settings',headers=HEADERS,json={'source_name':'旧名称','settings_revision':revision})
    assert result.status_code==409
    assert client.get('/api/settings').json()['source_name']=='新名称'


def test_checks_use_unsaved_input_and_failed_check_never_changes_running_source(connected,tmp_path):
    client,log=connected
    invalid=str(tmp_path/'missing.log')
    old=client.get('/api/settings').json()
    result=client.post('/api/source/check',headers=HEADERS,json={'log_path':invalid}).json()
    assert not result['ok'] and not result['checks']['log']
    result=client.post('/api/settings/check-save',headers=HEADERS,json={'log_path':invalid,'settings_revision':old['settings_revision']}).json()
    assert not result['saved']
    current=client.get('/api/settings').json()
    assert current['monitor_enabled'] and current['log_path']==str(log)
    assert current['settings_revision']==old['settings_revision']
    assert not (tmp_path/'missing.log').exists()


def test_check_save_switches_source_without_manual_stop(connected,tmp_path):
    client,_=connected
    new_log=tmp_path/'new.log'
    new_log.touch()
    assert client.patch('/api/settings',headers=HEADERS,json={'log_path':str(new_log)}).status_code==409
    result=client.post('/api/settings/check-save',headers=HEADERS,json={'log_path':str(new_log)}).json()
    assert result['ok'] and result['saved'] and result['settings']['monitor_enabled']
    assert result['settings']['log_path']==str(new_log)


def test_detection_returns_read_only_parameters_without_saving(connected):
    client,log=connected
    old=client.get('/api/settings').json()
    result=client.post('/api/source/detect',headers=HEADERS,json={}).json()
    assert result['ok'] and result['detected']['log_path']==str(log)
    assert client.get('/api/settings').json()==old


def test_delivery_pause_does_not_pause_monitor_or_change_sessions(connected):
    client,_=connected
    store=client.app.state.store
    with store.transaction() as db:
        db.execute("INSERT OR IGNORE INTO users(email) VALUES ('client-a')")
        db.execute("INSERT INTO sessions(email,state,started_at,last_online) VALUES ('client-a','online',1,2)")
    assert client.post('/api/delivery',headers=HEADERS,json={'paused':True}).status_code==200
    settings=store.settings()
    assert settings.monitor_enabled and settings.delivery_paused
    assert store.rows('SELECT state FROM sessions')[0]['state']=='online'


def test_resume_can_keep_or_ignore_backlog_without_deleting_history(connected):
    client,_=connected
    store=client.app.state.store
    with store.transaction() as db:
        for status in ('pending','retry','failed','sent','preview'):
            db.execute("INSERT INTO outbox(kind,text,created_at,status) VALUES ('online',?,1,?)",(status,status))
    client.post('/api/delivery',headers=HEADERS,json={'paused':True})
    client.post('/api/delivery',headers=HEADERS,json={'paused':False,'backlog':'keep'})
    assert [r['status'] for r in store.rows('SELECT status FROM outbox ORDER BY id')]==['pending','retry','failed','sent','preview']
    client.post('/api/delivery',headers=HEADERS,json={'paused':True})
    response=client.post('/api/delivery',headers=HEADERS,json={'paused':False,'backlog':'discard'}).json()
    assert response['skipped']==3
    assert [r['status'] for r in store.rows('SELECT status FROM outbox ORDER BY id')]==['skipped','skipped','skipped','sent','preview']
    assert store.settings().monitor_enabled


def test_snapshot_separates_collection_and_delivery_health(connected):
    client,_=connected
    data=client.get('/api/snapshot').json()
    assert data['delivery']['status']=='disabled'
    assert data['delivery']['pending']==0
    assert 'last_log_check' in data['health'] and 'last_inventory' in data['health']


def test_patch_does_not_control_monitor_or_delivery_switches(connected):
    client,_=connected
    result=client.patch('/api/settings',headers=HEADERS,json={'monitor_enabled':False,'delivery_paused':True})
    assert result.status_code==200
    assert result.json()['monitor_enabled'] and not result.json()['delivery_paused']


def test_destination_change_requires_delivery_pause_when_active(connected):
    client,_=connected
    store=client.app.state.store
    settings=store.settings()
    settings.telegram_enabled=True
    settings.telegram_token='123456:abcdefghijklmnopqrstuvwxyz'
    settings.telegram_chat_id='old-chat'
    store.save_settings(settings)
    result=client.patch('/api/settings',headers=HEADERS,json={'telegram_chat_id':'new-chat'})
    assert result.status_code==409 and store.settings().telegram_chat_id=='old-chat'
    client.post('/api/delivery',headers=HEADERS,json={'paused':True})
    result=client.patch('/api/settings',headers=HEADERS,json={'telegram_chat_id':'new-chat'})
    assert result.status_code==200 and store.settings().telegram_chat_id=='new-chat'


def test_bad_patch_never_echoes_secret(connected):
    client,_=connected
    result=client.patch('/api/settings',headers=HEADERS,json={'telegram_token':'private-value'*100})
    assert result.status_code==422 and 'private-value' not in result.text


def test_paused_delivery_collects_new_activity_and_enqueues_for_later(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    now=[datetime(2026,10,19,6,tzinfo=timezone.utc).timestamp()]
    monkeypatch.setattr('app.runtime.time.time',lambda:now[0])
    log=tmp_path/'access.log'
    log.touch()
    store=Store(tmp_path/'app')
    settings=Settings(source_type='panel',log_path=str(log),log_timezone='Etc/UTC',
        monitor_enabled=True,telegram_enabled=True,delivery_paused=True,source_name='测试节点')
    store.save_settings(settings)
    runtime=Runtime(store)
    online=[set()]
    async def onlines(_): return online[0]
    async def forbidden(*_): pytest.fail('Paused delivery sent a message')
    monkeypatch.setattr(runtime.panel,'onlines',onlines)
    monkeypatch.setattr('app.runtime.send_telegram',forbidden)
    asyncio.run(runtime.tick())
    now[0]+=10
    online[0]={'client-a'}
    log.write_text('2026/10/19 06:00:10 from 203.0.113.1:1234 accepted tcp:example.com:443 [in-a -> direct] email: client-a\n')
    asyncio.run(runtime.tick())
    asyncio.run(runtime.send_once())
    row=store.rows('SELECT text,status FROM outbox')[0]
    assert row['status']=='pending' and '🖥 节点：测试节点' in row['text']
    assert store.rows('SELECT requests,state FROM sessions')[0]=={'requests':1,'state':'online'}
    assert runtime.health['last_log_check']==now[0]
    assert runtime.delivery_health()['status']=='paused'
    asyncio.run(runtime.stop())
    store.close()


def test_queue_discard_while_geo_lookup_runs_prevents_late_send(tmp_path,monkeypatch):
    store=Store(tmp_path)
    store.save_settings(Settings(monitor_enabled=True,telegram_enabled=True,ip_location_enabled=True))
    with store.transaction() as db:
        db.execute("INSERT INTO outbox(kind,text,created_at,status,source_ip) VALUES ('online','🌐 IP：1.1.1.1',1,'pending','1.1.1.1')")
    runtime=Runtime(store)
    async def lookup(_):
        with store.transaction() as db:
            db.execute("UPDATE outbox SET status='skipped'")
        return '测试城市'
    async def forbidden(*_): pytest.fail('Discarded queue item sent after lookup')
    monkeypatch.setattr(runtime.geolocation,'lookup',lookup)
    monkeypatch.setattr('app.runtime.send_telegram',forbidden)
    asyncio.run(runtime.send_once())
    assert store.rows('SELECT status FROM outbox')[0]['status']=='skipped'
    store.close()
