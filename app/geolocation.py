"""Optional public-IP city lookup. Never sends identity, targets or credentials."""
import asyncio
import ipaddress
import time

import httpx


def clean_part(value):
    if not isinstance(value, str):
        return ''
    value = ''.join(c for c in value if c.isprintable() and c not in '\r\n')[:80].strip()
    return '' if value in {'0', '未知', 'N/A', '-'} else value


def format_location(data):
    country = clean_part(data.get('country'))
    province = clean_part(data.get('prov'))
    original_province = province
    city = clean_part(data.get('city'))
    area = clean_part(data.get('area'))
    if str(data.get('country_code', '')).lower() == 'cn':
        municipalities = {'北京', '上海', '天津', '重庆'}
        if province in municipalities:
            province += '市'
        elif province in {'香港', '澳门'}:
            province += '特别行政区'
        elif province == '内蒙古':
            province += '自治区'
        elif province in {'广西', '宁夏', '新疆', '西藏'}:
            province += {'广西': '壮族自治区', '宁夏': '回族自治区', '新疆': '维吾尔自治区', '西藏': '自治区'}[province]
        elif province and not province.endswith(('省', '市', '区')):
            province += '省'
        if city in {original_province, province}:
            city = ''
        if city and city not in {province, province.removesuffix('市')} and not city.endswith(('市', '州', '地区', '盟', '县', '区')):
            city += '市'
        parts = [province]
        for part in (city, area):
            if part and part not in parts and part != province.removesuffix('市'):
                parts.append(part)
        return ''.join(parts) or country
    parts = []
    for part in (country, province, city, area):
        if part and part not in parts:
            parts.append(part)
    return ' · '.join(parts)


def normalize_ip(value):
    if not isinstance(value, str):
        return '', False
    try:
        ip = ipaddress.ip_address(value)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return str(ip), ip.is_global and not ip.is_multicast
    except ValueError:
        return '', False


def cached_location(db, source_ip):
    ip, public = normalize_ip(source_ip)
    if not ip:
        return '暂无记录'
    if not public:
        return '内网或保留地址'
    row = db.execute('SELECT location FROM ip_locations WHERE ip=? AND expires_at>?', (ip, time.time())).fetchone()
    return row[0] if row else '待查询'


def with_location(text, location=None):
    lines = [line for line in text.splitlines() if not line.startswith(('IP归属地：', '📍 IP归属地：'))]
    if location is not None:
        for i, line in enumerate(lines):
            if line.startswith(('🌐 IP：', '🌐 最后 IP：')):
                lines.insert(i + 1, '📍 IP归属地：' + location)
                break
    return '\n'.join(lines)


class GeoLocator:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()

    def save(self, ip, location, ttl):
        now = time.time()
        with self.store.transaction() as db:
            db.execute('''INSERT INTO ip_locations(ip,location,updated_at,expires_at) VALUES (?,?,?,?)
                ON CONFLICT(ip) DO UPDATE SET location=excluded.location,updated_at=excluded.updated_at,
                expires_at=excluded.expires_at''', (ip, location, now, now + ttl))
            db.execute('DELETE FROM ip_locations WHERE ip NOT IN (SELECT ip FROM ip_locations ORDER BY updated_at DESC LIMIT 5000)')
        return location

    async def lookup(self, source_ip):
        ip, public = normalize_ip(source_ip)
        if not ip:
            return '暂无记录'
        if not public:
            return '内网或保留地址'
        async with self.lock:
            rows = self.store.rows('SELECT location FROM ip_locations WHERE ip=? AND expires_at>?', (ip, time.time()))
            if rows:
                return rows[0]['location']
            if time.time() < self.store.get('geo_retry_at', 0):
                return '暂未查询到'
            # Cross-restart rate control, below the provider's 60/minute limit.
            wait = self.store.get('geo_request_after', 0) - time.time()
            if 0 < wait <= 2:
                await asyncio.sleep(wait)
            self.store.set('geo_request_after', time.time() + 2)
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
                    response = await asyncio.wait_for(client.get('https://ip9.com.cn/get', params={'ip': ip}), timeout=4)
                if response.status_code == 429:
                    try:
                        retry = min(86400, max(60, int(response.headers.get('Retry-After', '60'))))
                    except ValueError:
                        retry = 60
                    self.store.set('geo_retry_at', time.time() + retry)
                    return self.save(ip, '查询限流，暂未获取', min(retry, 600))
                if response.status_code != 200 or len(response.content) > 32768:
                    raise ValueError()
                payload = response.json()
                data = payload.get('data') if isinstance(payload, dict) else None
                if (payload.get('ret') != 200 or not isinstance(data, dict) or
                        normalize_ip(data.get('ip', ''))[0] != ip):
                    raise ValueError()
                location = format_location(data)
                if not location:
                    raise ValueError()
                return self.save(ip, location, 7 * 86400)
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
                # Safe, bounded failure: no service description, URL or response
                # enters the notification. Telegram delivery still proceeds.
                return self.save(ip, '查询失败，暂未获取', 600)
