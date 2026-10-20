import pytest
from fastapi.testclient import TestClient

from app.bootstrap import initialize
from app.main import create_app
from app.storage import Store, password_matches

HEADERS = {'X-Requested-With': 'session-watch'}
PASSWORD = 'test-password-12345'


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, background=False), base_url='http://127.0.0.1') as client:
        client.post('/api/auth/setup', headers=HEADERS, json={'username': 'admin', 'password': PASSWORD})
        yield client


def change(client, **updates):
    return client.put('/api/auth/account', headers=HEADERS, json={
        'username': 'new-admin', 'current_password': PASSWORD, **updates})


def test_rename_preserves_password_and_revokes_other_sessions(client):
    old_cookie = client.cookies.get('sw_session')
    before = client.app.state.store.get('admin')['password']
    assert change(client).json() == {'ok': True, 'username': 'new-admin', 'changed': True}
    assert client.get('/api/snapshot').json()['admin_username'] == 'new-admin'
    assert client.app.state.store.get('admin')['password'] == before
    with TestClient(client.app, base_url='http://127.0.0.1') as other:
        other.cookies.set('sw_session', old_cookie)
        assert other.get('/api/auth/account').status_code == 401
        assert other.post('/api/auth/login', headers=HEADERS, json={'username':'admin', 'password':PASSWORD}).status_code == 401
        assert other.post('/api/auth/login', headers=HEADERS, json={'username':'new-admin', 'password':PASSWORD}).status_code == 200


def test_change_both_credentials_and_keep_current_session(client):
    assert change(client, new_password='another-password-123').status_code == 200
    assert client.get('/api/auth/account').json()['username'] == 'new-admin'
    admin = client.app.state.store.get('admin')
    assert password_matches('another-password-123', admin['password'])
    assert not password_matches(PASSWORD, admin['password'])


@pytest.mark.parametrize('updates,status', [
    ({'current_password':'wrong'}, 400), ({'username':'   '}, 422),
    ({'username':'bad\nname'}, 422), ({'new_password':'short'}, 422),
    ({'new_password':'contains-nul\0-secret'}, 422),
])
def test_invalid_change_is_atomic_and_does_not_echo_password(client, updates, status):
    before = client.app.state.store.get('admin')
    response = change(client, **updates)
    assert response.status_code == status
    assert PASSWORD not in response.text and 'contains-nul' not in response.text
    assert client.app.state.store.get('admin') == before
    assert client.get('/api/auth/account').status_code == 200


def test_account_verification_is_rate_limited(client):
    for _ in range(5):
        assert change(client, current_password='wrong').status_code == 400
    assert change(client).status_code == 429
    assert client.app.state.store.get('admin')['username'] == 'admin'


def test_account_noop_keeps_sessions_and_requires_auth(client):
    cookie = client.cookies.get('sw_session')
    assert change(client, username='admin').json()['changed'] is False
    assert client.cookies.get('sw_session') == cookie
    client.post('/api/auth/logout', headers=HEADERS)
    assert client.get('/api/auth/account').status_code == 401
    assert change(client).status_code == 401


def test_bootstrap_uses_chosen_credentials_and_preserves_existing_account(tmp_path):
    payload = dict(data=str(tmp_path), username='chosen', password=PASSWORD,
                   snapshot='/var/lib/session-watch-local/snapshot.json',
                   log='/var/lib/session-watch-bridge/access.log', timezone='UTC')
    assert initialize(payload)
    store = Store(tmp_path)
    try:
        before = store.get('admin')
        assert before['username'] == 'chosen' and password_matches(PASSWORD, before['password'])
        settings = store.public_settings()
    finally:
        store.close()
    assert not initialize({**payload, 'username':'replacement', 'password':'', 'timezone':'invalid'})
    store = Store(tmp_path)
    try:
        assert store.get('admin') == before and store.public_settings() == settings
    finally:
        store.close()
    assert PASSWORD.encode() not in (tmp_path / 'session-watch.db').read_bytes()
