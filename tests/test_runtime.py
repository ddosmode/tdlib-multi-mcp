"""Runtime contracts against the installed aiotdlib API; no Telegram traffic."""
import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from core import tdlib_client as runtime


def fingerprint():
    return dict(device_model='Desktop',system_version='Linux',app_version='1',system_language_code='en',lang_pack='tdesktop',lang_code='en')


def test_client_settings_match_installed_api(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.config, 'credentials_for_lang_pack', lambda pack: (2040, 'test-only'), raising=False)
    settings = runtime.build_client_settings(fingerprint(), tmp_path, {'host':'127.0.0.1','port':1080})
    assert settings.files_directory == tmp_path
    assert settings.proxy_settings.host == '127.0.0.1'
    assert settings.options.localization_target == 'tdesktop'
    assert settings.options.language_pack_id == 'en'


@pytest.mark.asyncio
async def test_import_never_fabricates_tdlib_session():
    from auth.session_import import import_telethon_session
    with pytest.raises(NotImplementedError, match='TDLib'):
        await import_telethon_session('invalid')


@pytest.mark.asyncio
async def test_export_never_claims_native_conversion():
    from auth.session_export import export_pyrogram_session
    with pytest.raises(NotImplementedError, match='TDLib'):
        await export_pyrogram_session(123)


@pytest.mark.asyncio
async def test_start_failure_releases_account_lock_even_if_stop_fails(monkeypatch):
    client = runtime.TDLibClient(991, '+123456789', fingerprint(), 'blob')
    failed = AsyncMock()
    failed.stop.side_effect = TimeoutError('close timeout')
    async def fail():
        client._client = failed
        raise RuntimeError('start failed')
    monkeypatch.setattr(client,'_start',fail)
    cleaned=[]
    monkeypatch.setattr(runtime,'cleanup_session',cleaned.append)
    with pytest.raises(TimeoutError):
        await client.__aenter__()
    assert not client._lock.locked()
    assert cleaned == []


@pytest.mark.asyncio
async def test_phone_discard_timeout_cleans_ram_and_pending_record(monkeypatch):
    from auth import phone_auth as auth
    client = AsyncMock()
    client.stop.side_effect=TimeoutError('native close timeout')
    flow=dict(client=client,account_id=992)
    auth._PHONE_SESSIONS['+123456789']=flow
    db=AsyncMock()
    monkeypatch.setattr(auth,'get_db',AsyncMock(return_value=db))
    cleaned=[]
    monkeypatch.setattr(auth,'cleanup_session',cleaned.append)
    with pytest.raises(TimeoutError):
        await auth._discard('+123456789',flow)
    assert '+123456789' not in auth._PHONE_SESSIONS
    assert cleaned == [992]
    db.update_account.assert_awaited_once_with('+123456789',status='revoked')


def auth_state(name, **fields):
    return type('AuthorizationState'+name,(),fields)()


@pytest.mark.asyncio
async def test_phone_code_password_active_flow(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from auth import phone_auth as auth
    client=AsyncMock()
    client.api.get_authorization_state.side_effect=[auth_state('WaitCode'),auth_state('WaitPassword')]
    client.api.get_me.return_value=SimpleNamespace(id=42,first_name='Test',last_name='',username='test')
    db=AsyncMock()
    monkeypatch.setattr(auth,'get_db',AsyncMock(return_value=db))
    monkeypatch.setattr(auth,'wait_auth_state',AsyncMock(side_effect=[auth_state('WaitPassword'),auth_state('Ready')]))
    monkeypatch.setattr(auth,'pack_session',lambda _: 'persisted-blob')
    monkeypatch.setattr(auth,'cleanup_session',lambda _: None)
    expiry=Mock()
    auth._PHONE_SESSIONS['+123456789']=dict(client=client,account_id=1,directory=tmp_path,created=__import__('time').monotonic(),expiry=expiry)
    result=await auth.submit_auth_code('+123456789','12345')
    assert result['status']=='awaiting_password'
    client.stop.assert_not_awaited()
    result=await auth.submit_auth_code('+123456789','',password_2fa='test-password')
    assert result['status']=='active'
    assert '+123456789' not in auth._PHONE_SESSIONS
    client.api.check_authentication_code.assert_awaited_once_with(code='12345')
    client.api.check_authentication_password.assert_awaited_once_with(password='test-password')
    assert db.update_account.await_args.kwargs['session_blob_base64']=='persisted-blob'


@pytest.mark.asyncio
async def test_qr_pending_password_active_flow(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from auth import qr_auth as auth
    client=AsyncMock()
    client.api.get_authorization_state.side_effect=[auth_state('WaitOtherDeviceConfirmation',link='tg://test'),auth_state('WaitPassword'),auth_state('WaitPassword')]
    client.api.get_me.return_value=SimpleNamespace(id=42,phone_number='123456789',first_name='Test',last_name='',username='test')
    db=AsyncMock()
    db.get_account.return_value=None
    monkeypatch.setattr(auth,'get_db',AsyncMock(return_value=db))
    monkeypatch.setattr(auth,'wait_auth_state',AsyncMock(return_value=auth_state('Ready')))
    monkeypatch.setattr(auth,'pack_session',lambda _: 'persisted-blob')
    monkeypatch.setattr(auth,'cleanup_session',lambda _: None)
    flow=dict(client=client,account_id=2,placeholder='qr_test',directory=tmp_path,created=__import__('time').monotonic(),expiry=Mock())
    auth._QR_SESSIONS['opaque']=flow
    assert (await auth.check_qr_auth('opaque'))['status']=='pending'
    assert (await auth.check_qr_auth('opaque'))['status']=='awaiting_password'
    result=await auth.check_qr_auth('opaque',password_2fa='test-password')
    assert result['status']=='active'
    assert result['phone']=='+123456789'
    assert 'opaque' not in auth._QR_SESSIONS
    assert db.update_account.await_args.kwargs['phone']=='+123456789'


@pytest.mark.asyncio
async def test_invalid_qr_state_revokes_record(monkeypatch):
    from auth import qr_auth as auth
    client=AsyncMock()
    client.api.get_authorization_state.return_value=auth_state('Closed')
    db=AsyncMock()
    monkeypatch.setattr(auth,'get_db',AsyncMock(return_value=db))
    monkeypatch.setattr(auth,'cleanup_session',lambda _: None)
    auth._QR_SESSIONS['invalid']=dict(client=client,account_id=3,placeholder='qr_test',created=__import__('time').monotonic())
    assert (await auth.check_qr_auth('invalid'))['status']=='expired'
    assert 'invalid' not in auth._QR_SESSIONS
    assert db.update_account.await_args.kwargs['status']=='revoked'


@pytest.mark.asyncio
async def test_session_snapshot_persisted_before_cleanup(monkeypatch,tmp_path):
    from db import database
    client=runtime.TDLibClient(994,'+123456789',fingerprint(),'old')
    client._sdir=tmp_path
    client._client=AsyncMock()
    events=[]
    db=AsyncMock()
    async def save(phone,blob):
        events.append(('saved',phone,blob))
    db.save_session_blob.side_effect=save
    monkeypatch.setattr(database,'get_db',AsyncMock(return_value=db))
    monkeypatch.setattr(runtime,'pack_session',lambda _: 'new')
    monkeypatch.setattr(runtime,'cleanup_session',lambda account: events.append(('cleanup',account)))
    await client._stop()
    assert events == [('saved','+123456789','new'),('cleanup',994)]


@pytest.mark.asyncio
async def test_session_snapshot_db_failure_retains_ram(monkeypatch,tmp_path):
    from db import database
    client=runtime.TDLibClient(995,'+123456789',fingerprint(),'old')
    client._sdir=tmp_path
    client._client=AsyncMock()
    db=AsyncMock()
    db.save_session_blob.side_effect=OSError('disk full')
    monkeypatch.setattr(database,'get_db',AsyncMock(return_value=db))
    monkeypatch.setattr(runtime,'pack_session',lambda _: 'new')
    cleaned=[]
    monkeypatch.setattr(runtime,'cleanup_session',cleaned.append)
    with pytest.raises(OSError,match='disk full'):
        await client._stop()
    assert cleaned == []
    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_reconnect_preserves_uncommitted_ram_snapshot(monkeypatch,tmp_path):
    client=runtime.TDLibClient(996,'+123456789',fingerprint(),'stale-blob')
    client._sdir=tmp_path
    recovery=tmp_path/'database.bin'
    recovery.write_bytes(b'only current session')
    restored=[]
    monkeypatch.setattr(runtime,'unpack_session',lambda *_:restored.append(True))
    with pytest.raises(RuntimeError,match='requires recovery'):
        await client.__aenter__()
    assert recovery.read_bytes()==b'only current session'
    assert restored==[]
    assert not client._lock.locked()
