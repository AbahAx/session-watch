import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from app.models import Settings

SECRET_FIELDS = {"panel_password", "panel_totp", "telegram_token"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 email TEXT PRIMARY KEY, alias TEXT NOT NULL DEFAULT '', monitored INTEGER NOT NULL DEFAULT 1,
 last_seen REAL, baseline TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE IF NOT EXISTS source_clients (
 email TEXT PRIMARY KEY REFERENCES users(email), enabled INTEGER NOT NULL,
 inbounds TEXT NOT NULL, present INTEGER NOT NULL, synced_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ip_locations (
 ip TEXT PRIMARY KEY, location TEXT NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL, state TEXT NOT NULL, started_at REAL NOT NULL,
 last_online REAL NOT NULL, offline_since REAL, ended_at REAL, last_activity REAL,
 last_ip TEXT NOT NULL DEFAULT '', first_ip TEXT NOT NULL DEFAULT '',
 first_inbound TEXT NOT NULL DEFAULT '', first_target TEXT NOT NULL DEFAULT '',
 requests INTEGER NOT NULL DEFAULT 0, online_notified INTEGER NOT NULL DEFAULT 0,
 stats_incomplete INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_session ON sessions(email) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS stats (
 session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, value TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(session_id,kind,value));
CREATE TABLE IF NOT EXISTS recent_logs (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL, at REAL NOT NULL, ip TEXT NOT NULL,
 inbound TEXT NOT NULL, target TEXT NOT NULL, session_id INTEGER);
CREATE INDEX IF NOT EXISTS recent_email_time ON recent_logs(email,at);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', sent_at REAL, UNIQUE(session_id,kind));
CREATE TABLE IF NOT EXISTS admin_sessions (
 token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS diagnostics (
 id INTEGER PRIMARY KEY, at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
"""


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = directory
        self.lock = threading.RLock()
        key_path = directory / "secret.key"
        if not key_path.exists():
            # O_EXCL avoids silently replacing a key during concurrent startup.
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.vault = Fernet(key_path.read_bytes())
        self.db = sqlite3.connect(directory / "session-watch.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        if 'stats_incomplete' not in {row[1] for row in self.db.execute('PRAGMA table_info(sessions)')}:
            self.db.execute('ALTER TABLE sessions ADD COLUMN stats_incomplete INTEGER NOT NULL DEFAULT 0')
        if 'source_ip' not in {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}:
            self.db.execute("ALTER TABLE outbox ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''")
            self.db.execute("""UPDATE outbox SET source_ip=COALESCE((SELECT CASE WHEN outbox.kind='online'
                THEN first_ip ELSE last_ip END FROM sessions WHERE id=outbox.session_id),'')
                WHERE status IN ('pending','retry','failed')""")
        self.db.commit()
        if os.name != "nt":
            os.chmod(directory / "session-watch.db", 0o600)
        if self.get("settings") is None:
            self.save_settings(Settings())

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    def settings(self) -> Settings:
        data = self.get("settings", {})
        for field in SECRET_FIELDS:
            if data.get(field):
                data[field] = self.vault.decrypt(data[field].encode()).decode()
        return Settings(**data)

    def save_settings(self, settings: Settings):
        data = settings.model_dump()
        for field in SECRET_FIELDS:
            if data.get(field):
                data[field] = self.vault.encrypt(data[field].encode()).decode()
        with self.transaction() as db:
            db.execute("INSERT INTO kv VALUES ('settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(data, ensure_ascii=False),))
            row = db.execute("SELECT value FROM kv WHERE key='settings_revision'").fetchone()
            revision = json.loads(row[0]) + 1 if row else 1
            db.execute("INSERT INTO kv VALUES ('settings_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(revision),))

    def public_settings(self):
        with self.lock:
            data = self.settings().model_dump()
            data['settings_revision'] = self.get('settings_revision', 0)
        for field in SECRET_FIELDS:
            data[field + "_configured"] = bool(data.pop(field))
        return data

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def diagnostic(self, at, level, message):
        with self.transaction() as db:
            db.execute("INSERT INTO diagnostics(at,level,message) VALUES (?,?,?)", (at, level, message))
            db.execute("DELETE FROM diagnostics WHERE id < (SELECT COALESCE(MAX(id),0)-199 FROM diagnostics)")

    def close(self):
        with self.lock:
            self.db.close()


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + digest


def password_matches(password: str, stored: str) -> bool:
    return secrets.compare_digest(password_hash(password, stored.split(":")[0]), stored)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

