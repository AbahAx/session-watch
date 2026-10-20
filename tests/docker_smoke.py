"""Opt-in real Docker/systemd integration on a disposable CI runner only.

Uses the upstream 3x-ui image with a fixture DB/config and its real Xray core.
Never run against a production container. No host ports are published.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from deploy.source_agent import Agent
from deploy.source_discovery import candidates, log_source, mounted_path, select_source


def run(args, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=240)
    if check and result.returncode:
        raise RuntimeError('Fixture command failed: ' + str(args[:3]) + '\n' + result.stderr[-2000:])
    return result


def wait(condition, description, timeout=40):
    deadline=time.time()+timeout
    while time.time()<deadline:
        if condition():return
        time.sleep(1)
    raise AssertionError('Timed out: '+description)


def main(package):
    assert os.geteuid()==0 and Path('/run/systemd/system').is_dir()
    name='sw-ci-'+secrets.token_hex(5)
    image='ghcr.io/mhsanaei/3x-ui:latest'
    volume=name+'-logs'
    instance='dockersmoke'
    assert not Path('/etc/session-watch-'+instance).exists()
    run(['docker','pull',image])
    print('3x-ui fixture image:',run(['docker','image','inspect',image,'--format','{{.Id}}']).stdout.strip(),flush=True)
    with tempfile.TemporaryDirectory(prefix='sw-docker-fixture-') as temporary:
        base=Path(temporary);db=base/'db';db.mkdir();config=base/'config.json'
        with sqlite3.connect(db/'x-ui.db') as connection:
            connection.executescript('CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT,enable INTEGER); CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER); CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT); INSERT INTO clients VALUES(1,"fixture",1);')
        value={'log':{'access':'none','loglevel':'warning'},'api':{'tag':'api','services':['StatsService']},
               'stats':{},'policy':{'levels':{'0':{'statsUserOnline':True}}},
               'inbounds':[{'tag':'api','listen':'127.0.0.1','port':62789,'protocol':'dokodemo-door','settings':{'address':'127.0.0.1'}}],
               'outbounds':[{'protocol':'freedom','tag':'direct'}],
               'routing':{'rules':[{'type':'field','inboundTag':['api'],'outboundTag':'api'}]}}
        config.write_text(json.dumps(value))
        def create(mount=None):
            args=['docker','run','--detach','--name',name,'--entrypoint','/app/bin/xray-linux-amd64',
                  '--mount',f'type=bind,src={db},dst=/etc/x-ui',
                  '--mount',f'type=bind,src={config},dst=/fixture/config.json,readonly']
            if mount:args+=['--mount',mount]
            args += [image,'run','-config','/fixture/config.json']
            run(args)
            wait(lambda: bool([c for c in candidates({}) if c['id']=='docker:'+name]),'Docker discovery')
        control=base/'control';control.mkdir()
        agent=Agent({},control,base/'snapshot',base/'mirror')
        preference={'target':'docker:'+name,'log_mode':'auto','log_path':''}
        installed=False
        try:
            create()
            agent.refresh(preference)
            assert not (base/'mirror/heartbeat').exists()
            value['log']['access']='./access.log';config.write_text(json.dumps(value))
            run(['docker','restart',name])
            wait(lambda: bool([c for c in candidates({}) if c['id']=='docker:'+name and c['process']]),'running core')
            def ready():
                agent.refresh(preference)
                return json.loads((base/'snapshot/source.json').read_text())['ready']
            wait(ready,'container-internal relative log')
            source=select_source(candidates({}),'docker:'+name)
            path,_=log_source(source,preference,{})
            assert mounted_path(source['container'],'/app/access.log') is None
            before=path.read_bytes()
            run(['docker','exec',name,'sh','-c','printf "fixture-new-line\\n" >> /app/access.log'])
            agent.step()
            assert agent.bridge.output.read_bytes()==b'fixture-new-line\n'
            assert path.read_bytes()==before+b'fixture-new-line\n'
            probe=agent.inspect(preference,candidates({}))
            assert probe['ok'],probe
            manual={**preference,'log_mode':'manual','log_path':'docker://'+name+'/app/access.log'}
            assert agent.inspect(manual,candidates({}))['ok']
            print('PASS Docker: disabled -> enabled, internal relative file, read-only mirror, namespace online query and manual URI.',flush=True)
            original_pid=source['container']['State']['Pid']
            agent.close();run(['docker','rm','--force',name])
            create()
            wait(ready,'recreated container')
            assert select_source(candidates({}),'docker:'+name)['container']['State']['Pid']!=original_pid
            print('PASS Docker: same-name container recreation automatically resolves new process/root.',flush=True)
            agent.close();run(['docker','rm','--force',name])
            run(['docker','volume','create',volume])
            value['log']['access']='/app/logs/access.log';config.write_text(json.dumps(value))
            create('type=volume,src='+volume+',dst=/app/logs')
            wait(ready,'named-volume log')
            source=select_source(candidates({}),'docker:'+name)
            assert log_source(source,preference,{})[0]==mounted_path(source['container'],'/app/logs/access.log')
            print('PASS Docker: named volume mapping resolved to its current host source.',flush=True)
            agent.close()
            # Verify the *actual generated service sandbox*, IPC and web preview.
            run(['bash',str(package/'install.sh'),'install','--non-interactive','--instance',instance,
                 '--port','18766','--no-install-deps'])
            installed=True
            status=Path('/var/lib/session-watch-'+instance+'-local/source.json')
            try:
                wait(lambda: status.is_file() and json.loads(status.read_text()).get('ready'),'sandboxed discovery')
            except AssertionError:
                print('Sandbox status:',status.read_text() if status.exists() else 'missing',flush=True)
                print(run(['journalctl','-u','session-watch-'+instance+'-local.service','-n','30','--no-pager']).stdout,flush=True)
                raise
            snapshot=json.loads(status.with_name('snapshot.json').read_text())
            assert snapshot['clients_ok'] and snapshot['online_ok']
            request=Path('/var/lib/session-watch-'+instance+'-control/preview.json')
            request.write_text(json.dumps({**manual,'log_path':'/app/logs/access.log','request_id':'a'*32,'at':time.time()}))
            response=status.with_name('preview.json')
            wait(lambda: response.is_file(),'sandboxed preview')
            assert json.loads(response.read_text())['result']['ok']
            print('PASS Docker: installed systemd sandbox, permissions, persistent discovery and manual preview IPC.',flush=True)
        finally:
            agent.close()
            marker=Path('/etc/session-watch-'+instance+'/install.json')
            if marker.exists():
                run(['session-watch-'+instance,'uninstall','--purge'])
            run(['docker','rm','--force',name],check=False)
            run(['docker','volume','rm',volume],check=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--package',type=Path,required=True)
    main(parser.parse_args().package.resolve())
