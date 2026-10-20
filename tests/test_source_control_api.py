from fastapi.testclient import TestClient
from app.main import create_app
from app import source_control
import asyncio
import json
import time
from app.models import Settings
from app.runtime import Runtime
from app.storage import Store

HEADERS={'X-Requested-With':'session-watch'}


def test_auto_configuration_can_wait_for_delayed_log_without_starting_monitor(tmp_path,monkeypatch):
    control=tmp_path/'control';control.mkdir()
    async def waiting(settings):
        return {'ok':False,'message':'文件尚未生成', 'checks':{'inventory':True,'online':True,'log':False},
                'client_count':1,'online_count':0,'log_readable':False}
    monkeypatch.setenv('SESSION_WATCH_SOURCE_CONTROL',str(control))
    monkeypatch.setattr(source_control,'preview',waiting)
    with TestClient(create_app(tmp_path/'app',background=False),base_url='http://127.0.0.1') as client:
        client.post('/api/auth/setup',headers=HEADERS,json={'username':'admin','password':'test-password-12345'})
        result=client.post('/api/settings/check-save',headers=HEADERS,json={'local_target':'docker:example','log_mode':'auto'}).json()
        assert result['saved'] and not result['ok']
        assert client.app.state.store.settings().local_target=='docker:example'
        assert not client.app.state.store.settings().monitor_enabled
        assert client.post('/api/monitor',headers=HEADERS,json={'enabled':True}).status_code==400
        assert (control/'settings.json').is_file()


def test_failed_manual_preview_preserves_saved_source(tmp_path,monkeypatch):
    control=tmp_path/'control';control.mkdir()
    async def failed(settings):
        return {'ok':False,'message':'手动文件不存在','checks':{'inventory':True,'online':True,'log':False}}
    monkeypatch.setenv('SESSION_WATCH_SOURCE_CONTROL',str(control))
    monkeypatch.setattr(source_control,'preview',failed)
    with TestClient(create_app(tmp_path/'app',background=False),base_url='http://127.0.0.1') as client:
        client.post('/api/auth/setup',headers=HEADERS,json={'username':'admin','password':'test-password-12345'})
        before=client.app.state.store.settings()
        response=client.post('/api/settings/check-save',headers=HEADERS,json={'log_mode':'manual','log_path':'/var/log/x-ui/missing.log'})
        assert not response.json()['saved']
        assert client.app.state.store.settings()==before


def test_mirror_rotation_does_not_block_drain_for_same_source_generation(tmp_path,monkeypatch):
    control=tmp_path/'control';control.mkdir()
    log=tmp_path/'mirror.log';log.touch()
    status=tmp_path/'source.json'
    status.write_text(json.dumps({'format':1,'ready':True,'generation':'same-source','mirror_identity':[0,0]}))
    snapshot=tmp_path/'snapshot.json'
    snapshot.write_text(json.dumps({'version':1,'generated_at':time.time(),'clients_ok':True,'online_ok':True,
                                   'clients':[],'online':[],'source_generation':'same-source','source_id':'native'}))
    monkeypatch.setenv('SESSION_WATCH_SOURCE_CONTROL',str(control))
    monkeypatch.setenv('SESSION_WATCH_SOURCE_STATUS',str(status))
    monkeypatch.setenv('SESSION_WATCH_MANAGED_LOG',str(log))
    store=Store(tmp_path/'app')
    store.save_settings(Settings(local_snapshot_path=str(snapshot),monitor_enabled=True))
    runtime=Runtime(store)
    try:
        asyncio.run(runtime.tick())
        assert runtime.health['log']=='ok'
    finally:
        asyncio.run(runtime.stop());store.close()
