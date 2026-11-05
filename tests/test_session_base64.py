"""tests/test_session_base64.py — Unit tests for core/session_manager.py"""

import os
import tempfile
from pathlib import Path
import pytest

from core.session_manager import pack_session, unpack_session, cleanup_session, session_exists_on_ramdisk


@pytest.fixture
def sample_session_dir(tmp_path):
    """Create a fake TDLib session directory with some files."""
    sdir = tmp_path / "session"
    sdir.mkdir()
    (sdir / "db.sqlite").write_bytes(b"SQLite fake data " * 100)
    (sdir / "td.binlog").write_bytes(b"\x00\x01\x02" * 50)
    sub = sdir / "subdir"
    sub.mkdir()
    (sub / "file.bin").write_bytes(b"nested file content")
    return sdir


def test_pack_returns_non_empty_string(sample_session_dir):
    blob = pack_session(sample_session_dir)
    assert isinstance(blob, str)
    assert len(blob) > 100


def test_pack_is_valid_base64(sample_session_dir):
    import base64
    blob = pack_session(sample_session_dir)
    # Should not raise
    decoded = base64.b64decode(blob)
    assert len(decoded) > 0


def test_unpack_roundtrip(tmp_path, sample_session_dir):
    """Pack a directory, unpack it elsewhere and verify content is identical."""
    blob      = pack_session(sample_session_dir)
    target    = tmp_path / "restored"
    unpack_session(blob, target)

    # Check that all original files are present and content matches
    orig_db      = (sample_session_dir / "db.sqlite").read_bytes()
    restored_db  = (target / "db.sqlite").read_bytes()
    assert orig_db == restored_db

    orig_binlog     = (sample_session_dir / "td.binlog").read_bytes()
    restored_binlog = (target / "td.binlog").read_bytes()
    assert orig_binlog == restored_binlog

    orig_nested     = (sample_session_dir / "subdir" / "file.bin").read_bytes()
    restored_nested = (target / "subdir" / "file.bin").read_bytes()
    assert orig_nested == restored_nested


def test_unpack_overwrites_existing(tmp_path, sample_session_dir):
    """unpack_session should replace existing target directory."""
    blob   = pack_session(sample_session_dir)
    target = tmp_path / "restored"
    target.mkdir()
    (target / "stale.txt").write_text("stale")

    unpack_session(blob, target)
    assert not (target / "stale.txt").exists()
    assert (target / "db.sqlite").exists()


def test_pack_nonexistent_raises():
    with pytest.raises(FileNotFoundError):
        pack_session(Path("/nonexistent/path/session"))


def test_cleanup_removes_directory(tmp_path, sample_session_dir, monkeypatch):
    """cleanup_session should remove the RAM-disk directory."""
    import config
    # Redirect RAM_DISK_BASE to tmp_path so we don't touch /dev/shm in tests
    monkeypatch.setattr(config, "RAM_DISK_BASE", tmp_path)
    from core import session_manager
    monkeypatch.setattr(session_manager, "session_dir", lambda aid: tmp_path / str(aid))

    fake_sdir = tmp_path / "42"
    fake_sdir.mkdir()
    (fake_sdir / "dummy.bin").write_bytes(b"x")

    assert fake_sdir.exists()
    session_manager.cleanup_session(42)
    assert not fake_sdir.exists()
