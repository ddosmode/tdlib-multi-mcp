"""Current-session export and QR-login boundary tests with synthetic keys."""
import base64
import struct
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from auth import session_export as exports
from auth import session_import as imports
from auth import session_transfer as transfer


def session_string():
    return '1' + base64.urlsafe_b64encode(
        bytes([2, 149, 154, 167, 51]) + struct.pack('>H', 443) + b'K' * 256
    ).decode()


def test_source_identity_is_explicit_and_consistent():
    with pytest.raises(ValueError,match='explicit source_lang_pack'):
        transfer.resolve_source_pack(None)
    assert transfer.resolve_source_pack(None,2040)=='tdesktop'
    with pytest.raises(ValueError,match='does not match'):
        transfer.resolve_source_pack('android',2040)
    with pytest.raises(ValueError,match='not a supported'):
        transfer.resolve_source_pack(None,123456)
    with pytest.raises(ValueError,match='does not match'):
        transfer.fingerprint_for_pack('seed','ios','android')


def test_telethon_init_profile_matches_lang_pack():
    fp=transfer.fingerprint_for_pack('seed','android')
    client=transfer.telethon_client(session_string(),fp)
    assert client.api_id==6
    assert client._init_request.lang_pack=='android'


@pytest.mark.asyncio
async def test_qr_identity_is_checked_before_session_persistence(monkeypatch,tmp_path):
    from auth import qr_auth
    from core import session_manager
    db=AsyncMock()
    monkeypatch.setattr(qr_auth,'get_db',AsyncMock(return_value=db))
    client=AsyncMock()
    client.api.get_authorization_state.return_value=type('AuthorizationStateReady',(),{})()
    client.api.get_me.return_value=SimpleNamespace(id=999,phone_number='123456789')
    monkeypatch.setattr(session_manager,'cleanup_session',lambda _:None)
    flow=dict(client=client,account_id=1,placeholder='qr_test',created=time.monotonic(),expected_user_id=42)
    qr_auth._QR_SESSIONS['mismatch']=flow
    with pytest.raises(ValueError,match='identity'):
        await qr_auth.check_qr_auth('mismatch')
    client.api.log_out.assert_awaited_once()
    assert all('session_blob_base64' not in call.kwargs for call in db.update_account.await_args_list)
    assert 'mismatch' not in qr_auth._QR_SESSIONS


@pytest.mark.asyncio
async def test_qr_flow_holds_cross_process_lock_until_cancel(monkeypatch,tmp_path):
    import os
    from auth import qr_auth
    from core import session_manager
    from core.account_lock import acquire_account_lock,release_account_lock
    db=AsyncMock()
    db.create_account.return_value=77
    db.reserve_proxy_for_account.return_value=None
    monkeypatch.setattr(qr_auth,'get_db',AsyncMock(return_value=db))
    directory=tmp_path/'ram'
    monkeypatch.setattr(qr_auth,'session_dir',lambda _:directory)
    monkeypatch.setattr(session_manager,'cleanup_session',lambda _:None)
    client=AsyncMock()
    monkeypatch.setattr(qr_auth,'make_client',lambda *_:client)
    monkeypatch.setattr(qr_auth,'wait_auth_state',AsyncMock(side_effect=[None,SimpleNamespace(link='tg://login?token=YWJj')]))
    result=await qr_auth.start_qr_auth()
    flow=qr_auth._QR_SESSIONS[result['token']]
    os.fstat(flow['process_lock'])
    with pytest.raises(TimeoutError):
        await acquire_account_lock(77,timeout=.05)
    await qr_auth.cancel_qr_auth(result['token'])
    fd=await acquire_account_lock(77,timeout=.05)
    release_account_lock(fd)


def export_fakes(monkeypatch, tmp_path, api_id=2040):
    from core import tdlib_session_key

    native = SimpleNamespace(fingerprint={'lang_pack': 'tdesktop'}, raw=AsyncMock())
    native.raw.api.get_active_sessions.return_value = SimpleNamespace(
        sessions=[SimpleNamespace(is_current=True, api_id=api_id)],
    )
    native.raw.api.get_me.return_value = SimpleNamespace(id=42, type_=object())

    @asynccontextmanager
    async def use_native(_):
        yield native

    def read_stopped_db(path):
        assert path == tmp_path / 'database'
        native.raw.stop.assert_awaited()
        return 2, bytes(range(256))

    db = AsyncMock()
    db.get_account_by_id.return_value = {'phone': '+10000000001', 'status': 'active'}
    monkeypatch.setattr(exports, 'get_db', AsyncMock(return_value=db))
    monkeypatch.setattr(exports, 'account_client', use_native)
    monkeypatch.setattr(exports, 'session_dir', lambda _: tmp_path)
    monkeypatch.setattr(tdlib_session_key, 'require_supported_tdlib', lambda: None)
    monkeypatch.setattr(tdlib_session_key, 'read_native_session_key', read_stopped_db)
    return native


@pytest.mark.asyncio
async def test_export_preserves_the_same_native_key_and_profile(monkeypatch, tmp_path):
    native = export_fakes(monkeypatch, tmp_path)
    telethon = await exports.export_telethon_session(1)
    pyrogram = await exports.export_pyrogram_session(1)
    first = imports._decode_telethon(telethon['string_session'])
    second = imports._decode_pyrogram(pyrogram['string_session'])
    assert first['auth_key'] == second['auth_key'] == bytes(range(256))
    assert first['dc_id'] == second['dc_id'] == 2
    assert telethon['lang_pack'] == pyrogram['lang_pack'] == 'tdesktop'
    assert telethon['api_id'] == pyrogram['api_id'] == second['api_id'] == 2040
    assert telethon['transfer'] == pyrogram['transfer'] == 'use_current_session'
    native.raw.api.confirm_qr_code_authentication.assert_not_awaited()
    native.raw.api.check_authentication_password.assert_not_awaited()
    native.raw.api.log_out.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_refuses_mismatched_profile_before_reading_key(monkeypatch, tmp_path):
    native = export_fakes(monkeypatch, tmp_path, api_id=6)
    with pytest.raises(ValueError, match='application ID'):
        await exports.export_telethon_session(1)
    native.raw.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversion_tools_reject_password_fields():
    from server import list_tools
    from jsonschema import ValidationError, validate

    tools = {tool.name: tool for tool in await list_tools()}
    for name in ('tg_export_telethon', 'tg_export_pyrogram', 'tg_import_telethon', 'tg_import_pyrogram'):
        schema = tools[name].inputSchema
        payload = {'account_id': 1} if 'export' in name else {
            'string_session': session_string(), 'source_lang_pack': 'tdesktop',
        }
        validate(payload, schema)
        with pytest.raises(ValidationError):
            validate({**payload, 'password_file': 'unused'}, schema)
