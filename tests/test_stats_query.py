import pytest
from deploy.stats_query import decode_users, users


def test_empty_and_repeated_online_users():
    assert decode_users(b'') == []
    assert decode_users(b'\x0a\x03one\x0a\x03two') == ['one','two']


@pytest.mark.parametrize('data',[b'\x0a\x03x',b'\x80',b'\x00',b'\x0a\x00',b'\x0a\x01\xff',b'\x08\x01'])
def test_corrupt_protocol_is_unknown_not_empty_online_list(data):
    with pytest.raises((ValueError,UnicodeError)):decode_users(data)


@pytest.mark.parametrize('address',['example.com:62789','192.0.2.1:62789','127.0.0.1:0','127.0.0.1:99999'])
def test_only_existing_loopback_stats_allowed(address):
    with pytest.raises(ValueError):users(address)
