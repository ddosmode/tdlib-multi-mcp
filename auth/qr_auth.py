"""QR authorization with opaque polling handles and bounded session lifetime."""
from __future__ import annotations
import asyncio
import secrets
import time
from typing import Any

from core.fingerprints import generate_fingerprint
from core.session_manager import session_dir,pack_session,cleanup_session
from core.tdlib_client import make_client,wait_auth_state
from db.database import get_db
from auth.session_transfer import discard_native_flow, release_flow_lock
from core.account_lock import acquire_account_lock, release_account_lock
from auth.phone_auth import _username

_QR_SESSIONS: dict[str,dict[str,Any]] = {}
_LOCK = asyncio.Lock()
AUTH_TTL = 300


async def _discard(token, flow):
    expiry = flow.get('expiry')
    if expiry and expiry is not asyncio.current_task():
        expiry.cancel()
    try:
        await discard_native_flow(flow)
    finally:
        _QR_SESSIONS.pop(token,None)
        db = await get_db()
        await db.update_account(flow['placeholder'],status='revoked')


async def start_qr_auth(device_type: str | None = None, *, _fingerprint: dict | None = None):
    async with _LOCK:
        for token,flow in list(_QR_SESSIONS.items()):
            if time.monotonic()-flow['created']>AUTH_TTL:
                await _discard(token,flow)
        placeholder = 'qr_'+secrets.token_hex(12)
        fp = _fingerprint or generate_fingerprint(placeholder,device_type)
        import config
        config.credentials_for_lang_pack(fp['lang_pack'])
        db = await get_db()
        account_id = await db.create_account(phone=placeholder,fingerprint=fp)
        process_lock = await acquire_account_lock(account_id)
        try:
            proxy = await db.reserve_proxy_for_account(placeholder)
            if proxy:
                from proxy.rotator import ensure_ready_proxy
                proxy = await ensure_ready_proxy(placeholder)
            directory = session_dir(account_id)
            if directory.exists():
                raise RuntimeError('A private RAM session requires recovery before starting authorization')
            directory.mkdir(parents=True,exist_ok=False,mode=0o700)
            client = make_client(fp,directory,proxy)
        except BaseException:
            release_account_lock(process_lock)
            raise
        try:
            await client.start()
            await wait_auth_state(client,{'AuthorizationStateWaitPhoneNumber'})
            await client.api.request_qr_code_authentication(other_user_ids=[])
            state = await wait_auth_state(client,{'AuthorizationStateWaitOtherDeviceConfirmation'})
        except BaseException:
            await _discard('',dict(client=client,account_id=account_id,placeholder=placeholder,process_lock=process_lock))
            raise
        token = secrets.token_urlsafe(24)
        flow = dict(client=client,account_id=account_id,placeholder=placeholder,directory=directory,created=time.monotonic(),process_lock=process_lock)
        _QR_SESSIONS[token] = flow
        flow['expiry'] = asyncio.create_task(_expire_after(token,flow))
        await db.update_account(placeholder,status='qr_pending')
        await db.log_auth_event(account_id,'qr_requested')
        return {'status':'qr_pending','qr_link':state.link,'token':token,'account_id':account_id}


async def check_qr_auth(token: str, password_2fa: str | None = None, *, _expected_user_id: int | None = None):
    async with _LOCK:
        flow = _QR_SESSIONS.get(token)
        if not flow:
            return {'status':'expired'}
        db = await get_db()
        if time.monotonic()-flow['created']>AUTH_TTL:
            await _discard(token,flow)
            await db.update_account(flow['placeholder'],status='revoked')
            return {'status':'expired'}
        client = flow['client']
        state = await client.api.get_authorization_state()
        name = type(state).__name__
        if name == 'AuthorizationStateWaitOtherDeviceConfirmation':
            return {'status':'pending','token':token,'qr_link':state.link}
        if name == 'AuthorizationStateWaitPassword':
            if password_2fa is None:
                return {'status':'awaiting_password','token':token}
            await client.api.check_authentication_password(password=password_2fa)
            state = await wait_auth_state(client,{'AuthorizationStateReady'})
            name = type(state).__name__
        if name != 'AuthorizationStateReady':
            await _discard(token,flow)
            await db.update_account(flow['placeholder'],status='revoked')
            return {'status':'expired'}
        me = await client.api.get_me()
        expected_user_id = _expected_user_id if _expected_user_id is not None else flow.get('expected_user_id')
        if expected_user_id is not None and me.id != expected_user_id:
            await _discard(token,flow)
            raise ValueError('Destination account identity does not match the source')
        phone = '+'+str(me.phone_number).lstrip('+')
        existing = await db.get_account(phone)
        if existing:
            await _discard(token,flow)
            raise ValueError('This phone already has an account record')
        try:
            await client.stop()
            blob = pack_session(flow['directory'])
            await db.update_account(flow['placeholder'],phone=phone,session_blob_base64=blob,status='active',tg_user_id=me.id,
                first_name=me.first_name,last_name=me.last_name or '',username=_username(me))
        except BaseException:
            flow['expiry'].cancel()
            _QR_SESSIONS.pop(token,None)
            release_flow_lock(flow)
            raise RuntimeError('Authenticated session requires recovery from its private RAM directory; persistence did not complete') from None
        cleanup_session(flow['account_id'])
        release_flow_lock(flow)
        _QR_SESSIONS.pop(token,None)
        flow['expiry'].cancel()
        await db.log_auth_event(flow['account_id'],'qr_auth_complete')
        return {'status':'active','account_id':flow['account_id'],'phone':phone,'user_id':me.id,'first_name':me.first_name}


async def close_qr_sessions():
    await asyncio.gather(*(_discard(token,flow) for token,flow in list(_QR_SESSIONS.items())),return_exceptions=True)


async def _expire_after(token, flow):
    await asyncio.sleep(AUTH_TTL)
    async with _LOCK:
        if _QR_SESSIONS.get(token) is flow:
            await _discard(token,flow)
            db = await get_db()
            await db.update_account(flow['placeholder'],status='revoked')


async def cancel_qr_auth(token: str):
    """Close a pending QR flow after a failed transfer without touching its source."""
    async with _LOCK:
        flow = _QR_SESSIONS.get(token)
        if flow:
            await _discard(token,flow)
