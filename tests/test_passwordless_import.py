"""Verify same-key import orchestration with SQLite and no Telegram connections."""

import base64
import inspect
import struct
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from auth import session_import, session_transfer
from core import tdlib_client
from db import database


def telethon_string():
    raw = struct.pack('>B4sH256s', 2, bytes([149, 154, 167, 51]), 443, b'T' * 256)
    return '1' + base64.urlsafe_b64encode(raw).decode()


@pytest_asyncio.fixture
async def import_setup(tmp_path, monkeypatch):
    ram = tmp_path / 'ram'
    ram.mkdir()
    monkeypatch.setattr(tdlib_client.config, 'RAM_DISK_BASE', ram)
    monkeypatch.setattr(tdlib_client.config, 'DATA_DIR', tmp_path)
    db = database.Database(tmp_path / 'accounts.db', tmp_path / 'proxies.db')
    await db.connect()
    monkeypatch.setattr(database, 'get_db', AsyncMock(return_value=db))

    source = AsyncMock()
    source.session = SimpleNamespace(dc_id=2, auth_key=SimpleNamespace(key=b'T' * 256))
    source.is_user_authorized.return_value = True
    source.get_me.return_value = SimpleNamespace(id=42, phone='123456789', bot=False)
    source.return_value = SimpleNamespace(authorizations=[SimpleNamespace(
        current=True, api_id=2040, device_model='Source desktop',
        system_version='Source system', app_version='Source version',
    )])
    monkeypatch.setattr(session_transfer, 'telethon_client', Mock(return_value=source))

    native = AsyncMock()
    native.api.get_me.return_value = SimpleNamespace(
        id=42, phone_number='123456789', first_name='Example', last_name='', username='',
    )
    native.api.get_active_sessions.return_value = SimpleNamespace(sessions=[
        SimpleNamespace(is_current=True, api_id=2040),
        SimpleNamespace(is_current=False, api_id=6),
    ])
    ready = type('AuthorizationStateReady', (), {})()
    monkeypatch.setattr(tdlib_client, 'wait_auth_state', AsyncMock(return_value=ready))

    def open_native(fingerprint, directory, **kwargs):
        source.disconnect.assert_awaited_once()
        assert fingerprint['lang_pack'] == 'tdesktop'
        assert fingerprint['device_model'] == 'Source desktop'
        assert (directory / 'database' / 'td.binlog').is_file()
        return native

    monkeypatch.setattr(tdlib_client, 'make_client', Mock(side_effect=open_native))

    def write_native(directory, dc_id, auth_key, user_id):
        source.disconnect.assert_awaited_once()
        assert (dc_id, auth_key, user_id) == (2, b'T' * 256, 42)
        directory.mkdir()
        (directory / 'td.binlog').write_bytes(b'dummy native snapshot')

    codec = SimpleNamespace(
        require_supported_tdlib=Mock(), write_native_session_key=Mock(side_effect=write_native)
    )
    monkeypatch.setitem(sys.modules, 'core.tdlib_session_key', codec)
    try:
        yield db, ram, source, native, codec
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_passwordless_import_disconnects_source_and_persists_native_session(import_setup):
    db, ram, source, native, codec = import_setup
    result = await session_import.import_telethon_session(
        telethon_string(), source_lang_pack='tdesktop'
    )
    assert result['transfer'] == 'reuse_current_session'
    assert result['user_id'] == 42
    account = await db.get_account('+123456789')
    assert account['status'] == 'active'
    assert account['session_blob_base64']
    native.stop.assert_awaited_once()
    native.api.get_active_sessions.assert_awaited_once()
    native.api.log_out.assert_not_awaited()
    source.log_out.assert_not_awaited()
    assert type(source.call_args.args[0]).__name__ == 'GetAuthorizationsRequest'
    assert not list(ram.iterdir())


@pytest.mark.asyncio
async def test_source_application_mismatch_fails_before_native_open(import_setup):
    db, _, source, native, codec = import_setup
    source.return_value.authorizations[0].api_id = 6
    with pytest.raises(ValueError, match='application ID'):
        await session_import.import_telethon_session(telethon_string(), source_lang_pack='tdesktop')
    source.disconnect.assert_awaited_once()
    codec.write_native_session_key.assert_not_called()
    native.start.assert_not_awaited()
    assert await db.get_account('+123456789') is None


@pytest.mark.asyncio
async def test_duplicate_account_never_opens_native_clone(import_setup):
    db, _, _, native, codec = import_setup
    original_id = await db.create_account('+123456789', {})
    with pytest.raises(ValueError, match='already exists'):
        await session_import.import_telethon_session(telethon_string(), source_lang_pack='tdesktop')
    codec.write_native_session_key.assert_not_called()
    native.start.assert_not_awaited()
    assert (await db.get_account('+123456789'))['id'] == original_id


@pytest.mark.asyncio
async def test_mismatched_native_identity_is_closed_without_revocation(import_setup):
    db, ram, source, native, _ = import_setup
    native.api.get_me.return_value.id = 43
    with pytest.raises(ValueError, match='Native session identity'):
        await session_import.import_telethon_session(telethon_string(), source_lang_pack='tdesktop')
    native.stop.assert_awaited_once()
    native.api.log_out.assert_not_awaited()
    source.log_out.assert_not_awaited()
    assert await db.get_account('+123456789') is None
    assert not list(ram.iterdir())


@pytest.mark.asyncio
async def test_persistence_failure_retains_verified_ram_session(import_setup, monkeypatch):
    db, ram, _, native, _ = import_setup
    monkeypatch.setattr(db, 'update_account', AsyncMock(side_effect=OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        await session_import.import_telethon_session(telethon_string(), source_lang_pack='tdesktop')
    account = await db.get_account('+123456789')
    assert account['status'] == 'pending'
    assert (ram / str(account['id']) / 'database' / 'td.binlog').is_file()
    native.stop.assert_awaited_once()
    native.api.log_out.assert_not_awaited()


@pytest.mark.asyncio
async def test_pyrogram_embedded_user_id_must_match_source(import_setup):
    _, _, source, native, codec = import_setup
    raw = struct.pack('>BI?256sQ?', 2, 2040, False, b'T' * 256, 43, False)
    with pytest.raises(ValueError, match='Pyrogram user ID'):
        await session_import.import_pyrogram_session(base64.urlsafe_b64encode(raw).decode())
    source.disconnect.assert_awaited_once()
    codec.write_native_session_key.assert_not_called()
    native.start.assert_not_awaited()


def test_import_public_api_has_no_password_input():
    for method in (session_import.import_telethon_session, session_import.import_pyrogram_session):
        assert not any('password' in field for field in inspect.signature(method).parameters)


@pytest.mark.asyncio
async def test_incompatible_native_version_never_connects_source(import_setup):
    _, _, source, native, codec = import_setup
    codec.require_supported_tdlib.side_effect = RuntimeError('Unsupported native runtime')
    with pytest.raises(RuntimeError, match='Unsupported native runtime'):
        await session_import.import_telethon_session(telethon_string(), source_lang_pack='tdesktop')
    source.connect.assert_not_awaited()
    native.start.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize('current_api_ids', [[], [6], [2040, 2040]])
async def test_import_requires_exactly_one_matching_native_current_session(import_setup, current_api_ids):
    db, ram, _, native, _ = import_setup
    native.api.get_active_sessions.return_value.sessions = [
        SimpleNamespace(is_current=True, api_id=api_id) for api_id in current_api_ids
    ]
    with pytest.raises(ValueError, match='Native authorization application ID'):
        await session_import.import_telethon_session(telethon_string(), source_lang_pack='tdesktop')
    native.stop.assert_awaited_once()
    native.api.log_out.assert_not_awaited()
    assert await db.get_account('+123456789') is None
    assert not list(ram.iterdir())


@pytest.mark.asyncio
async def test_import_uses_official_endpoint_instead_of_embedded_address(import_setup):
    raw = struct.pack('>B4sH256s', 2, bytes([127, 0, 0, 1]), 12345, b'T' * 256)
    untrusted_endpoint = '1' + base64.urlsafe_b64encode(raw).decode()
    await session_import.import_telethon_session(untrusted_endpoint, source_lang_pack='tdesktop')
    actual = session_transfer.telethon_client.call_args.args[0]
    assert actual == telethon_string()
