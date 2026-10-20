"""Shared administrator input rules; standard library only for the installer."""


def admin_username(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('管理员用户名不能包含控制字符')
    value = value.strip()
    if not 1 <= len(value) <= 80:
        raise ValueError('管理员用户名需为 1–80 个字符')
    return value


def admin_password(value: str) -> str:
    if not isinstance(value, str) or not 12 <= len(value) <= 256 or '\0' in value:
        raise ValueError('管理员密码需为 12–256 个字符')
    return value
