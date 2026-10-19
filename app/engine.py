import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.logs import Event
from app.models import Settings
from app.storage import Store
from app.geolocation import cached_location, with_location


def clock_text(value, timezone):
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%H:%M:%S") if value is not None else "暂无记录"


def render_message(db, session, kind: str, settings: Settings) -> str:
    user = db.execute("SELECT alias FROM users WHERE email=?", (session["email"],)).fetchone()
    name = (user[0] if user and user[0] else session["email"])[:80]
    if kind == "online":
        message = (f"🟢 {name} 上线\n\n🖥 节点：{settings.source_name}\n🌐 IP：{session['first_ip'] or '暂无记录'}\n"
                f"🚪 入站：{session['first_inbound'] or '暂无记录'}\n"
                f"🎯 首次访问：{session['first_target'] or '暂无记录'}\n"
                f"🕐 时间：{clock_text(session['started_at'], settings.timezone)}")
        return with_location(message, cached_location(db, session['first_ip'])) if settings.ip_location_enabled else message
    end = session["ended_at"] or session["offline_since"] or session["last_online"]
    seconds = max(0, int(end - session["started_at"]))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    duration = (f"{hours} 小时 " if hours else "") + f"{minutes} 分 {seconds} 秒"
    counts = dict(db.execute("SELECT kind,COUNT(*) FROM stats WHERE session_id=? GROUP BY kind", (session["id"],)).fetchall())
    targets = db.execute("SELECT value,count FROM stats WHERE session_id=? AND kind='target' ORDER BY count DESC,value LIMIT ?", (session["id"], settings.top_targets)).fetchall()
    lines = [f"🔴 {name} 下线", "", f"🖥 节点：{settings.source_name}", f"🌐 最后 IP：{session['last_ip'] or '暂无记录'}", f"⏱ 在线：{duration}",
             f"📊 请求：{session['requests']}", f"🔄 IP：{counts.get('ip', 0)} 个 · 入站：{counts.get('inbound', 0)} 个",
             f"🌍 目标：{counts.get('target', 0)} 个", f"🕐 最后活动：{clock_text(session['last_activity'], settings.timezone)}", "", "🌍 主要访问"]
    for i, row in enumerate(targets, 1):
        line = f"{i}. {row[0]} ×{row[1]}"
        if len("\n".join(lines)) + len(line) > 3500:
            break
        lines.append(line)
    if not targets:
        lines.append("暂无访问记录")
    shown = len(lines) - lines.index("🌍 主要访问") - 1
    if targets and counts.get("target", 0) > shown:
        lines.append(f"… 其余 {counts['target'] - shown} 个目标见管理面板")
    lines.extend(["", f"💤 已连续离线 {settings.offline_seconds // 60} 分钟"])
    if session["stats_incomplete"]:
        lines.append("⚠️ 期间访问日志读取异常，访问统计可能不完整")
    message = "\n".join(lines)
    return with_location(message, cached_location(db, session['last_ip'])) if settings.ip_location_enabled else message


class Engine:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def ensure_user(db, email):
        db.execute("INSERT OR IGNORE INTO users(email) VALUES (?)", (email,))

    def ingest(self, db, events: list[Event], now: float):
        for event in events:
            # Reject implausible timestamps, rather than polluting a current session.
            if event.at > now + 60 or event.at < now - 86400:
                continue
            self.ensure_user(db, event.email)
            db.execute("UPDATE users SET last_seen=MAX(COALESCE(last_seen,0),?) WHERE email=?", (event.at, event.email))
            session = db.execute("SELECT * FROM sessions WHERE email=? AND ended_at IS NULL", (event.email,)).fetchone()
            assigned = None
            if session and event.at >= session["started_at"] - 60:
                self.attach(db, session["id"], event)
                assigned = session["id"]
            db.execute("INSERT INTO recent_logs(email,at,ip,inbound,target,session_id) VALUES (?,?,?,?,?,?)", (event.email, event.at, event.ip, event.inbound, event.target, assigned))

    @staticmethod
    def attach(db, session_id, event):
        db.execute("""UPDATE sessions SET requests=requests+1,
          first_ip=CASE WHEN first_ip='' THEN ? ELSE first_ip END,
          first_inbound=CASE WHEN first_inbound='' THEN ? ELSE first_inbound END,
          first_target=CASE WHEN first_target='' THEN ? ELSE first_target END,
          last_ip=CASE WHEN last_activity IS NULL OR ?>=last_activity THEN ? ELSE last_ip END,
          last_activity=MAX(COALESCE(last_activity,0),?) WHERE id=?""",
          (event.ip, event.inbound, event.target, event.at, event.ip, event.at, session_id))
        for kind, value in [("ip", event.ip), ("inbound", event.inbound), ("target", event.target)]:
            db.execute("INSERT INTO stats VALUES (?,?,?,1) ON CONFLICT(session_id,kind,value) DO UPDATE SET count=count+1", (session_id, kind, value))

    def enqueue(self, db, session, kind, settings, now):
        status = "pending" if settings.telegram_enabled else "preview"
        db.execute("INSERT OR IGNORE INTO outbox(session_id,kind,text,created_at,status,source_ip) VALUES (?,?,?,?,?,?)",
                   (session["id"], kind, render_message(db, session, kind, settings), now, status,
                    session['first_ip'] if kind == 'online' else session['last_ip']))
        if kind == "online":
            db.execute("UPDATE sessions SET online_notified=1 WHERE id=?", (session["id"],))

    def poll(self, db, online: set[str] | None, now: float, settings: Settings, logs_ready=True):
        """None is unknown, NOT an empty online list. Five minutes requires
        consecutive successful polls; restart/gaps reset a pending offline timer.
        """
        previous = db.execute("SELECT value FROM kv WHERE key='last_good_poll'").fetchone()
        last_good = json.loads(previous[0]) if previous else None
        stale = last_good is not None and now - last_good > settings.poll_seconds * 2.5
        if online is None:
            db.execute("UPDATE sessions SET state='unknown',offline_since=NULL WHERE ended_at IS NULL")
            return
        db.execute("INSERT INTO kv VALUES ('last_good_poll',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(now),))
        for email in online:
            self.ensure_user(db, email)
        users = db.execute("SELECT * FROM users").fetchall()
        for user in users:
            email = user["email"]
            session = db.execute("SELECT * FROM sessions WHERE email=? AND ended_at IS NULL", (email,)).fetchone()
            if not user["monitored"]:
                continue
            # First healthy snapshot is a baseline, not a fabricated arrival.
            if user["baseline"] in ("unknown", "baseline") and not session:
                db.execute("UPDATE users SET baseline=? WHERE email=?", ("online" if email in online else "offline", email))
                if email in online:
                    session = self.start(db, email, now, settings, baseline=last_good is None or user["baseline"] == "baseline")
            elif email in online and not session:
                session = self.start(db, email, now, settings, baseline=False)
            if not session:
                continue
            if email in online:
                db.execute("UPDATE sessions SET state='online',last_online=?,offline_since=NULL WHERE id=?", (now, session["id"]))
                db.execute("UPDATE users SET last_seen=? WHERE email=?", (now, email))
                fresh = db.execute("SELECT * FROM sessions WHERE id=?", (session["id"],)).fetchone()
                # Give the log reader one poll to collect the first access before
                # rendering the one online message. A missing record is explicit.
                if not fresh["online_notified"] and (fresh["first_target"] or now - fresh["started_at"] >= settings.poll_seconds):
                    self.enqueue(db, fresh, "online", settings, now)
            else:
                since = session["offline_since"]
                if since is None or stale or session["state"] == "unknown":
                    db.execute("UPDATE sessions SET state='pending',offline_since=? WHERE id=?", (now, session["id"]))
                elif now - since >= settings.offline_seconds and logs_ready:
                    # Preserve arrival-before-departure ordering for very short sessions.
                    if not session["online_notified"]:
                        self.enqueue(db, session, "online", settings, now)
                    db.execute("UPDATE sessions SET state='offline',ended_at=? WHERE id=?", (since, session["id"]))
                    closed = db.execute("SELECT * FROM sessions WHERE id=?", (session["id"],)).fetchone()
                    self.enqueue(db, closed, "offline", settings, now)
                    db.execute("UPDATE users SET baseline='offline' WHERE email=?", (email,))

    def start(self, db, email, now, settings, baseline):
        result = db.execute("INSERT INTO sessions(email,state,started_at,last_online,online_notified) VALUES (?,'online',?,?,?)", (email, now, now, int(baseline)))
        session_id = result.lastrowid
        for row in db.execute("SELECT * FROM recent_logs WHERE email=? AND session_id IS NULL AND at>=? ORDER BY at,id", (email, now - settings.poll_seconds * 2)).fetchall():
            self.attach(db, session_id, Event(row["at"], email, row["ip"], row["inbound"], row["target"]))
            db.execute("UPDATE recent_logs SET session_id=? WHERE id=?", (session_id, row["id"]))
        return db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()

    def housekeeping(self, db, now, settings):
        db.execute("DELETE FROM recent_logs WHERE at<?", (now - 600,))
        # Do not delete unsent deliveries merely because their session is old.
        db.execute("DELETE FROM sessions WHERE ended_at<? AND id NOT IN (SELECT session_id FROM outbox WHERE status IN ('pending','retry') AND session_id IS NOT NULL)", (now - settings.retention_days * 86400,))
