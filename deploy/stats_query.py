"""Only Xray GetAllOnlineUsers; empty request has no mutation/reset fields.

Wire schema: https://github.com/XTLS/Xray-core/blob/main/app/stats/command/command.proto
"""
import ipaddress
import json
import sys
from urllib.parse import urlsplit


def decode_users(data):
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('Oversized stats response')
    index, users = 0, []
    def varint():
        nonlocal index
        value = 0
        for shift in range(0, 70, 7):
            if index >= len(data):
                raise ValueError('Truncated stats response')
            byte = data[index]; index += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('Invalid stats response')
    while index < len(data):
        tag = varint()
        if tag & 7 != 2 or tag == 0:
            raise ValueError('Unexpected stats schema')
        length = varint()
        if index + length > len(data):
            raise ValueError('Truncated stats response')
        if tag >> 3 == 1:
            value = data[index:index + length].decode('utf-8')
            if not value.strip() or len(value) > 8192:
                raise ValueError('Invalid online name')
            users.append(value)
        index += length
    return users


def users(address):
    parts = urlsplit('//' + address)
    if not parts.hostname or not ipaddress.ip_address(parts.hostname).is_loopback or not 1 <= parts.port <= 65535:
        raise ValueError('Stats address must be loopback')
    import grpc
    with grpc.insecure_channel(address, options=[('grpc.max_receive_message_length', 4 * 1024 * 1024)]) as channel:
        method = channel.unary_unary('/xray.app.stats.command.StatsService/GetAllOnlineUsers',
                                     request_serializer=lambda _: b'', response_deserializer=decode_users)
        return method(None, timeout=3)


if __name__ == '__main__':
    try:
        print(json.dumps({'users': users(sys.argv[1])}))
    except Exception:
        # RPC errors may embed upstream data; emit a fixed message only.
        print('Existing loopback statistics query failed', file=sys.stderr)
        raise SystemExit(1)
