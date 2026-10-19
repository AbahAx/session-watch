from app.engine import Engine, render_message
from app.logs import Event
from app.models import Settings
from app.storage import Store
import pytest


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path)
    engine = Engine(store)
    settings = Settings()
    yield store, engine, settings
    store.close()


def poll(state, online, at, logs_ready=True):
    store, engine, settings = state
    with store.transaction() as db:
        engine.poll(db, online, at, settings, logs_ready)


def start(state, at=1000):
    poll(state, set(), at - 10)
    poll(state, {"lin"}, at)
    store, engine, settings = state
    with store.transaction() as db:
        engine.ingest(db, [Event(at + 1, "lin", "203.0.113.10", "in-1", "example.com")], at + 2)
    poll(state, {"lin"}, at + 10)


def messages(store):
    return store.rows("SELECT kind,text,status FROM outbox ORDER BY id")


def test_first_online_snapshot_is_baseline(state):
    poll(state, {"lin"}, 1000)
    poll(state, {"lin"}, 1010)
    assert messages(state[0]) == []


def test_newly_discovered_online_user_after_baseline_notifies(state):
    start(state)
    rows = messages(state[0])
    assert len(rows) == 1 and rows[0]["kind"] == "online"
    assert "203.0.113.10" in rows[0]["text"]
    assert "首次访问：example.com" in rows[0]["text"]
    assert rows[0]["status"] == "preview"


def test_offline_exact_five_minute_boundary(state):
    start(state)
    poll(state, set(), 1020)
    for at in range(1030, 1320, 10):
        poll(state, set(), at)
    assert [r["kind"] for r in messages(state[0])] == ["online"]
    poll(state, set(), 1319)
    assert len(messages(state[0])) == 1
    poll(state, set(), 1320)
    assert [r["kind"] for r in messages(state[0])] == ["online", "offline"]
    assert "在线：0 分 20 秒" in messages(state[0])[1]["text"]
    assert "已连续离线 5 分钟" in messages(state[0])[1]["text"]
    poll(state, set(), 1330)
    assert len(messages(state[0])) == 2


def test_recovery_within_five_minutes_keeps_same_session(state):
    start(state)
    poll(state, set(), 1020)
    for at in range(1030, 1310, 10):
        poll(state, set(), at)
    poll(state, {"lin"}, 1319)
    row = state[0].rows("SELECT * FROM sessions")[0]
    assert row["offline_since"] is None and row["state"] == "online"
    assert len(messages(state[0])) == 1
    assert len(state[0].rows("SELECT * FROM sessions")) == 1


def test_unknown_resets_offline_confirmation(state):
    start(state)
    poll(state, set(), 1020)
    for at in range(1030, 1300, 10):
        poll(state, set(), at)
    poll(state, None, 1300)
    poll(state, set(), 1310)
    for at in range(1320, 1610, 10):
        poll(state, set(), at)
    assert len(messages(state[0])) == 1
    poll(state, set(), 1610)
    assert len(messages(state[0])) == 2


def test_poll_gap_cannot_count_as_observed_offline(state):
    start(state)
    poll(state, set(), 1020)
    poll(state, set(), 5000)
    assert len(messages(state[0])) == 1
    assert state[0].rows("SELECT offline_since FROM sessions")[0]["offline_since"] == 5000


def test_new_round_after_confirmed_departure(state):
    start(state)
    for at in range(1020, 1330, 10):
        poll(state, set(), at)
    poll(state, {"lin"}, 1330)
    poll(state, {"lin"}, 1340)
    assert len(state[0].rows("SELECT * FROM sessions")) == 2
    assert [r["kind"] for r in messages(state[0])] == ["online", "offline", "online"]


def test_log_stats_sum_ips_inbounds_and_targets(state):
    start(state)
    store, engine, settings = state
    with store.transaction() as db:
        engine.ingest(db, [Event(1011, "lin", "203.0.113.11", "in-2", "example.com"), Event(1012, "lin", "203.0.113.10", "in-1", "8.8.8.8")], 1013)
        row = db.execute("SELECT * FROM sessions").fetchone()
        text = render_message(db, row, "offline", settings)
    assert "请求：3" in text
    assert "IP：2 个 · 入站：2 个" in text
    assert "目标：2 个" in text
    assert "example.com ×2" in text
    assert "最后 IP：203.0.113.10" in text


def test_catch_up_delays_summary_until_logs_drained(state):
    start(state)
    for at in range(1020, 1320, 10):
        poll(state, set(), at)
    poll(state, set(), 1320, logs_ready=False)
    assert len(messages(state[0])) == 1
    poll(state, set(), 1330, logs_ready=True)
    assert len(messages(state[0])) == 2


def test_short_session_sends_online_before_offline_without_log(state):
    poll(state, set(), 990)
    poll(state, {"lin"}, 1000)
    for at in range(1010, 1320, 10):
        poll(state, set(), at)
    assert [r["kind"] for r in messages(state[0])] == ["online", "offline"]
    assert "暂无记录" in messages(state[0])[0]["text"]


def test_target_summary_fits_telegram_length(state):
    start(state)
    store, engine, settings = state
    settings.top_targets = 50
    with store.transaction() as db:
        engine.ingest(db, [Event(1011, "lin", "203.0.113.10", "in-1", f"{'x'*235}{i}.com") for i in range(60)], 1012)
        text = render_message(db, db.execute("SELECT * FROM sessions").fetchone(), "offline", settings)
    assert len(text) < 4096
    assert "其余" in text

