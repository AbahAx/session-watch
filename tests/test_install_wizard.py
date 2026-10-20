from pathlib import Path
import os

import pytest

from deploy import install_manager as manager
from deploy import install_wizard as wizard


class Console:
    def __init__(self, answers=(), passwords=()):
        self.answers, self.passwords = iter(answers), iter(passwords)
        self.messages = []

    def ask(self, prompt, default=''):
        return next(self.answers) or default

    def secret(self, prompt):
        return next(self.passwords)

    def say(self, message):
        self.messages.append(message)


def collect(args=(), console=None, old=None, has_admin=False, check=None):
    return wizard.collect_preferences(manager.parser().parse_args(args), old, has_admin, console,
                                      check or manager.connection, lambda *a: None, Path.read_bytes)


def test_local_wizard_retries_invalid_input_and_confirms_password():
    console = Console(['bad', '1', 'abc', '999', '18765', '   ', 'chosen'],
                      ['short', 'a-long-password', 'different-password', 'correct-password', 'correct-password'])
    connection, admin = collect(console=console)
    assert connection['host'] == '127.0.0.1' and connection['port'] == 18765
    assert admin.username == 'chosen' and admin.password == 'correct-password' and not admin.generated
    assert 'correct-password' not in repr(admin) and 'correct-password' not in str(console.messages)


def test_domain_wizard_detects_existing_cert_and_selects_public_host(monkeypatch):
    monkeypatch.setattr(wizard, 'find_certificate', lambda d: ('/cert/fullchain.pem','/cert/key.pem'))
    def check(args, old):
        assert args.domain == 'watch.example.com' and args.cert == '/cert/fullchain.pem' and args.key == '/cert/key.pem'
        return dict(host=args.host, port=args.port, domain=args.domain)
    connection, admin = collect(console=Console(['2','WATCH.EXAMPLE.COM','','','',''], ['']), check=check)
    assert connection == dict(host='0.0.0.0', port=8443, domain='watch.example.com')
    assert admin.generated and len(admin.password) >= 12


def test_unattended_defaults_and_custom_username():
    connection, admin = collect(['--non-interactive', '--port','18765','--admin-username','chosen'])
    assert connection['port'] == 18765 and admin.username == 'chosen' and admin.generated


def test_occupied_port_is_reprompted_before_asking_password():
    console = Console(['1','8765','18765','chosen'], [''])
    ports = []
    def check_port(connection, old):
        ports.append(connection['port'])
        if connection['port'] == 8765:
            raise ValueError('port occupied')
    connection, _ = wizard.collect_preferences(manager.parser().parse_args([]), None, False, console,
                                               manager.connection, check_port, Path.read_bytes)
    assert ports == [8765,18765] and connection['port'] == 18765


def test_upgrade_preserves_existing_credentials_and_rejects_reset_flags():
    old = {'connection': manager.connection(manager.parser().parse_args(['--port','18765']))}
    connection, admin = collect(old=old, has_admin=True)
    assert connection == old['connection'] and admin is None
    with pytest.raises(ValueError, match='已有管理员'):
        collect(['--admin-username','replacement'], old=old, has_admin=True)


@pytest.mark.parametrize('value', ['https://watch.example.com', '../outside', 'host:8443', 'bad host', '-bad.example', 'a..b'])
def test_domain_rejects_non_host_input(value):
    with pytest.raises(ValueError):
        wizard.valid_domain(value)


def test_password_file_is_not_echoed(tmp_path, monkeypatch):
    path = tmp_path / 'password'
    path.write_text('file-password-12345\n')
    path.chmod(0o600)
    # Simulate root ownership on CI without changing process-global os.name.
    original = Path.stat
    def owned_stat(self, *args, **kwargs):
        info = original(self, *args, **kwargs)
        if os.name != 'nt' and self == path:
            values = list(info)
            values[4] = 0  # st_uid
            return os.stat_result(values)
        return info
    monkeypatch.setattr(Path, 'stat', owned_stat)
    _, admin = collect(['--admin-password-file',str(path)])
    assert admin.password == 'file-password-12345' and not admin.generated
    assert admin.password not in repr(admin)


def test_dry_run_never_opens_terminal(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('dry-run tried to open a terminal')
    monkeypatch.setattr(wizard, 'open', forbidden, raising=False)
    args = manager.parser().parse_args(['--dry-run','--interactive'])
    assert wizard.prepare_preferences(args, None, tmp_path, manager.connection, lambda *a:None, Path.read_bytes)[1].generated


def test_explicit_interactive_requires_terminal_but_auto_falls_back(tmp_path, monkeypatch):
    def no_tty(*a, **kw):
        raise OSError('no controlling tty')
    monkeypatch.setattr(wizard, 'open', no_tty, raising=False)
    args = manager.parser().parse_args(['--interactive'])
    with pytest.raises(ValueError, match='真实终端'):
        wizard.prepare_preferences(args, None, tmp_path, manager.connection, lambda *a:None, Path.read_bytes)
    args.interactive = None
    assert wizard.prepare_preferences(args, None, tmp_path, manager.connection, lambda *a:None, Path.read_bytes)[1].generated
