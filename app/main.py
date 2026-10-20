import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, ValidationError

from app import __version__
from app.engine import render_message
from app.integrations import DeliveryError, IntegrationError, PanelClient, send_telegram
from app.models import AccountUpdate, Credentials, Settings, UserUpdate
from app.local_source import read_snapshot, sync_clients, online_set
from app.runtime import InstanceLock, Runtime
from app.storage import SECRET_FIELDS, Store, password_hash, password_matches, token_hash
from app.configuration import SOURCE_FIELDS, inspect_source
from app import source_control

ROOT = Path(__file__).resolve().parent.parent


def create_app(data_dir: Path | None = None, background=True):
    directory = data_dir or Path(os.environ.get("SESSION_WATCH_DATA", ROOT / "data"))
    store = Store(directory)
    runtime = Runtime(store)
    lock = InstanceLock(directory)
    login_attempts: dict[str, list[float]] = {}
    account_attempts: dict[str, list[float]] = {}
    secure_cookie = os.environ.get("SESSION_WATCH_SECURE_COOKIE", "0") == "1"

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        try:
            source_control.publish_preferences(store.settings())
            if background:
                await runtime.start()
            yield
        finally:
            await runtime.stop()
            lock.release()
            store.close()

    app = FastAPI(title="Session Watch", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    hosts = os.environ.get("SESSION_WATCH_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(",")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in hosts if h.strip()])
    app.state.store = store
    app.state.runtime = runtime

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default errors echo `input`, including passwords/tokens.
        return JSONResponse(status_code=422, content={"detail": [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]})

    @app.middleware("http")
    async def headers_and_csrf(request: Request, call_next):
        if request.url.path.startswith("/api") and request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("X-Requested-With") != "session-watch":
                return JSONResponse({"detail": "请求校验失败，请从管理页面操作"}, status_code=403)
            origin = request.headers.get("origin")
            expected_origin = os.environ.get("SESSION_WATCH_PUBLIC_ORIGIN") or str(request.base_url)
            if origin and origin.rstrip("/") != expected_origin.rstrip("/"):
                return JSONResponse({"detail": "禁止跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def logged_in(request: Request):
        token = request.cookies.get("sw_session", "")
        if not token:
            return False
        rows = store.rows("SELECT expires_at FROM admin_sessions WHERE token_hash=?", (token_hash(token),))
        return bool(rows and rows[0]["expires_at"] > time.time())

    def require_auth(request: Request):
        if not logged_in(request):
            raise HTTPException(401, "请先登录")

    def dataset():
        return store

    def make_session(response):
        token = secrets.token_urlsafe(32)
        with store.transaction() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires_at<?", (time.time(),))
            db.execute("INSERT INTO admin_sessions VALUES (?,?)", (token_hash(token), time.time() + 43200))
        response.set_cookie("sw_session", token, httponly=True, secure=secure_cookie, samesite="strict", max_age=43200, path="/")

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"initialized": store.get("admin") is not None, "authenticated": logged_in(request)}

    @app.get('/api/auth/account', dependencies=[Depends(require_auth)])
    def account():
        return {'username': store.get('admin')['username']}

    @app.post("/api/auth/setup")
    def setup(body: Credentials, request: Request, response: Response):
        # Claim first-run setup only from loopback; remote installations must use
        # an SSH tunnel (forwarded client is loopback) or the local CLI.
        if request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "首次初始化请通过服务器本机或 SSH 隧道访问")
        with store.transaction() as db:
            if store.get("admin") is not None:
                raise HTTPException(409, "管理员已创建")
            store.set("admin", {"username": body.username, "password": password_hash(body.password)})
        make_session(response)
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        now = time.time()
        # Bounded per-IP throttling; no credentials enter diagnostics.
        if len(login_attempts) > 1000:
            login_attempts.clear()
        attempts = [t for t in login_attempts.get(address, []) if now - t < 600]
        if len(attempts) >= 5:
            raise HTTPException(429, "尝试过于频繁，请 10 分钟后重试")
        admin = store.get("admin")
        if not admin or body.username != admin["username"] or not password_matches(body.password, admin["password"]):
            attempts.append(now)
            login_attempts[address] = attempts
            raise HTTPException(401, "账号或密码错误")
        login_attempts.pop(address, None)
        make_session(response)
        return {"ok": True}

    @app.post("/api/auth/logout", dependencies=[Depends(require_auth)])
    def logout(request: Request, response: Response):
        with store.transaction() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(request.cookies.get("sw_session", "")),))
        response.delete_cookie("sw_session", path="/")
        return {"ok": True}

    @app.get("/api/settings", dependencies=[Depends(require_auth)])
    def get_settings():
        return store.public_settings()

    @app.put("/api/settings", dependencies=[Depends(require_auth)])
    async def update_settings(body: Settings):
        return await save_changes(body.model_dump())

    def candidate_settings(changes):
        old = store.settings()
        revision = changes.get('settings_revision')
        if revision is not None and revision != store.get('settings_revision', 0):
            raise HTTPException(409, '配置已被其他操作更新，请先重新载入；未保存的输入仍保留在页面')
        values = old.model_dump()
        for key, value in changes.items():
            if key not in Settings.model_fields or key in {'monitor_enabled', 'delivery_paused'}:
                continue
            if key in SECRET_FIELDS and not value:
                continue
            values[key] = value
        try:
            candidate = Settings(**values)
        except ValidationError as exc:
            raise HTTPException(422, [{'loc': e['loc'], 'msg': e['msg']} for e in exc.errors()]) from None
        return old, candidate

    async def save_changes(changes, check=False):
        async with runtime.guard:
            old, candidate = candidate_settings(changes)
            changed = {field for field in SOURCE_FIELDS if getattr(old, field) != getattr(candidate, field)}
            if old.monitor_enabled and changed and not check:
                raise HTTPException(409, '采集来源发生变化，请使用“检查并保存”；普通设置可以直接保存')
            destination_changed = any(getattr(old, f) != getattr(candidate, f) for f in ('telegram_token', 'telegram_chat_id'))
            if destination_changed and old.monitor_enabled and old.telegram_enabled and not old.delivery_paused:
                raise HTTPException(409, '修改 Telegram 接收配置前，请先暂停推送；活动统计可继续运行')
            if candidate.monitor_enabled and candidate.telegram_enabled and not all([candidate.telegram_token, candidate.telegram_chat_id]):
                raise HTTPException(400, '请填写 Telegram 配置，或关闭实际发送')
            result = await inspect_source(candidate) if check else None
            waiting = (result is not None and not result['ok'] and source_control.managed() and
                       not old.monitor_enabled and candidate.source_type == 'local' and candidate.log_mode == 'auto' and
                       result['checks']['inventory'] and result['checks']['online'] and not result['checks']['log'])
            if result is not None and not result['ok'] and not waiting:
                return {**result, 'saved': False}
            if waiting:
                result['message'] += '；自动检测配置已保存，文件可用后会自行接入'
            async with runtime.delivery_guard:
                store.save_settings(candidate)
                source_control.publish_preferences(candidate)
                if changed:
                    runtime.reader.close()
                    await runtime.panel.close()
                    with store.transaction() as db:
                        runtime.engine.poll(db, None, time.time(), candidate)
                        db.execute('UPDATE sessions SET stats_incomplete=1 WHERE ended_at IS NULL')
            return ({**result, 'saved': True, 'settings': store.public_settings()} if check else store.public_settings())

    @app.patch('/api/settings', dependencies=[Depends(require_auth)])
    async def patch_settings(body: dict):
        return await save_changes(body)

    @app.post('/api/settings/check-save', dependencies=[Depends(require_auth)])
    async def check_save_settings(body: dict):
        return await save_changes(body, check=True)

    class MonitorSwitch(BaseModel):
        enabled: bool

    @app.post("/api/monitor", dependencies=[Depends(require_auth)])
    async def monitor(body: MonitorSwitch):
        async with runtime.guard, runtime.delivery_guard:
            settings = store.settings()
            if body.enabled:
                if settings.source_type == 'local' and source_control.managed():
                    result = await inspect_source(settings)
                    if not result['ok']:
                        raise HTTPException(400, result['message'])
                if settings.source_type == 'local':
                    try:
                        data = read_snapshot(settings)
                        online_set(data)
                        with store.transaction() as db:
                            sync_clients(db, data)
                    except IntegrationError as exc:
                        raise HTTPException(400, str(exc)) from None
                elif not all([settings.panel_url, settings.panel_username, settings.panel_password]):
                    raise HTTPException(400, "请先配置面板连接和日志路径")
                if not source_control.effective_log_path(settings):
                    raise HTTPException(400, "请先配置日志路径")
                if settings.telegram_enabled and not all([settings.telegram_token, settings.telegram_chat_id]):
                    raise HTTPException(400, "请先填写 Telegram 配置，或关闭实际发送")
            settings.monitor_enabled = body.enabled
            store.save_settings(settings)
            with store.transaction() as db:
                runtime.engine.poll(db, None, time.time(), settings)
            if not body.enabled:
                await runtime.panel.close()
                runtime.reader.close()
            return {"ok": True, "enabled": body.enabled}

    class DeliverySwitch(BaseModel):
        paused: bool
        backlog: str = Field(default='keep', pattern='^(keep|discard)$')

    @app.post('/api/delivery', dependencies=[Depends(require_auth)])
    async def delivery(body: DeliverySwitch):
        async with runtime.guard, runtime.delivery_guard:
            settings = store.settings()
            skipped = 0
            if not body.paused and body.backlog == 'discard':
                with store.transaction() as db:
                    skipped = db.execute("UPDATE outbox SET status='skipped',error='恢复推送时已忽略积压提醒' WHERE status IN ('pending','retry','failed')").rowcount
            settings.delivery_paused = body.paused
            store.save_settings(settings)
            return {'ok': True, 'paused': body.paused, 'skipped': skipped}

    @app.post("/api/source/check", dependencies=[Depends(require_auth)])
    async def check_source(body: dict | None = None):
        _, settings = candidate_settings(body or {})
        result = await inspect_source(settings)
        # Preserve the old endpoint's inventory sync only for saved settings.
        if not body and result['ok'] and settings.source_type == 'local':
            with store.transaction() as db:
                sync_clients(db, read_snapshot(settings))
        return result

    @app.post('/api/source/detect', dependencies=[Depends(require_auth)])
    async def detect_source():
        settings = store.settings()
        if source_control.managed():
            candidate = settings.model_copy(update={'log_mode': 'auto'})
            result = await inspect_source(candidate)
            metadata = source_control.status() or {}
            return {**result, 'source_status': metadata,
                    'detected': {'source_type': 'local', 'log_mode': 'auto', 'local_target': candidate.local_target},
                    'message': result['message'] + '；自动模式会持续检测，日志生成后自动接入'}
        snapshots = list(dict.fromkeys([settings.local_snapshot_path, Settings().local_snapshot_path]))
        logs = [settings.log_path, '/var/lib/session-watch-bridge/access.log']
        heartbeat = os.environ.get('SESSION_WATCH_BRIDGE_HEARTBEAT')
        if heartbeat:
            logs.insert(0, str(Path(heartbeat).parent / 'access.log'))
        for snapshot_path in snapshots:
            for log_path in dict.fromkeys(logs):
                if not log_path:
                    continue
                candidate = settings.model_copy(update={'source_type': 'local', 'local_snapshot_path': snapshot_path, 'log_path': log_path})
                result = await inspect_source(candidate)
                if result['ok']:
                    return {**result, 'detected': {'source_type': 'local', 'local_snapshot_path': snapshot_path, 'log_path': log_path},
                            'message': result['message'] + '。已找到本机接入参数，确认日志时区后检查并保存'}
        return {'ok': False, 'message': '未找到健康的本机采集器与日志，请先安装或检查本地采集服务'}

    @app.put("/api/users/{email:path}", dependencies=[Depends(require_auth)])
    def update_user(email: str, body: UserUpdate):
        target = dataset()
        with target.transaction() as db:
            if not db.execute("SELECT email FROM users WHERE email=?", (email,)).fetchone():
                raise HTTPException(404, "用户不存在")
            db.execute("UPDATE users SET alias=?,monitored=? WHERE email=?", (body.alias.strip(), int(body.monitored), email))
            if not body.monitored:
                db.execute("UPDATE sessions SET state='offline',ended_at=COALESCE(offline_since,last_online) WHERE email=? AND ended_at IS NULL", (email,))
                db.execute("UPDATE users SET baseline='baseline' WHERE email=?", (email,))
        return {"ok": True}

    @app.get("/api/sessions/{session_id}", dependencies=[Depends(require_auth)])
    def session_detail(session_id: int):
        target = dataset()
        rows = target.rows("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not rows:
            raise HTTPException(404, "活动不存在")
        session = rows[0]
        session["targets"] = target.rows("SELECT value,count FROM stats WHERE session_id=? AND kind='target' ORDER BY count DESC,value LIMIT 500", (session_id,))
        session["ips"] = target.rows("SELECT value,count FROM stats WHERE session_id=? AND kind='ip' ORDER BY count DESC", (session_id,))
        with target.lock:
            session["online_message"] = render_message(target.db, session, "online", store.settings())
            session["offline_message"] = render_message(target.db, session, "offline", store.settings())
        return session

    @app.post("/api/notifications/{notification_id}/retry", dependencies=[Depends(require_auth)])
    def retry(notification_id: int):
        with store.transaction() as db:
            row = db.execute("SELECT status FROM outbox WHERE id=?", (notification_id,)).fetchone()
            if not row or row[0] not in ("retry", "failed"):
                raise HTTPException(400, "该消息不需要重试")
            db.execute("UPDATE outbox SET status='pending',next_try=0,error='' WHERE id=?", (notification_id,))
        return {"ok": True}

    @app.post("/api/telegram/test", dependencies=[Depends(require_auth)])
    async def telegram_test():
        settings = store.settings()
        if not settings.telegram_enabled:
            raise HTTPException(400, "请开启 Telegram 发送后再测试")
        try:
            async with runtime.delivery_guard:
                settings = store.settings()
                if not settings.telegram_enabled:
                    raise HTTPException(400, '请开启 Telegram 发送后再测试')
                await send_telegram(settings, "✅ Session Watch 测试消息\n机器人推送配置正常。")
            store.set('telegram_test_success', time.time())
        except DeliveryError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"ok": True, "message": "测试消息已发送"}

    class ChangePassword(BaseModel):
        current_password: str = Field(max_length=256)
        new_password: str = Field(min_length=12, max_length=256)

    def update_account(current_password, username, new_password, request, response):
        address = request.client.host if request.client else 'unknown'
        now = time.time()
        # One transaction changes credentials and revokes every previous session.
        # Concurrent changes must re-check the latest password under the lock.
        with store.transaction() as db:
            if len(account_attempts) > 1000:
                account_attempts.clear()
            attempts = [t for t in account_attempts.get(address, []) if now - t < 600]
            if len(attempts) >= 5:
                raise HTTPException(429, '验证过于频繁，请 10 分钟后重试')
            admin = store.get('admin')
            if not password_matches(current_password, admin['password']):
                account_attempts[address] = [*attempts, now]
                raise HTTPException(400, '当前密码错误')
            changed = (username is not None and username != admin['username']) or bool(new_password)
            if changed:
                if username is not None:
                    admin['username'] = username
                if new_password:
                    admin['password'] = password_hash(new_password)
                db.execute("UPDATE kv SET value=? WHERE key='admin'", (json.dumps(admin, ensure_ascii=False),))
                db.execute('DELETE FROM admin_sessions')
            account_attempts.pop(address, None)
        if changed:
            make_session(response)
        return {'ok': True, 'username': admin['username'], 'changed': changed}

    @app.put('/api/auth/account', dependencies=[Depends(require_auth)])
    def change_account(body: AccountUpdate, request: Request, response: Response):
        return update_account(body.current_password, body.username, body.new_password, request, response)

    @app.post("/api/auth/password", dependencies=[Depends(require_auth)])
    def change_password(body: ChangePassword, request: Request, response: Response):
        return update_account(body.current_password, None, body.new_password, request, response)

    @app.get("/api/snapshot", dependencies=[Depends(require_auth)])
    def snapshot():
        target = dataset()
        settings = store.settings()
        now = time.time()
        zone = ZoneInfo(settings.timezone)
        start = datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        users = target.rows("""SELECT u.*,s.id AS session_id,
          COALESCE(s.state,CASE WHEN u.baseline='offline' THEN 'offline' ELSE 'unknown' END) AS state,
          s.started_at,s.offline_since,s.last_ip,s.requests,
          c.enabled AS source_enabled,c.present AS source_present,c.inbounds AS source_inbounds,c.synced_at,
          (SELECT COUNT(DISTINCT value) FROM stats WHERE session_id=s.id AND kind='target') AS targets
          FROM users u LEFT JOIN sessions s ON s.id=(SELECT id FROM sessions WHERE email=u.email ORDER BY id DESC LIMIT 1)
          LEFT JOIN source_clients c ON c.email=u.email
          ORDER BY COALESCE(s.started_at,0) DESC""")
        # In paused mode the last observation is historical, not a current status.
        if not settings.monitor_enabled:
            for user in users:
                if user["state"] in ("online", "pending"):
                    user["state"] = "unknown"
        for user in users:
            if not user["monitored"]:
                user["state"] = "unknown"
        sessions = target.rows("""SELECT s.*,u.alias,
          (SELECT COUNT(*) FROM stats WHERE session_id=s.id AND kind='target') AS targets
          FROM sessions s JOIN users u ON u.email=s.email ORDER BY s.started_at DESC LIMIT 100""")
        totals = target.rows("SELECT COALESCE(SUM(requests),0) AS requests,COUNT(*) AS sessions FROM sessions WHERE started_at>=?", (start,))[0]
        targets = target.rows("SELECT value,SUM(count) AS count FROM stats t JOIN sessions s ON s.id=t.session_id WHERE kind='target' AND s.started_at>=? GROUP BY value ORDER BY count DESC,value LIMIT 6", (start,))
        notifications = target.rows("SELECT o.*,u.alias,s.email FROM outbox o LEFT JOIN sessions s ON s.id=o.session_id LEFT JOIN users u ON u.email=s.email ORDER BY o.created_at DESC,o.id DESC LIMIT 100")
        histogram = []
        for i in range(24):
            lo = start + i * 3600
            count = target.rows("SELECT COUNT(*) AS n FROM sessions WHERE started_at>=? AND started_at<?", (lo, lo + 3600))[0]["n"]
            histogram.append({"hour": i, "count": count})
        return {
            "version": __version__, "now": now, "settings": store.public_settings(),
            "admin_username": store.get('admin')['username'],
            'source_status': source_control.status(),
            "health": runtime.health, 'delivery': runtime.delivery_health(),
            "users": users, "sessions": sessions, "notifications": notifications, "targets": targets,
            "totals": {**totals, "online": sum(u["state"] == "online" for u in users), "pending": sum(u["state"] == "pending" for u in users),
                       "failures": target.rows("SELECT COUNT(*) AS n FROM outbox WHERE status IN ('failed','retry')")[0]["n"]},
            "histogram": histogram, "diagnostics": store.rows("SELECT * FROM diagnostics ORDER BY id DESC LIMIT 30"),
        }

    frontend = ROOT / "frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


# Uvicorn imports a factory so importing the module itself does not touch disk.
