"""Read-only binary tailing. The caller commits cursor + events in one DB transaction."""
import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PATTERN = re.compile(
    r"^(?P<date>\d{4}[/-]\d{2}[/-]\d{2})[ T](?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?P<tz>Z|[+-]\d{2}:?\d{2})?\s+"
    r"(?:from\s+)?(?P<source>\S+)\s+accepted\s+(?P<target>\S+)"
    r"(?:\s+\[(?P<route>[^\]]+)\])?.*?\s+email:\s*(?P<email>\S+)\s*$"
)


@dataclass
class Event:
    at: float
    email: str
    ip: str
    inbound: str
    target: str


def endpoint_host(value: str) -> str:
    value = re.sub(r"^(tcp|udp):", "", value)
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    host, sep, port = value.rpartition(":")
    return host if sep and port.isdigit() else value


def parse_line(line: str, timezone: str = "Asia/Shanghai") -> Event | None:
    match = PATTERN.match(line.strip())
    if not match:
        return None
    p = match.groupdict()
    try:
        stamp = datetime.fromisoformat(p["date"].replace("/", "-") + "T" + p["time"] + (p["tz"] or ""))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(timezone))
        ip = str(ipaddress.ip_address(endpoint_host(p["source"])))
    except (ValueError, OverflowError):
        return None
    target = endpoint_host(p["target"]).lower().rstrip(".")
    email = p["email"]
    if len(target) > 253 or len(email) > 256 or not target:
        return None
    route = p["route"] or ""
    inbound = re.split(r"\s*(?:->|>>)\s*", route)[0][:128] or "未知入站"
    return Event(stamp.timestamp(), email, ip, inbound, target)


class LogReader:
    def __init__(self):
        self.file = None
        self.path = None

    def close(self):
        if self.file:
            self.file.close()
        self.file = None
        self.path = None

    @staticmethod
    def identity(stat):
        return [stat.st_dev, stat.st_ino]

    @staticmethod
    def anchor(f, offset):
        position = f.tell()
        f.seek(max(0, offset - 128))
        value = hashlib.sha256(f.read(min(128, offset))).hexdigest()
        f.seek(position)
        return value

    def read(self, path: str, cursor: dict | None, timezone: str, limit: int = 2000):
        """Return events, next cursor, at_eof, skipped count. Never create or write logs.

        On first attachment start at EOF. On rotation drain the already-open old
        inode before opening its successor. Fingerprint detects copytruncate even
        if the new file has already grown beyond the saved byte offset.
        """
        p = Path(path)
        stat = p.stat()
        if not p.is_file():
            raise ValueError("日志路径必须是普通文件")
        changed = self.path != str(p)
        if changed:
            self.close()
        if not self.file:
            self.file = p.open("rb")
            self.path = str(p)
        f = self.file
        current_id = self.identity(os.fstat(f.fileno()))
        path_id = self.identity(stat)
        if cursor is None or cursor.get("path") != str(p):
            f.seek(0, 2)
            offset = f.tell()
            cursor = {"path": str(p), "identity": current_id, "offset": offset, "anchor": self.anchor(f, offset)}
        elif cursor["identity"] != current_id:
            f.seek(0)
        else:
            offset = cursor["offset"]
            if os.fstat(f.fileno()).st_size < offset or self.anchor(f, offset) != cursor["anchor"]:
                f.seek(0)
            else:
                f.seek(offset)
        events = []
        skipped = 0
        complete = False
        for _ in range(limit):
            start = f.tell()
            line = f.readline(65537)
            if not line:
                complete = True
                break
            if len(line) > 65536:
                # Discard the entire oversized record, not a parseable suffix.
                while line and not line.endswith(b"\n"):
                    line = f.readline(65537)
                skipped += 1
                continue
            if not line.endswith(b"\n"):
                f.seek(start)
                complete = True
                break
            event = parse_line(line.decode("utf-8", errors="replace"), timezone)
            if event:
                events.append(event)
            else:
                skipped += 1
        offset = f.tell()
        result = {"path": str(p), "identity": current_id, "offset": offset, "anchor": self.anchor(f, offset)}
        if complete and current_id != path_id:
            # Next batch opens the new inode at its beginning. The current old
            # cursor is still checkpointed atomically with the drained records.
            self.close()
            complete = False
        return events, result, complete, skipped

