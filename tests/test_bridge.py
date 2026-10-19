from pathlib import Path
import pytest
from deploy.log_bridge import Bridge


def test_bridge_only_appends_new_bytes_and_source_unchanged(tmp_path):
    source=tmp_path/'original.log'
    source.write_bytes(b'historical\n')
    bridge=Bridge(source,tmp_path/'spool')
    try:
        assert bridge.step()==0
        assert bridge.output.read_bytes()==b''
        with source.open('ab') as f:
            f.write(b'new connection\n')
        original=source.read_bytes()
        assert bridge.step()==len(b'new connection\n')
        assert bridge.output.read_bytes()==b'new connection\n'
        assert source.read_bytes()==original
        assert bridge.step()==0
    finally:
        bridge.close()


def test_bridge_resumes_checkpoint_after_restart(tmp_path):
    source=tmp_path/'source.log'
    source.write_bytes(b'old\n')
    spool=tmp_path/'spool'
    bridge=Bridge(source,spool)
    bridge.step()
    bridge.close()
    with source.open('ab') as f:
        f.write(b'during downtime\n')
    resumed=Bridge(source,spool)
    try:
        resumed.step()
        assert resumed.output.read_bytes()==b'during downtime\n'
    finally:
        resumed.close()


def test_bridge_truncates_only_uncommitted_own_spool(tmp_path):
    source=tmp_path/'source.log'
    source.write_bytes(b'old\n')
    spool=tmp_path/'spool'
    bridge=Bridge(source,spool)
    bridge.step()
    bridge.close()
    with (spool/'access.log').open('ab') as f:
        f.write(b'uncommitted')
    resumed=Bridge(source,spool)
    try:
        assert resumed.output.read_bytes()==b''
        assert source.read_bytes()==b'old\n'
    finally:
        resumed.close()


def test_bridge_refuses_source_inside_writable_directory(tmp_path):
    source=tmp_path/'access.log'
    source.write_bytes(b'protected\n')
    with pytest.raises(ValueError):
        Bridge(source,tmp_path)
    assert source.read_bytes()==b'protected\n'


def test_bridge_bounds_own_spool_via_rotation(tmp_path):
    source=tmp_path/'source.log'
    source.write_bytes(b'old\n')
    bridge=Bridge(source,tmp_path/'spool',max_bytes=4)
    try:
        bridge.step()
        with source.open('ab') as f:f.write(b'first\n')
        bridge.step()
        with source.open('ab') as f:f.write(b'second\n')
        bridge.step()
        assert bridge.output.read_bytes()==b'second\n'
        assert (bridge.directory/'access.log.1').read_bytes()==b'first\n'
        assert source.read_bytes()==b'old\nfirst\nsecond\n'
    finally:
        bridge.close()
