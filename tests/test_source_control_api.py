from fastapi.testclient import TestClient
from app.main import create_app
from app import source_control

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
