"""Installer-only initialization via stdin; emits no administrator secrets."""
import json
from pathlib import Path
import sys

from app.accounts import admin_username, admin_password
from app.models import Settings
from app.storage import Store, password_hash


def initialize(payload):
    store = Store(Path(payload['data']))
    try:
        if store.get('admin'):
            return False
        username = admin_username(payload['username'])
        password = admin_password(payload['password'])
        settings = Settings(**{**store.settings().model_dump(),
            'local_snapshot_path': payload['snapshot'], 'log_path': payload['log'],
            'log_timezone': payload['timezone']})
        # Store settings before claiming admin so a failed validation remains retryable.
        store.save_settings(settings)
        store.set('admin', {'username': username, 'password': password_hash(password)})
        return True
    finally:
        store.close()


if __name__ == '__main__':
    print('created' if initialize(json.load(sys.stdin)) else 'preserved')
