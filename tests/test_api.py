import json
from fastapi.testclient import TestClient
import pytest
from app.main import create_app

HEADERS = {"X-Requested-With": "session-watch"}


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, background=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def setup(client):
    return client.post("/api/auth/setup", headers=HEADERS, json={"username": "admin", "password": "test-password-12345"})


def test_auth_required_and_setup_claimed_once(client):
    assert client.get("/api/snapshot").status_code == 401
    assert setup(client).status_code == 200
    assert setup(client).status_code == 409
    assert client.get("/api/snapshot").status_code == 200


def test_cookie_has_httponly_and_same_site(client):
    cookie = setup(client).headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie


def test_csrf_custom_header_and_origin_required(client):
    assert client.post("/api/auth/setup", json={"username": "admin", "password": "test-password-12345"}).status_code == 403
    assert client.post("/api/auth/setup", headers={**HEADERS, "Origin": "https://evil.example"}, json={"username": "admin", "password": "test-password-12345"}).status_code == 403


def test_untrusted_host_rejected(client):
    assert client.get('/api/auth/status', headers={'Host': 'evil.example'}).status_code == 400


def test_explicit_public_origin_supports_https_reverse_proxy(client, monkeypatch):
    monkeypatch.setenv('SESSION_WATCH_PUBLIC_ORIGIN', 'https://127.0.0.1')
    assert client.post('/api/auth/setup', headers={**HEADERS, 'Origin':'https://127.0.0.1'}, json={'username':'admin', 'password':'test-password-12345'}).status_code == 200


def test_new_install_has_no_fabricated_users_or_demo_mode(client):
    setup(client)
    data = client.get("/api/snapshot").json()
    assert 'mode' not in data and 'mode' not in data['settings']
    assert data['settings']['source_type'] == 'local'
    assert data['users'] == [] and data['notifications'] == []
    assert not (client.app.state.store.directory / 'demo').exists()
    assert client.app.state.store.rows("SELECT * FROM users") == []


def test_settings_secrets_are_encrypted_and_never_echoed(client):
    setup(client)
    payload = client.get("/api/settings").json()
    payload.update(panel_password="super-secret-panel-password", telegram_token="123456:abcdefghijklmnopqrstuvwxyz", panel_url="https://example.test/panel-root")
    response = client.put("/api/settings", headers=HEADERS, json=payload)
    assert response.status_code == 200
    assert "super-secret" not in response.text and "abcdefghijklmnopqrstuvwxyz" not in response.text
    assert response.json()["panel_password_configured"] is True
    encrypted = client.app.state.store.get("settings")
    assert "super-secret" not in json.dumps(encrypted)
    payload["panel_password"] = ""
    assert client.put("/api/settings", headers=HEADERS, json=payload).status_code == 200
    assert client.app.state.store.settings().panel_password == "super-secret-panel-password"


def test_invalid_config_does_not_echo_secret_input(client):
    setup(client)
    payload = client.get("/api/settings").json()
    payload.update(panel_url="https://user:secret-leak@example.test", telegram_token="secret-token"*100)
    response = client.put("/api/settings", headers=HEADERS, json=payload)
    assert response.status_code == 422
    assert "secret-leak" not in response.text and "secret-token" not in response.text


def test_unconfigured_install_cannot_monitor_or_send_test(client):
    setup(client)
    assert client.post("/api/monitor", headers=HEADERS, json={"enabled": True}).status_code == 400
    assert client.post("/api/telegram/test", headers=HEADERS).status_code == 400


def test_local_source_can_check_import_and_start_without_panel_credentials(client, tmp_path):
    from deploy.local_collector import publish
    import time
    setup(client)
    spool = tmp_path / 'local'
    publish(spool, {'version': 1, 'generated_at': time.time(), 'clients_ok': True,
                    'online_ok': True, 'clients': [{'email': 'existing', 'enabled': False, 'inbounds': []}], 'online': []})
    log = tmp_path / 'access.log'
    log.touch()
    settings = client.get('/api/settings').json()
    settings.update(local_snapshot_path=str(spool / 'snapshot.json'), log_path=str(log))
    assert client.put('/api/settings', headers=HEADERS, json=settings).status_code == 200
    result = client.post('/api/source/check', headers=HEADERS).json()
    assert result['ok'] and result['client_count'] == 1
    users = client.get('/api/snapshot').json()['users']
    assert users[0]['email'] == 'existing' and users[0]['source_enabled'] == 0
    assert client.post('/api/monitor', headers=HEADERS, json={'enabled': True}).status_code == 200
    assert not client.app.state.store.settings().panel_password


def test_user_alias_save_and_session_detail(client):
    setup(client)
    store = client.app.state.store
    import time
    from app.logs import Event
    now = time.time()
    with store.transaction() as db:
        client.app.state.runtime.engine.ingest(db, [Event(now, 'lin@example.test', '203.0.113.1', 'in', 'example.test')], now)
        client.app.state.runtime.engine.poll(db, {'lin@example.test'}, now, store.settings())
    response = client.put("/api/users/lin@example.test", headers=HEADERS, json={"alias": "我的电脑", "monitored": True})
    assert response.status_code == 200
    data = client.get("/api/snapshot").json()
    user = next(u for u in data["users"] if u["email"] == "lin@example.test")
    assert user["alias"] == "我的电脑"
    detail = client.get(f"/api/sessions/{user['session_id']}").json()
    assert detail["targets"] and "我的电脑" in detail["offline_message"]


def test_logout_revokes_cookie(client):
    setup(client)
    cookie = client.cookies.get("sw_session")
    assert client.post("/api/auth/logout", headers=HEADERS).status_code == 200
    client.cookies.set("sw_session", cookie)
    assert client.get("/api/snapshot").status_code == 401


def test_password_change_invalidates_previous_session(client):
    setup(client)
    old = client.cookies.get("sw_session")
    assert client.post("/api/auth/password", headers=HEADERS, json={"current_password": "test-password-12345", "new_password": "a-new-long-password-123"}).status_code == 200
    assert client.cookies.get("sw_session") != old
    client.cookies.set("sw_session", old)
    assert client.get("/api/snapshot").status_code == 401


def test_login_rate_limit(client):
    setup(client)
    client.post("/api/auth/logout", headers=HEADERS)
    for _ in range(5):
        assert client.post("/api/auth/login", headers=HEADERS, json={"username":"admin", "password":"wrong-password-123"}).status_code == 401
    assert client.post("/api/auth/login", headers=HEADERS, json={"username":"admin", "password":"test-password-12345"}).status_code == 429
