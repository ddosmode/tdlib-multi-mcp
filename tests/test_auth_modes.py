"""
tests/test_auth_modes.py — Unit tests for auth subsystem (mocked, no Telegram).

Tests decode/encode logic for Telethon and Pyrogram session format.
No actual network connections are made.
"""

import base64
import struct
import pytest

from auth.session_import import _decode_telethon, _decode_pyrogram
from auth.session_export import _read_tdlib_key_file
from pathlib import Path
import tempfile


# ── Helpers to build synthetic sessions ───────────────────────────────────────

def _make_telethon_session(dc_id: int = 2, auth_key: bytes = b"\x42" * 256) -> str:
    """Build a synthetic Telethon StringSession string."""
    ip    = bytes([149, 154, 167, 51])
    port  = struct.pack(">H", 443)
    payload = bytes([dc_id]) + ip + port + auth_key
    encoded  = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return "1" + encoded


def _make_pyrogram_session(
    dc_id: int = 2,
    auth_key: bytes = b"\x43" * 256,
    user_id: int = 123456789,
) -> str:
    """Build a synthetic Pyrogram v2 StringSession string."""
    payload = bytes([dc_id, 0]) + auth_key + struct.pack(">Q", user_id) + bytes([0])
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


# ── Telethon decode tests ──────────────────────────────────────────────────────

def test_decode_telethon_basic():
    auth_key = b"\xAB" * 256
    session  = _make_telethon_session(dc_id=2, auth_key=auth_key)
    decoded  = _decode_telethon(session)

    assert decoded["dc_id"]   == 2
    assert decoded["auth_key"] == auth_key
    assert decoded["ip"]      == "149.154.167.51"
    assert decoded["port"]    == 443


def test_decode_telethon_dc_id_preserved():
    for dc_id in (1, 2, 3, 4, 5):
        session = _make_telethon_session(dc_id=dc_id)
        decoded = _decode_telethon(session)
        assert decoded["dc_id"] == dc_id


def test_decode_telethon_too_short():
    with pytest.raises(ValueError, match="too short"):
        _decode_telethon("1" + base64.urlsafe_b64encode(b"short").decode())


# ── Pyrogram decode tests ──────────────────────────────────────────────────────

def test_decode_pyrogram_basic():
    auth_key = b"\xBC" * 256
    user_id  = 987654321
    session  = _make_pyrogram_session(dc_id=4, auth_key=auth_key, user_id=user_id)
    decoded  = _decode_pyrogram(session)

    assert decoded["dc_id"]    == 4
    assert decoded["auth_key"] == auth_key
    assert decoded["user_id"]  == user_id


def test_decode_pyrogram_too_short():
    with pytest.raises(ValueError, match="too short"):
        _decode_pyrogram(base64.urlsafe_b64encode(b"x" * 10).decode())


# ── TDLib key file read/write ──────────────────────────────────────────────────

def test_read_tdlib_key_file_roundtrip(tmp_path):
    """Write a key file manually and verify _read_tdlib_key_file returns correct values."""
    dc_id    = 3
    auth_key = b"\xDE" * 256

    key_file = tmp_path / "td_auth_key.bin"
    key_file.write_bytes(struct.pack("<I", dc_id) + auth_key)

    read_dc, read_key = _read_tdlib_key_file(tmp_path)
    assert read_dc  == dc_id
    assert read_key == auth_key


def test_read_tdlib_key_file_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        _read_tdlib_key_file(tmp_path)
