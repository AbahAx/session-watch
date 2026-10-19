import asyncio
import httpx
import pytest

from app.integrations import DeliveryError, IntegrationError, PanelClient, send_telegram
from app.models import Settings
from app.runtime import Runtime
from app.storage import Store


def panel_settings():
    return Settings(panel_url="https://example.test/secret-base", panel_username="admin", panel_password="private-password")


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport))


def test_panel_uses_only_auth_and_query_allowlist_with_old_api_fallback(monkeypatch):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith('/csrf-token'):
            return httpx.Response(404)
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={"success": True}, headers={"set-cookie": "session=test; Path=/"})
        if request.url.path.endswith('/clients/onlines'):
            return httpx.Response(404)
        if request.url.path.endswith('/inbounds/onlines'):
            assert "session=test" in request.headers['cookie']
            return httpx.Response(200, json={"success": True, "obj": ["lin", "work"]})
        pytest.fail("Unexpected endpoint: " + request.url.path)

    install_transport(monkeypatch, handler)

    async def run():
        client = PanelClient()
        assert await client.onlines(panel_settings()) == {"lin", "work"}
        assert await client.onlines(panel_settings()) == {"lin", "work"}
        await client.close()
    asyncio.run(run())
    assert len([p for m, p in calls if p.endswith('/login')]) == 1
    assert all(not any(word in p for word in ('reset', 'update', 'add', 'del')) for m, p in calls)


@pytest.mark.parametrize("response", [{"success": False}, {"success": True}, {"success": True, "obj": {}}, {"success": True, "obj": [None]}, {"success": True, "obj": [""]}])
def test_bad_online_response_never_becomes_empty_offline_list(monkeypatch, response):
    def handler(request):
        if request.url.path.endswith('/csrf-token'):
            return httpx.Response(404)
        return httpx.Response(200, json={"success": True} if request.url.path.endswith('/login') else response)
    install_transport(monkeypatch, handler)

    async def run():
        client = PanelClient()
        with pytest.raises(IntegrationError):
            await client.onlines(panel_settings())
        await client.close()
    asyncio.run(run())


def test_panel_tls_errors_never_expose_credentials(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("private-password https://example.test/secret-base", request=request)
    install_transport(monkeypatch, handler)

    async def run():
        client = PanelClient()
        with pytest.raises(IntegrationError) as exc:
            await client.onlines(panel_settings())
        assert "private-password" not in str(exc.value)
        assert "secret-base" not in str(exc.value)
        await client.close()
    asyncio.run(run())


def test_successful_go_nil_list_is_empty_online_set(monkeypatch):
    def handler(request):
        if request.url.path.endswith('/csrf-token'):
            return httpx.Response(404)
        return httpx.Response(200, json={'success': True} if request.url.path.endswith('/login') else {'success': True, 'obj': None})
    install_transport(monkeypatch, handler)
    async def run():
        client = PanelClient()
        assert await client.onlines(panel_settings()) == set()
        await client.close()
    asyncio.run(run())


def test_new_panel_string_csrf_token_is_used_and_refreshed(monkeypatch):
    tokens = []
    requests = []
    def handler(request):
        path = request.url.path
        requests.append(path)
        if path.endswith('/csrf-token'):
            token = f'csrf-{len(tokens)}'
            tokens.append(token)
            return httpx.Response(200, json={'success': True, 'obj': token})
        if path.endswith('/login'):
            assert request.headers['x-csrf-token'] == 'csrf-0'
            return httpx.Response(200, json={'success': True})
        assert request.headers['x-csrf-token'] == 'csrf-1'
        return httpx.Response(200, json={'success': True, 'obj': []})
    install_transport(monkeypatch, handler)
    async def run():
        client = PanelClient()
        assert await client.onlines(panel_settings()) == set()
        await client.close()
    asyncio.run(run())
    assert len(tokens) == 2


def test_failed_login_cools_down_instead_of_hammering_panel(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404) if request.url.path.endswith('/csrf-token') else httpx.Response(200, json={'success': False})
    install_transport(monkeypatch, handler)
    async def run():
        client = PanelClient()
        for _ in range(5):
            with pytest.raises(IntegrationError):
                await client.onlines(panel_settings())
        await client.close()
    asyncio.run(run())
    assert len([p for p in calls if p.endswith('/login')]) == 1


def test_telegram_payload_plaintext_no_parse_mode(monkeypatch):
    token = "123456:abcdefghijklmnopqrstuvwxyz"
    captured = []
    def handler(request):
        import json
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    install_transport(monkeypatch, handler)
    settings = Settings(telegram_token=token, telegram_chat_id="123")
    assert asyncio.run(send_telegram(settings, "<user>&domain.com")) == 42
    assert captured[0]["text"] == "<user>&domain.com"
    assert "parse_mode" not in captured[0]


def test_telegram_rate_limit_and_secret_redaction(monkeypatch):
    token = "123456:abcdefghijklmnopqrstuvwxyz"
    install_transport(monkeypatch, lambda request: httpx.Response(429, json={"ok": False, "description": token, "parameters": {"retry_after": 90}}))
    with pytest.raises(DeliveryError) as exc:
        asyncio.run(send_telegram(Settings(telegram_token=token, telegram_chat_id="123"), "hello"))
    assert exc.value.retry_after == 90
    assert token not in str(exc.value)


def test_sender_preserves_order_and_retries_persistently(tmp_path, monkeypatch):
    store = Store(tmp_path)
    settings = Settings(monitor_enabled=True, telegram_enabled=True)
    store.save_settings(settings)
    with store.transaction() as db:
        sid = db.execute("INSERT INTO sessions(email,state,started_at,last_online) VALUES ('lin','online',1,2)").lastrowid
        db.execute("INSERT INTO outbox(session_id,kind,text,created_at,status) VALUES (?,'online','arrived',1,'pending')", (sid,))
        db.execute("INSERT INTO outbox(session_id,kind,text,created_at,status) VALUES (?,'offline','departed',2,'pending')", (sid,))
    captured = []
    async def fail(settings, text):
        captured.append(text)
        raise DeliveryError("网络暂不可用", retry_after=90)
    monkeypatch.setattr('app.runtime.send_telegram', fail)
    runtime = Runtime(store)
    asyncio.run(runtime.send_once())
    assert captured == ['arrived']
    assert store.rows("SELECT status,attempts FROM outbox ORDER BY id")[0] == {"status":"retry", "attempts":1}
    # The departure must not jump ahead while its arrival is pending.
    asyncio.run(runtime.send_once())
    assert captured == ['arrived']
    async def succeed(settings, text):
        captured.append(text)
        return 1
    monkeypatch.setattr('app.runtime.send_telegram', succeed)
    with store.transaction() as db:
        db.execute("UPDATE outbox SET next_try=0")
    asyncio.run(runtime.send_once())
    asyncio.run(runtime.send_once())
    assert captured == ['arrived', 'arrived', 'departed']
    assert all(r['status']=='sent' for r in store.rows("SELECT status FROM outbox"))
    store.close()


@pytest.mark.parametrize('monitor,telegram', [(False, True), (True, False)])
def test_paused_or_delivery_disabled_sender_never_calls_network(tmp_path, monkeypatch, monitor, telegram):
    store = Store(tmp_path)
    settings = Settings(monitor_enabled=monitor, telegram_enabled=telegram)
    store.save_settings(settings)
    async def forbidden(*args):
        pytest.fail("Paused or disabled sender attempted network send")
    monkeypatch.setattr('app.runtime.send_telegram', forbidden)
    asyncio.run(Runtime(store).send_once())
    store.close()

