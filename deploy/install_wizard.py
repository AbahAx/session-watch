"""Interactive install preferences, using /dev/tty even behind a heredoc/pipe."""
from dataclasses import dataclass, field
import getpass
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import warnings

from app.accounts import admin_password, admin_username


@dataclass
class InitialAdmin:
    username: str
    password: str = field(repr=False)
    generated: bool = False


class Console:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    def say(self, message):
        print(message, file=self.writer, flush=True)

    def ask(self, prompt, default=''):
        suffix = f' [{default}]' if default else ''
        self.writer.write(prompt + suffix + ': ')
        self.writer.flush()
        line = self.reader.readline()
        if line == '':
            raise ValueError('安装向导输入已关闭；未修改任何服务。自动安装请使用 --non-interactive')
        return line.rstrip('\r\n') or default

    def secret(self, prompt):
        # getpass independently opens the controlling tty and disables echo.
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                return getpass.getpass(prompt + ': ', stream=self.writer)
            except getpass.GetPassWarning:
                raise ValueError('终端无法隐藏密码输入，已停止；请改用 --admin-password-file') from None


def existing_admin(data):
    path = data / 'session-watch.db'
    if not path.exists():
        return False
    if path.is_symlink():
        raise ValueError('Refusing a symlink administrator database')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        return bool(db.execute("SELECT value FROM kv WHERE key='admin'").fetchone())


def find_certificate(domain):
    # Only look for this exact, already validated host name. Preserve live links.
    candidates = [
        (f'/root/cert/{domain}/fullchain.pem', f'/root/cert/{domain}/privkey.pem'),
        (f'/etc/letsencrypt/live/{domain}/fullchain.pem', f'/etc/letsencrypt/live/{domain}/privkey.pem'),
        (f'/root/.acme.sh/{domain}_ecc/fullchain.cer', f'/root/.acme.sh/{domain}_ecc/{domain}.key'),
        (f'/root/.acme.sh/{domain}/fullchain.cer', f'/root/.acme.sh/{domain}/{domain}.key'),
    ]
    return next(((cert, key) for cert, key in candidates if Path(cert).is_file() and Path(key).is_file()), ('', ''))


def valid_domain(value):
    import re
    value = value.strip().lower().rstrip('.')
    if not value or len(value) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.')):
        raise ValueError('请输入域名本身，例如 watch.example.com，不包含 https://、路径或端口')
    return value


def ask_valid(console, prompt, validate, default=''):
    while True:
        value = console.ask(prompt, default)
        try:
            return validate(value)
        except (ValueError, OSError) as exc:
            console.say(str(exc))


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise ValueError('端口请输入数字') from None
    if not 1024 <= port <= 65535:
        raise ValueError('面板端口需为 1024–65535，不占用原面板的 443 端口')
    return port


def access_mode(value):
    if value not in ('1', '2'):
        raise ValueError('请选择 1 或 2')
    return value


def password_file(path, read_regular):
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ValueError('密码文件必须是普通文件，不能是符号链接')
    if os.name != 'nt' and (info.st_uid != 0 or info.st_mode & 0o077):
        raise ValueError('密码文件必须属于 root，且权限为 600 或更严格')
    if info.st_size > 4096:
        raise ValueError('密码文件过大')
    return admin_password(read_regular(path).decode('utf-8').removesuffix('\n').removesuffix('\r'))


def collect_preferences(args, old, has_admin, console, check_connection, check_port, read_regular):
    if has_admin and (args.admin_username is not None or args.admin_password_file):
        raise ValueError('已有管理员账号会保留；请在面板“系统设置 → 管理员账号”修改，不在安装/升级时重置')
    if args.domain and args.host is None and not old:
        args.host = '0.0.0.0'
    if console:
        console.say('\nSession Watch 安装向导：端口、域名与管理员账号\n')
        console.say('已有 3x-ui 的程序、数据库、日志权限和证书均不会被修改。')
        if args.host is None and not old and not any((args.domain, args.cert, args.key)):
            console.say('1) 本机访问（通过 SSH 隧道）\n2) 域名 + 端口（HTTPS，使用现有证书）')
            mode = ask_valid(console, '访问方式', access_mode, '1')
            args.host = '0.0.0.0' if mode == '2' else '127.0.0.1'
        previous = (old or {}).get('connection', {})
        public = args.host == '0.0.0.0' or bool(args.domain or args.cert or args.key or previous.get('cert'))
        if public and not args.domain:
            args.domain = ask_valid(console, '访问域名', valid_domain, (old or {}).get('connection', {}).get('domain', ''))
        if args.domain:
            args.domain = valid_domain(args.domain)
            if args.host is None and not old:
                args.host = '0.0.0.0'
        if args.port is None:
            default = str((old or {}).get('connection', {}).get('port', 8443 if public else 8765))
            args.port = ask_valid(console, '面板端口', port_number, default)
        if public:
            detected_cert, detected_key = find_certificate(args.domain)
            previous = previous if previous.get('domain') == args.domain else {}
            if not args.cert:
                args.cert = console.ask('证书 fullchain 文件路径', previous.get('cert') or detected_cert)
            if not args.key:
                args.key = console.ask('证书私钥文件路径', previous.get('key') or detected_key)
            console.say('证书只读使用；请确保域名已解析到本机，且防火墙/云安全组已允许所选端口。')
        # Validate TLS pair and socket availability before asking for secrets.
        connection = check_connection(args, (old or {}).get('connection'))
        while True:
            try:
                check_port(connection, old)
                break
            except (ValueError, OSError) as exc:
                console.say(str(exc))
                args.port = ask_valid(console, '请重新设置面板端口', port_number)
                connection = check_connection(args, (old or {}).get('connection'))
        if has_admin:
            console.say('检测到已有管理员：保留用户名、密码及页面配置。')
    else:
        connection = check_connection(args, (old or {}).get('connection'))
        check_port(connection, old)
    if has_admin:
        return connection, None
    username = admin_username(args.admin_username) if args.admin_username is not None else (
        ask_valid(console, '管理员用户名', admin_username, 'admin') if console else 'admin')
    if args.admin_password_file:
        password = password_file(args.admin_password_file, read_regular)
        return connection, InitialAdmin(username, password)
    if console:
        while True:
            password = console.secret('管理员密码（12–256 个字符；留空生成随机密码）')
            if not password:
                console.say('将生成随机密码，保存在 root 专用的初始账号文件中。')
                break
            try:
                admin_password(password)
            except ValueError as exc:
                console.say(str(exc))
                continue
            if password == console.secret('再次输入管理员密码'):
                return connection, InitialAdmin(username, password)
            console.say('两次密码不同，请重新输入。')
    return connection, InitialAdmin(username, secrets.token_urlsafe(21), generated=True)


def prepare_preferences(args, old, data, check_connection, check_port, read_regular):
    has_admin = existing_admin(data)
    reader, writer = None, None
    # A heredoc in the online launcher consumes stdin; /dev/tty still reaches
    # the human's SSH terminal. CI/unattended runs without a tty keep defaults.
    wants_prompt = args.interactive if args.interactive is not None else args.command == 'install'
    if wants_prompt and not args.dry_run:
        try:
            reader = open('/dev/tty', 'r', encoding='utf-8')
            writer = open('/dev/tty', 'w', encoding='utf-8', buffering=1)
        except OSError:
            if reader:
                reader.close()
                reader = None
            if args.interactive:
                raise ValueError('交互安装需要真实终端；请在 SSH 终端运行，或使用 --non-interactive') from None
    try:
        return collect_preferences(args, old, has_admin, Console(reader, writer) if writer else None,
                                   check_connection, check_port, read_regular)
    finally:
        if reader:
            reader.close()
        if writer:
            writer.close()
