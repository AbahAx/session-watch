import json
import os
from pathlib import Path
import time

import pytest

from app import source_control
from app.models import Settings
from deploy import source_discovery as discovery
from deploy.source_agent import Agent, read_request


def test_multiple_sources_require_selection_and_never_guess():
    choices=[{'id':'native'},{'id':'docker:panel'}]
    with pytest.raises(ValueError,match='多个'):
        discovery.select_source(choices,'auto')
    assert discovery.select_source(choices,'docker:panel') == choices[1]
    with pytest.raises(ValueError,match='尚未'):
        discovery.select_source([], 'docker:panel')


@pytest.mark.parametrize('endpoint', ['/containers/a/exec', '/containers/create', '/images/json', '/containers/json?all=true'])
def test_docker_api_rejects_every_non_discovery_endpoint(endpoint):
    with pytest.raises(ValueError):
        discovery.docker_get('/unreachable.sock', endpoint)


@pytest.mark.parametrize('path', ['relative.log','/x/../etc/shadow','/x\nlog','/x\0log'])
def test_reject_unsafe_manual_paths(path):
    with pytest.raises(ValueError):
        discovery.safe_path(path)


def test_managed_reader_never_opens_manual_root_path(monkeypatch):
    monkeypatch.setenv('SESSION_WATCH_SOURCE_CONTROL','/control')
    monkeypatch.setenv('SESSION_WATCH_MANAGED_LOG','/mirror/access.log')
    settings=Settings(log_mode='manual',log_path='/root/access.log')
    assert source_control.effective_log_path(settings) == '/mirror/access.log'
    assert source_control.preferences(settings)['log_path'] == '/root/access.log'


def test_requests_do_not_export_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSION_WATCH_SOURCE_CONTROL',str(tmp_path))
    settings=Settings(panel_password='secret-panel-password',telegram_token='secret-token',log_mode='auto')
    source_control.publish_preferences(settings)
    data=(tmp_path/'settings.json').read_text()
    assert 'secret' not in data and 'password' not in data and 'telegram' not in data
    assert read_request(tmp_path/'settings.json') == {'target':'auto','log_mode':'auto','log_path':''}


@pytest.mark.skipif(os.name=='nt',reason='Linux source discovery uses POSIX paths and namespaces')
class TestPosixDiscovery:
    def source(self,tmp_path):
        config=tmp_path/'config.json'
        config.write_text(json.dumps({'log':{'access':'none'}}))
        return {'id':'native','label':'native','kind':'native','config':config,'cwd':str(tmp_path),'database':tmp_path/'db','binary':tmp_path/'xray'}

    def test_configured_relative_log_waits_then_appears(self,tmp_path):
        source=self.source(tmp_path)
        source['config'].write_text(json.dumps({'log':{'access':'./access.log'}}))
        with pytest.raises(ValueError,match='尚未生成'):
            discovery.log_source(source,{'log_mode':'auto'}, {})
        log=tmp_path/'access.log';log.write_bytes(b'')
        assert discovery.log_source(source,{'log_mode':'auto'}, {})[0] == log

    def test_manual_fixed_path_survives_auto_config_changes(self,tmp_path):
        source=self.source(tmp_path)
        manual=tmp_path/'manual.log';manual.touch()
        automatic=tmp_path/'auto.log';automatic.touch()
        source['config'].write_text(json.dumps({'log':{'access':str(automatic)}}))
        assert discovery.log_source(source,{'log_mode':'manual','log_path':str(manual)}, {})[0] == manual
        assert discovery.log_source(source,{'log_mode':'auto'}, {})[0] == automatic

    def test_manual_path_cannot_read_root_secrets_or_escape_symlinks(self,tmp_path):
        source=self.source(tmp_path)
        other=tmp_path.parent/'outside-secret.log';other.write_text('secret')
        link=tmp_path/'escape.log';link.symlink_to(other)
        with pytest.raises(ValueError,match='超出'):
            discovery.log_source(source,{'log_mode':'manual','log_path':str(link)}, {})
        with pytest.raises(ValueError,match='手动路径'):
            discovery.log_source(source,{'log_mode':'manual','log_path':'/etc/shadow'}, {})

    def test_docker_nested_file_mount_and_volume_mapping(self,tmp_path):
        container={'Mounts':[{'Type':'volume','Source':str(tmp_path/'volume'),'Destination':'/var/log'},
                             {'Type':'bind','Source':str(tmp_path/'single.log'),'Destination':'/var/log/x-ui/access.log'}]}
        assert discovery.mounted_path(container,'/var/log/x-ui/access.log') == tmp_path/'single.log'
        assert discovery.mounted_path(container,'/var/log/other.log') == tmp_path/'volume/other.log'
        assert discovery.mounted_path(container,'/var/logger/file.log') is None

    def test_docker_absolute_symlink_stays_in_container(self,tmp_path):
        volume=tmp_path/'volume';volume.mkdir()
        (volume/'alias.log').symlink_to('/logs/actual.log')
        actual=volume/'actual.log';actual.touch()
        container={'State':{'Pid':123},'Mounts':[{'Type':'bind','Source':str(volume),'Destination':'/logs'}]}
        assert discovery.container_path(container,'/logs/alias.log') == actual

    def test_agent_detects_disabled_then_enabled_then_relocated_logs(self,tmp_path,monkeypatch):
        source=self.source(tmp_path)
        monkeypatch.setattr('deploy.source_agent.candidates',lambda _: [source])
        monkeypatch.setattr('deploy.source_agent.collect_source',lambda _:dict(version=1,generated_at=time.time(),clients_ok=True,online_ok=True,clients=[],online=[]))
        agent=Agent({},tmp_path/'control',tmp_path/'snapshot',tmp_path/'mirror')
        preference={'target':'auto','log_mode':'auto','log_path':''}
        agent.refresh(preference)
        assert not (tmp_path/'mirror/heartbeat').exists()
        log=tmp_path/'access.log';log.write_bytes(b'historical\n')
        source['config'].write_text(json.dumps({'log':{'access':str(log)}}))
        agent.refresh(preference)
        assert agent.bridge and agent.bridge.output.read_bytes()==b''
        with log.open('ab') as f:f.write(b'new\n')
        agent.step()
        assert agent.bridge.output.read_bytes()==b'new\n'
        original=log.read_bytes()
        agent.close()
        with log.open('ab') as f:f.write(b'during-restart\n')
        resumed=Agent({},tmp_path/'control',tmp_path/'snapshot',tmp_path/'mirror')
        resumed.refresh(preference)
        assert resumed.bridge.output.read_bytes()==b'new\nduring-restart\n'
        other=tmp_path/'second.log';other.write_bytes(b'historical-other\n')
        source['config'].write_text(json.dumps({'log':{'access':str(other)}}))
        resumed.refresh(preference)
        assert resumed.bridge.output.read_bytes()==b''
        assert log.read_bytes()==original+b'during-restart\n' and other.read_bytes()==b'historical-other\n'
        resumed.close()

    def test_agent_manual_preview_never_switches_active_mirror(self,tmp_path,monkeypatch):
        source=self.source(tmp_path)
        log=tmp_path/'access.log';log.touch()
        source['config'].write_text(json.dumps({'log':{'access':str(log)}}))
        monkeypatch.setattr('deploy.source_agent.candidates',lambda _: [source])
        monkeypatch.setattr('deploy.source_agent.collect_source',lambda _:dict(version=1,generated_at=time.time(),clients_ok=True,online_ok=True,clients=[],online=[]))
        agent=Agent({},tmp_path/'control',tmp_path/'snapshot',tmp_path/'mirror')
        agent.refresh({'target':'auto','log_mode':'auto','log_path':''})
        before=agent.identity
        missing=agent.inspect({'target':'native','log_mode':'manual','log_path':str(tmp_path/'missing.log')},[source])
        assert not missing['ok'] and agent.identity==before
        agent.close()

    def test_request_symlink_rejected_and_status_group_readable_under_strict_umask(self,tmp_path):
        original=tmp_path/'original';original.write_text('{}')
        link=tmp_path/'link';link.symlink_to(original)
        with pytest.raises(OSError):read_request(link)
        old=os.umask(0o077)
        try:source_control.atomic_json(tmp_path/'source.json',{'ready':True})
        finally:os.umask(old)
        assert (tmp_path/'source.json').stat().st_mode & 0o777 == 0o640
