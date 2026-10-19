"""Optional privileged read-only source bridge for root-only Xray logs.

The web app stays unprivileged. All writable state belongs to this bridge,
never to 3x-ui. Launch only with the sandboxed service example in this directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import time


class Bridge:
    def __init__(self, source: Path, directory: Path, max_bytes=32 * 1024 * 1024):
        self.source = source
        self.directory = directory
        self.max_bytes = max_bytes
        if source.resolve().is_relative_to(directory.resolve()):
            raise ValueError('Source must be outside the writable spool directory')
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        directory.chmod(0o750)
        self.output = directory / 'access.log'
        self.checkpoint = directory / 'cursor.json'
        self.heartbeat = directory / 'heartbeat'
        self.file = None
        if self.checkpoint.exists():
            self.state = json.loads(self.checkpoint.read_text())
            if self.state.get('source') != str(source):
                raise ValueError('Bridge source changed; use a separate spool directory')
        else:
            self.state = None
        # Recover any bytes appended after the last durable cursor commit.
        if self.state and self.output.exists():
            size = self.state.get('output_size', 0)
            if self.output.stat().st_size >= size:
                with self.output.open('r+b') as f:
                    f.truncate(size)

    @staticmethod
    def identity(stat):
        return [stat.st_dev, stat.st_ino]

    @staticmethod
    def anchor(f, offset):
        position = f.tell()
        f.seek(max(0, offset - 128))
        digest = hashlib.sha256(f.read(min(128, offset))).hexdigest()
        f.seek(position)
        return digest

    def commit(self, state):
        temp = self.directory / '.cursor.tmp'
        with temp.open('w') as f:
            os.chmod(temp, 0o600)
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.checkpoint)
        self.state = state

    def step(self):
        path_stat = self.source.stat()
        if not self.source.is_file():
            raise ValueError('Source must be a regular log file')
        if self.file is None:
            self.file = self.source.open('rb')
        f = self.file
        identity = self.identity(os.fstat(f.fileno()))
        state = self.state
        if state is None:
            # Do not replay historical traffic on first installation.
            f.seek(0, 2)
            self.output.touch(mode=0o640, exist_ok=True)
            self.output.chmod(0o640)
            self.commit({'source':str(self.source), 'identity':identity, 'offset':f.tell(), 'anchor':self.anchor(f,f.tell()), 'output_size':self.output.stat().st_size})
            self.heartbeat.touch(mode=0o640, exist_ok=True)
            return 0
        if state['identity'] != identity:
            f.seek(0)
        else:
            offset = state['offset']
            if os.fstat(f.fileno()).st_size < offset or self.anchor(f, offset) != state['anchor']:
                f.seek(0)
            else:
                f.seek(offset)
        chunk = f.read(256 * 1024)
        if chunk:
            # Rotate only the bridge's own spool. Persist the new output position
            # before writing, allowing recovery after a crash between operations.
            if self.output.exists() and self.output.stat().st_size >= self.max_bytes:
                archived = self.directory / 'access.log.1'
                os.replace(self.output, archived)
                self.output.touch(mode=0o640)
                self.commit({**state, 'output_size':0})
            with self.output.open('ab') as out:
                os.chmod(self.output, 0o640)
                out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            self.commit({'source':str(self.source), 'identity':identity, 'offset':f.tell(), 'anchor':self.anchor(f,f.tell()), 'output_size':self.output.stat().st_size})
        else:
            if identity != self.identity(path_stat):
                f.close()
                self.file = None
        # An unreadable/missing source never refreshes this heartbeat.
        self.heartbeat.touch(mode=0o640, exist_ok=True)
        return len(chunk)

    def close(self):
        if self.file:
            self.file.close()
        self.file = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--spool', type=Path, required=True)
    args = parser.parse_args()
    bridge = Bridge(args.source, args.spool)
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while running:
            try:
                size = bridge.step()
                time.sleep(0.05 if size == 256 * 1024 else 1)
            except (OSError, ValueError):
                # No source writes or permission changes, ever. The stale
                # heartbeat lets the web app mark partial statistics explicitly.
                bridge.close()
                time.sleep(2)
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
