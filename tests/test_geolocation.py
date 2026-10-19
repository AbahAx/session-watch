import asyncio
import json
import time

import httpx
import pytest

from app.engine import render_message
from app.geolocation import GeoLocator, format_location, with_location
from app.models import Settings
from app.runtime import Runtime
from app.storage import Store


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr('app.geolocation.httpx.AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def response(ip='1.1.1.1', **changes):
    return {'ret': 200, 'data': {'ip': ip, 'country': '中国', 'country_code': 'cn',
                               'prov': '河北', 'city': '保定', 'area': '', **changes}}


def test_formats_only_provider_fields_without_inventing_district():
    assert format_location(response()['data']) == '河北省保定市'
    assert format_location(response(area='涿州市')['data']) == '河北省保定市涿州市'
    assert format_location(response(prov='北京', city='北京')['data']) == '北京市'
    assert format_location(response(prov='香港', city='香港')['data']) == '香港特别行政区'
    assert format_location({'country': '美国', 'prov': '加利福尼亚州', 'city': '洛杉矶'}) == '美国 · 加利福尼亚州 · 洛杉矶'
    assert '\n' not in format_location(response(city='保定\n伪造字段')['data'])


@pytest.mark.parametrize('ip,expected', [('', '暂无记录'), ('not-ip', '暂无记录'),
    ('192.168.1.1', '内网或保留地址'), ('127.0.0.1', '内网或保留地址'),
    ('::1', '内网或保留地址'), ('2001:db8::1', '内网或保留地址'), ('224.0.0.1', '内网或保留地址')])
def test_private_reserved_or_invalid_ip_never_leaves_server(tmp_path, monkeypatch, ip, expected):
    def forbidden(_):
        pytest.fail('Non-public IP sent to provider')
    transport(monkeypatch, forbidden)
    store = Store(tmp_path)
    assert asyncio.run(GeoLocator(store).lookup(ip)) == expected
    store.close()


def test_public_lookup_uses_https_only_ip_and_survives_restart_cache(tmp_path, monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.url.scheme == 'https' and request.url.host == 'ip9.com.cn'
        assert dict(request.url.params) == {'ip': '1.1.1.1'}
        assert not request.content
        return httpx.Response(200, json=response())
    transport(monkeypatch, handler)
    store=Store(tmp_path)
    assert asyncio.run(GeoLocator(store).lookup('1.1.1.1')) == '河北省保定市'
    store.close()
    store=Store(tmp_path)
    assert asyncio.run(GeoLocator(store).lookup('1.1.1.1')) == '河北省保定市'
    assert len(calls)==1
    store.close()


def test_ipv6_and_mapped_ipv4_normalized_before_query(tmp_path, monkeypatch):
    def handler(request):
        ip=request.url.params['ip']
        return httpx.Response(200,json=response(ip=ip))
    transport(monkeypatch,handler)
    store=Store(tmp_path)
    locator=GeoLocator(store)
    assert asyncio.run(locator.lookup('::ffff:1.1.1.1')) == '河北省保定市'
    store.set('geo_retry_at',0)
    store.set('geo_request_after',0)
    assert asyncio.run(locator.lookup('2606:4700:4700::1111')) == '河北省保定市'
    store.close()


@pytest.mark.parametrize('reply', [httpx.Response(200,json={'ret':500,'data':{}}),
    httpx.Response(200,json=response(ip='9.9.9.9')), httpx.Response(200,content=b'invalid-json'),
    httpx.Response(500), httpx.Response(302,headers={'Location':'http://private.test'})])
def test_errors_cached_and_not_exposed_in_notification(tmp_path,monkeypatch,reply):
    calls=[]
    def handler(request):
        calls.append(request)
        return reply
    transport(monkeypatch,handler)
    store=Store(tmp_path)
    locator=GeoLocator(store)
    assert asyncio.run(locator.lookup('1.1.1.1')) == '查询失败，暂未获取'
    assert asyncio.run(locator.lookup('1.1.1.1')) == '查询失败，暂未获取'
    assert len(calls)==1
    store.close()


def test_provider_rate_limit_honored_across_different_ips(tmp_path,monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(429,headers={'Retry-After':'120'})
    transport(monkeypatch,handler)
    store=Store(tmp_path)
    locator=GeoLocator(store)
    assert asyncio.run(locator.lookup('1.1.1.1')) == '查询限流，暂未获取'
    assert asyncio.run(locator.lookup('8.8.8.8')) == '暂未查询到'
    assert len(calls)==1 and store.get('geo_retry_at')>time.time()+100
    store.close()


def test_online_first_ip_and_offline_last_ip_are_frozen_per_queued_message(tmp_path,monkeypatch):
    store=Store(tmp_path)
    settings=Settings(monitor_enabled=True,telegram_enabled=True,ip_location_enabled=True)
    store.save_settings(settings)
    with store.transaction() as db:
        db.execute("INSERT INTO users(email) VALUES ('Lite')")
        sid=db.execute("""INSERT INTO sessions(email,state,started_at,last_online,first_ip,last_ip)
            VALUES ('Lite','offline',1,30,'1.1.1.1','8.8.8.8')""").lastrowid
        session=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        runtime=Runtime(store)
        runtime.engine.enqueue(db,session,'online',settings,1)
        runtime.engine.enqueue(db,session,'offline',settings,2)
        # A later change must not relabel a previously queued IP.
        db.execute("UPDATE sessions SET first_ip='9.9.9.9',last_ip='1.1.1.1' WHERE id=?",(sid,))
    calls=[]
    async def lookup(ip):
        calls.append(ip)
        return {'1.1.1.1':'河北省保定市','8.8.8.8':'美国'}[ip]
    captured=[]
    async def send(config,text):
        captured.append(text)
    monkeypatch.setattr(runtime.geolocation,'lookup',lookup)
    monkeypatch.setattr('app.runtime.send_telegram',send)
    asyncio.run(runtime.send_once())
    asyncio.run(runtime.send_once())
    assert calls==['1.1.1.1','8.8.8.8']
    assert '🌐 IP：1.1.1.1\n📍 IP归属地：河北省保定市' in captured[0]
    assert '🌐 最后 IP：8.8.8.8\n📍 IP归属地：美国' in captured[1]
    assert [r['text'] for r in store.rows('SELECT text FROM outbox ORDER BY id')]==captured
    assert all(r['status']=='sent' for r in store.rows('SELECT status FROM outbox'))
    store.close()


def test_provider_timeout_does_not_block_telegram_delivery(tmp_path,monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout('private service error',request=request)
    transport(monkeypatch,handler)
    store=Store(tmp_path)
    store.save_settings(Settings(monitor_enabled=True,telegram_enabled=True,ip_location_enabled=True))
    with store.transaction() as db:
        db.execute("INSERT INTO outbox(kind,text,created_at,status,source_ip) VALUES ('online','🌐 IP：1.1.1.1',1,'pending','1.1.1.1')")
    captured=[]
    async def send(_,text):
        captured.append(text)
    monkeypatch.setattr('app.runtime.send_telegram',send)
    asyncio.run(Runtime(store).send_once())
    assert captured==['🌐 IP：1.1.1.1\n📍 IP归属地：查询失败，暂未获取']
    assert store.rows('SELECT status FROM outbox')[0]['status']=='sent'
    store.close()


def test_disabled_option_removes_location_without_query(tmp_path,monkeypatch):
    store=Store(tmp_path)
    store.save_settings(Settings(monitor_enabled=True,telegram_enabled=True))
    with store.transaction() as db:
        db.execute("INSERT INTO outbox(kind,text,created_at,status) VALUES ('online','🌐 IP：1.1.1.1\nIP归属地：待查询',1,'pending')")
    runtime=Runtime(store)
    async def forbidden(_):
        pytest.fail('Disabled feature performed lookup')
    captured=[]
    async def send(_,text):
        captured.append(text)
    monkeypatch.setattr(runtime.geolocation,'lookup',forbidden)
    monkeypatch.setattr('app.runtime.send_telegram',send)
    asyncio.run(runtime.send_once())
    assert captured==['🌐 IP：1.1.1.1']
    store.close()


def test_pause_during_lookup_leaves_message_pending(tmp_path,monkeypatch):
    store=Store(tmp_path)
    store.save_settings(Settings(monitor_enabled=True,telegram_enabled=True,ip_location_enabled=True))
    with store.transaction() as db:
        db.execute("INSERT INTO outbox(kind,text,created_at,status,source_ip) VALUES ('online','🌐 IP：1.1.1.1',1,'pending','1.1.1.1')")
    runtime=Runtime(store)
    async def lookup(_):
        current=store.settings()
        current.monitor_enabled=False
        store.save_settings(current)
        return '河北省保定市'
    async def forbidden(*_):
        pytest.fail('Message sent after pause')
    monkeypatch.setattr(runtime.geolocation,'lookup',lookup)
    monkeypatch.setattr('app.runtime.send_telegram',forbidden)
    asyncio.run(runtime.send_once())
    assert store.rows('SELECT status FROM outbox')[0]['status']=='pending'
    store.close()
