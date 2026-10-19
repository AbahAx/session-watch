import asyncio
from datetime import datetime, timezone

from app.models import Settings
from app.runtime import InstanceLock, Runtime
from app.storage import Store
from app.integrations import IntegrationError


def test_complete_pipeline_log_to_online_to_five_minute_summary(tmp_path, monkeypatch):
    log = tmp_path / 'access.log'
    log.write_bytes(b'')
    store = Store(tmp_path / 'appdata')
    store.save_settings(Settings(source_type='panel', monitor_enabled=True, log_path=str(log)))
    runtime = Runtime(store)
    now = [datetime(2026, 10, 19, 4, 30, tzinfo=timezone.utc).timestamp()]
    online = [set()]
    monkeypatch.setattr('app.runtime.time.time', lambda: now[0])
    async def fake_panel(settings):
        return online[0]
    monkeypatch.setattr(runtime.panel, 'onlines', fake_panel)

    async def run():
        await runtime.tick()  # Empty initial baseline; attach at EOF.
        now[0] += 10
        online[0] = {'lin'}
        with log.open('a') as f:
            f.write('2026/10/19 12:30:10 from 203.0.113.10:1234 accepted tcp:example.com:443 [in -> direct] email: lin\n')
        before = log.read_bytes()
        await runtime.tick()
        assert [r['kind'] for r in store.rows('SELECT kind FROM outbox')] == ['online']
        assert 'example.com' in store.rows('SELECT text FROM outbox')[0]['text']
        online[0] = set()
        for _ in range(30):
            now[0] += 10
            await runtime.tick()
        assert len(store.rows('SELECT * FROM outbox')) == 1
        now[0] += 10
        await runtime.tick()
        rows = store.rows('SELECT * FROM outbox ORDER BY id')
        assert [r['kind'] for r in rows] == ['online', 'offline']
        assert '请求：1' in rows[1]['text'] and '5 分钟' in rows[1]['text']
        assert log.read_bytes() == before
        await runtime.stop()
    asyncio.run(run())
    store.close()


def test_single_instance_lock_prevents_duplicate_workers(tmp_path):
    a, b = InstanceLock(tmp_path), InstanceLock(tmp_path)
    a.acquire()
    import pytest
    try:
        with pytest.raises(RuntimeError):
            b.acquire()
    finally:
        a.release()
    b.acquire()
    b.release()


def test_queued_messages_and_key_survive_restart(tmp_path):
    store = Store(tmp_path)
    config = Settings(panel_password='private-password')
    store.save_settings(config)
    with store.transaction() as db:
        db.execute("INSERT INTO outbox(kind,text,created_at,status) VALUES ('online','saved',1,'pending')")
    key = (tmp_path / 'secret.key').read_bytes()
    store.close()
    reopened = Store(tmp_path)
    assert reopened.settings().panel_password == 'private-password'
    assert reopened.rows('SELECT text,status FROM outbox')[0] == {'text':'saved','status':'pending'}
    assert (tmp_path / 'secret.key').read_bytes() == key
    reopened.close()
