import asyncio
import json
import os
import time
from pathlib import Path

from app.engine import Engine
from app.integrations import DeliveryError, IntegrationError, PanelClient, send_telegram
from app.logs import LogReader
from app.local_source import read_snapshot, sync_clients, online_set
from app.geolocation import GeoLocator, with_location


class Runtime:
    def __init__(self, store):
        self.store = store
        self.engine = Engine(store)
        self.reader = LogReader()
        self.panel = PanelClient()
        self.geolocation = GeoLocator(store)
        self.guard = asyncio.Lock()
        self.delivery_guard = asyncio.Lock()
        self.health = {"panel": "idle", "log": "idle", "inventory": "idle", "last_inventory": None,
                       "last_log_check": None, "log_detail": "等待读取", "last_poll": None, "last_log": None,
                       "parsed": 0, "skipped": 0, "detail": "等待采集", "clients": 0}
        self.tasks = []
        self.closed = False

    async def start(self):
        # An unobserved process downtime never counts as confirmed offline.
        with self.store.transaction() as db:
            self.engine.poll(db, None, time.time(), self.store.settings())
        self.tasks = [asyncio.create_task(self.collect_loop()), asyncio.create_task(self.sender_loop())]

    async def stop(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.reader.close()
        await self.panel.close()

    async def collect_loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health.update(panel="error", detail="采集异常，请检查配置与日志格式")
                self.store.diagnostic(time.time(), "error", "采集异常，已将本轮状态置为未知")
                with self.store.transaction() as db:
                    self.engine.poll(db, None, time.time(), self.store.settings())
            await asyncio.sleep(self.store.settings().poll_seconds)

    async def tick(self):
        async with self.guard:
            settings = self.store.settings()
            from app import source_control
            source_control.publish_preferences(settings)
            local = None
            local_error = None
            if settings.source_type == "local":
                try:
                    local = read_snapshot(settings)
                    with self.store.transaction() as db:
                        sync_clients(db, local)
                    self.health['clients'] = len(local['clients']) if local['clients_ok'] else self.health['clients']
                    self.health['inventory'] = 'ok' if local['clients_ok'] else 'error'
                    if local['clients_ok']:
                        self.health['last_inventory'] = local['generated_at']
                except IntegrationError as exc:
                    local_error = str(exc)
                    self.health['inventory'] = 'error'
            else:
                self.health['inventory'] = 'remote'
            if not settings.monitor_enabled:
                if local is not None and not local['clients_ok']:
                    local_error = '本地客户端数据库不可读或版本不兼容'
                self.health.update(panel="paused", log="paused", detail=local_error or
                                   ("监控已暂停；本地客户端清单仍自动同步" if settings.source_type == 'local' else "监控已暂停"))
                return
            logs_ready = True
            try:
                # Read bounded batches, checkpointing with the event transaction.
                for _ in range(20):
                    cursor = self.store.get("log_cursor")
                    events, cursor, logs_ready, skipped = self.reader.read(source_control.effective_log_path(settings), cursor, settings.log_timezone)
                    with self.store.transaction() as db:
                        self.engine.ingest(db, events, time.time())
                        db.execute("INSERT INTO kv VALUES ('log_cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cursor),))
                    self.health["parsed"] += len(events)
                    self.health["skipped"] += skipped
                    if events:
                        self.health["last_log"] = max(e.at for e in events)
                    if logs_ready:
                        break
                self.health["log"] = "ok" if logs_ready else "catching_up"
                self.health['log_detail'] = '读取正常，等待新访问' if not events else '已读取新增访问记录'
                heartbeat = os.environ.get("SESSION_WATCH_BRIDGE_HEARTBEAT")
                if heartbeat and time.time() - Path(heartbeat).stat().st_mtime > 20:
                    self.health["log"] = "error"
                    self.health['log_detail'] = '日志桥接心跳过期'
                if self.health['log'] != 'error':
                    self.health['last_log_check'] = time.time()
            except (OSError, ValueError):
                self.health["log"] = "error"
                self.health['log_detail'] = '日志文件不可读，请检查路径或采集服务'
                # Status still works without logs; summary fields will be missing.
                logs_ready = True
            now = time.time()
            try:
                if settings.source_type == 'local':
                    if local is None:
                        raise IntegrationError(local_error)
                    online = online_set(local)
                else:
                    online = await self.panel.onlines(settings)
                self.health.update(panel="ok", last_poll=now, detail="状态查询正常" if self.health["log"] == "ok" else "状态可用；访问日志不可用或正在追赶，访问统计可能不完整")
            except IntegrationError as exc:
                online = None
                self.health.update(panel="error", detail=str(exc))
            with self.store.transaction() as db:
                if self.health["log"] == "error":
                    db.execute("UPDATE sessions SET stats_incomplete=1 WHERE ended_at IS NULL")
                self.engine.poll(db, online, now, settings, logs_ready=logs_ready)
                if self.health["log"] == "error":
                    db.execute("UPDATE sessions SET stats_incomplete=1 WHERE ended_at IS NULL")
                self.engine.housekeeping(db, now, settings)

    async def sender_loop(self):
        while True:
            try:
                await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.store.diagnostic(time.time(), "error", "推送任务异常，消息已保留，等待重试")
            await asyncio.sleep(1)

    async def send_once(self):
        settings = self.store.settings()
        if not settings.telegram_enabled or not settings.monitor_enabled or settings.delivery_paused:
            return
        rows = self.store.rows("""SELECT * FROM outbox o WHERE o.status IN ('pending','retry') AND o.next_try<=?
          AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.session_id=o.session_id AND p.id<o.id AND p.status IN ('pending','retry','failed'))
          ORDER BY o.id LIMIT 1""", (time.time(),))
        if not rows:
            return
        row = rows[0]
        try:
            location = await self.geolocation.lookup(row['source_ip']) if settings.ip_location_enabled else None
            # A pause/settings change while awaiting the provider must not send.
            async with self.delivery_guard:
                settings = self.store.settings()
                latest = self.store.rows('SELECT status FROM outbox WHERE id=?', (row['id'],))
                if (not settings.monitor_enabled or not settings.telegram_enabled or settings.delivery_paused or
                        not latest or latest[0]['status'] not in ('pending', 'retry')):
                    return
                text = with_location(row['text'], location if settings.ip_location_enabled else None)
                with self.store.transaction() as db:
                    db.execute('UPDATE outbox SET text=? WHERE id=?', (text, row['id']))
                await send_telegram(settings, text)
                with self.store.transaction() as db:
                    db.execute("UPDATE outbox SET status='sent',sent_at=?,error='',attempts=attempts+1 WHERE id=?", (time.time(), row["id"]))
        except DeliveryError as exc:
            delay = exc.retry_after or min(3600, 5 * 2 ** min(row["attempts"], 10))
            with self.store.transaction() as db:
                db.execute("UPDATE outbox SET status=?,attempts=attempts+1,next_try=?,error=? WHERE id=?", ("failed" if exc.permanent else "retry", time.time() + delay, str(exc), row["id"]))

    def delivery_health(self):
        settings = self.store.settings()
        counts = self.store.rows("""SELECT SUM(CASE WHEN status IN ('pending','retry') THEN 1 ELSE 0 END) AS pending,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            MIN(CASE WHEN status IN ('pending','retry','failed') THEN created_at END) AS oldest,
            MAX(sent_at) AS last_success FROM outbox""")[0]
        last_success = max(counts['last_success'] or 0, self.store.get('telegram_test_success', 0)) or None
        pending, failed = counts['pending'] or 0, counts['failed'] or 0
        status = ('disabled' if not settings.telegram_enabled else 'paused' if settings.delivery_paused else
                  'stopped' if not settings.monitor_enabled else 'error' if failed else 'queued' if pending else 'ok')
        errors = self.store.rows("SELECT error FROM outbox WHERE status IN ('failed','retry') ORDER BY created_at DESC LIMIT 1")
        return {'status': status, 'pending': pending, 'failed': failed, 'oldest': counts['oldest'],
                'last_success': last_success, 'last_error': errors[0]['error'] if errors else ''}


class InstanceLock:
    """One collector/sender per data directory, including multi-worker accidents."""
    def __init__(self, directory):
        self.path = directory / "instance.lock"
        self.file = None

    def acquire(self):
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("该数据目录已有实例运行；请使用单 worker") from None

    def release(self):
        if self.file:
            self.file.close()
