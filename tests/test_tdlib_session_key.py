"""Closed-binlog compatibility checks use synthetic keys and no Telegram accounts."""
from __future__ import annotations

import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib

import pytest

from core import tdlib_session_key as codec

SYNTHETIC_KEY = bytes(range(256))
NATIVE_DEPS = Path.home() / '.local/share/tdlib-multi-mcp/native/usr/lib/x86_64-linux-gnu'


@pytest.fixture
def binlog_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(codec, 'require_supported_tdlib', lambda: None)
    directory = tmp_path / 'database'
    codec.write_native_session_key(directory, 4, SYNTHETIC_KEY, 123456789)
    return directory


def test_closed_binlog_roundtrip_and_exclusive_write(binlog_dir):
    assert codec.read_native_session_key(binlog_dir) == (4, SYNTHETIC_KEY)
    assert (binlog_dir / 'td.binlog').stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='fresh empty'):
        codec.write_native_session_key(binlog_dir, 2, SYNTHETIC_KEY)
    assert codec.read_native_session_key(binlog_dir) == (4, SYNTHETIC_KEY)


def _event(event_id, event_type, flags, payload=b''):
    raw = struct.pack('<IQiiQ', 32 + len(payload), event_id, event_type, flags, 0) + payload
    return raw + struct.pack('<I', zlib.crc32(raw))


def test_rewrite_and_tombstone_cannot_resurrect_old_authorization(binlog_dir):
    path = binlog_dir / 'td.binlog'
    with path.open('ab') as stream:
        # Main authorization marker is event 3 in the minimal database.
        stream.write(_event(3, 0x4327, 1, b'\x04auth\0\0\0\x06logout\0'))
    with pytest.raises(ValueError, match='no authorized user session'):
        codec.read_native_session_key(binlog_dir)
    with path.open('ab') as stream:
        stream.write(_event(3, -2, 1))
    with pytest.raises(ValueError, match='no authorized user session'):
        codec.read_native_session_key(binlog_dir)


def test_crc_and_truncation_rejected(binlog_dir):
    path = binlog_dir / 'td.binlog'
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(ValueError, match='checksum'):
        codec.read_native_session_key(binlog_dir)
    path.write_bytes(original[:-1])
    with pytest.raises(ValueError, match='Truncated'):
        codec.read_native_session_key(binlog_dir)


def test_encrypted_binlog_and_unknown_build_rejected(binlog_dir, monkeypatch):
    path = binlog_dir / 'td.binlog'
    path.write_bytes(_event(0, -3, 0))
    with pytest.raises(ValueError, match='encrypted binlog'):
        codec.read_native_session_key(binlog_dir)
    def unknown_build():
        raise RuntimeError('pinned TDLib build required')
    monkeypatch.setattr(codec, 'require_supported_tdlib', unknown_build)
    with pytest.raises(RuntimeError, match='pinned TDLib'):
        codec.read_native_session_key(binlog_dir)


@pytest.mark.skipif(not (NATIVE_DEPS / 'libc++.so.1').exists(), reason='Local TDLib native dependencies unavailable')
def test_pinned_native_library_opens_seeded_binlog_offline(tmp_path):
    env = dict(os.environ, TG_MCP_TESTING='1', TG_MCP_TEST_TMPDIR=str(tmp_path), PYTHONDONTWRITEBYTECODE='1')
    env['LD_LIBRARY_PATH'] = str(NATIVE_DEPS) + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    program = '''
import asyncio
import os
from pathlib import Path
from aiotdlib.api import NetworkTypeNone, SetNetworkType
from core.tdlib_session_key import read_native_session_key, write_native_session_key
root = Path(os.environ['TG_MCP_TEST_TMPDIR'])
# Keep the subprocess's config-created log directory inside its test fixture.
Path.home = classmethod(lambda cls: root)
from core.tdlib_client import ManagedClient, build_client_settings, wait_auth_state

class OfflineClient(ManagedClient):
    async def _set_tdlib_parameters(self):
        await self.send(SetNetworkType(type=NetworkTypeNone()))
        await super()._set_tdlib_parameters()

async def main():
    fingerprint = dict(device_model='Desktop', system_version='Linux', app_version='1',
                       system_language_code='en', lang_pack='tdesktop', lang_code='en')
    session = root / 'synthetic'
    key = bytes(range(256))
    write_native_session_key(session / 'database', 4, key, 123456789)
    client = OfflineClient(build_client_settings(fingerprint, session))
    try:
        await asyncio.wait_for(client.start(), 10)
        state = await asyncio.wait_for(wait_auth_state(client, {'AuthorizationStateReady'}), 10)
        assert type(state).__name__ == 'AuthorizationStateReady'
    finally:
        await asyncio.wait_for(client.stop(), 10)
    assert read_native_session_key(session / 'database') == (4, key)
    print('pinned native binlog open and close passed offline')

asyncio.run(main())
'''
    result = subprocess.run([sys.executable, '-c', program], cwd=Path(__file__).parents[1],
                            env=env, capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stderr[-2000:]
    assert 'pinned native binlog open and close passed offline' in result.stdout
