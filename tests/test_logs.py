from datetime import datetime, timezone
from app.logs import LogReader, parse_line
import pytest


LINE = "2026/10/19 12:30:00.123456 from 203.0.113.10:50321 accepted tcp:example.com:443 [in-24641-tcp -> direct] email: lin\n"


def test_parser_extracts_fields_and_timezone():
    e = parse_line(LINE)
    assert (e.email, e.ip, e.inbound, e.target) == ("lin", "203.0.113.10", "in-24641-tcp", "example.com")
    assert abs(e.at - datetime(2026, 10, 19, 4, 30, 0, 123456, tzinfo=timezone.utc).timestamp()) < .001


def test_parser_ipv6_utc_and_old_format():
    e = parse_line("2026-10-19T12:30:00Z [2001:db8::1]:1234 accepted udp:[2001:4860:4860::8888]:53 [in >> direct] email: demo")
    assert e.ip == "2001:db8::1"
    assert e.target == "2001:4860:4860::8888"
    assert e.inbound == "in"


@pytest.mark.parametrize("line", ["rejected", LINE.replace("accepted", "rejected"), LINE.replace("email: lin", ""), LINE.replace("203.0.113.10", "1.2.*.*"), LINE.replace("2026/10/19", "2026/99/99")])
def test_parser_ignores_unsupported_or_malformed(line):
    assert parse_line(line) is None


def test_first_attach_skips_history_then_reads_only_new_records(tmp_path):
    p = tmp_path / "access.log"
    p.write_text(LINE)
    reader = LogReader()
    try:
        events, cursor, eof, _ = reader.read(str(p), None, "Asia/Shanghai")
        assert not events
        with p.open("a") as f:
            f.write(LINE)
        events, cursor, eof, _ = reader.read(str(p), cursor, "Asia/Shanghai")
        assert len(events) == 1
        assert reader.read(str(p), cursor, "Asia/Shanghai")[0] == []
    finally:
        reader.close()


def test_partial_line_retried_and_readonly_source_unchanged(tmp_path):
    p = tmp_path / "access.log"
    p.write_bytes(b"")
    reader = LogReader()
    try:
        _, cursor, _, _ = reader.read(str(p), None, "Asia/Shanghai")
        with p.open("ab") as f:
            f.write(LINE[:-1].encode())
        assert reader.read(str(p), cursor, "Asia/Shanghai")[0] == []
        with p.open("ab") as f:
            f.write(b"\n")
        before = p.read_bytes()
        events, cursor, _, _ = reader.read(str(p), cursor, "Asia/Shanghai")
        assert len(events) == 1 and p.read_bytes() == before
    finally:
        reader.close()


def test_copytruncate_with_regrowth_detected_by_anchor(tmp_path):
    p = tmp_path / "access.log"
    p.write_text(LINE)
    reader = LogReader()
    try:
        _, cursor, _, _ = reader.read(str(p), None, "Asia/Shanghai")
        # New contents are longer than the old checkpoint (size alone fails).
        p.write_text(LINE.replace("lin", "new") * 3)
        events, _, _, _ = reader.read(str(p), cursor, "Asia/Shanghai")
        assert len(events) == 3 and all(e.email == "new" for e in events)
    finally:
        reader.close()


def test_restart_resumes_saved_offset(tmp_path):
    p = tmp_path / "access.log"
    p.write_text(LINE)
    reader = LogReader()
    _, cursor, _, _ = reader.read(str(p), None, "Asia/Shanghai")
    reader.close()
    with p.open("a") as f:
        f.write(LINE)
    reader = LogReader()
    try:
        events, _, _, _ = reader.read(str(p), cursor, "Asia/Shanghai")
        assert len(events) == 1
    finally:
        reader.close()


def test_rotation_after_restart_reads_new_inode_from_start(tmp_path):
    p = tmp_path / "access.log"
    p.write_text(LINE)
    reader = LogReader()
    _, cursor, _, _ = reader.read(str(p), None, "Asia/Shanghai")
    reader.close()
    p.rename(tmp_path / "access.log.1")
    p.write_text(LINE * 2)
    reader = LogReader()
    try:
        events, _, _, _ = reader.read(str(p), cursor, "Asia/Shanghai")
        assert len(events) == 2
    finally:
        reader.close()


def test_missing_file_is_not_created(tmp_path):
    p = tmp_path / "missing.log"
    with pytest.raises(FileNotFoundError):
        LogReader().read(str(p), None, "Asia/Shanghai")
    assert not p.exists()

