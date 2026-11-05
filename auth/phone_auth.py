"""Phone authorization keeps the live TDLib client until the flow completes."""
from __future__ import annotations
import asyncio
import re
import time
from typing import Any

from core.fingerprints import generate_fingerprint, fingerprint_from_json
from core.session_manager import session_dir, pack_session, cleanup_session
from core.tdlib_client import make_client, wait_auth_state
from db.database import get_db

_PHONE_SESSIONS: dict[str, dict[str, Any]] = {}
_FLOW_LOCK = asyncio.Lock()
AUTH_TTL = 300


async def _discard(phone, flow):
    expiry = flow.get('expiry')
    if expiry and expiry is not asyncio.current_task():
        expiry.cancel()
    try:
        await flow['client'].stop()
    finally:
        cleanup_session(flow['account_id'])
        _PHONE_SESSIONS.pop(phone,None)
        db = await get_db()
        await db.update_account(phone,status='revoked')


async def _expire_sessions():
    expired = [(phone,flow) for phone,flow in list(_PHONE_SESSIONS.items())
               if time.monotonic() - flow['created'] > AUTH_TTL]
    await asyncio.gather(*(_discard(phone,flow) for phone,flow in expired),return_exceptions=True)


async def start_phone_auth(phone: str, device_type: str | None = None):
    if not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone):
        raise ValueError('Phone must use international format')
    async with _FLOW_LOCK:
        await _expire_sessions()
        if phone in _PHONE_SESSIONS:
            return {'status':'awaiting_code','phone':phone,'account_id':_PHONE_SESSIONS[phone]['account_id']}
        db = await get_db()
        existing = await db.get_account(phone)
        if existing and existing['status'] == 'active':
            raise ValueError('Account already has an active session')
        fp = fingerprint_from_json(existing['fingerprint_json']) if existing else generate_fingerprint(phone, device_type)
        # Validate application identity before writing an account record.
        import config
        config.credentials_for_lang_pack(fp['lang_pack'])
        account_id = await db.create_account(phone=phone,fingerprint=fp)
        proxy = await db.get_best_available_proxy()
        if proxy:
            await db.update_account(phone,proxy_id=proxy['id'])
        directory = session_dir(account_id)
        if directory.exists():
            raise RuntimeError('A private RAM session requires recovery before starting authorization')
        directory.mkdir(parents=True,exist_ok=False,mode=0o700)
        client = make_client(fp,directory,proxy,phone)
        try:
            await client.start()
            await wait_auth_state(client, {'AuthorizationStateWaitPhoneNumber'})
            await client.api.set_authentication_phone_number(phone_number=phone,settings=None)
            state = await wait_auth_state(client, {'AuthorizationStateWaitCode','AuthorizationStateWaitPassword','AuthorizationStateReady'})
        except BaseException:
            await _discard(phone,dict(client=client,account_id=account_id))
            raise
        flow = dict(client=client,account_id=account_id,directory=directory,created=time.monotonic())
        _PHONE_SESSIONS[phone] = flow
        flow['expiry'] = asyncio.create_task(_expire_after(phone,flow))
        status = 'awaiting_password' if type(state).__name__ == 'AuthorizationStateWaitPassword' else 'awaiting_code'
        await db.update_account(phone,status=status)
        await db.log_auth_event(account_id,'phone_sent')
        return {'status':status,'phone':phone,'account_id':account_id}


async def submit_auth_code(phone: str, code: str, password_2fa: str | None = None):
    async with _FLOW_LOCK:
        await _expire_sessions()
        flow = _PHONE_SESSIONS.get(phone)
        if not flow:
            raise ValueError('No live phone authorization; start a new phone flow')
        client, account_id = flow['client'], flow['account_id']
        state = await client.api.get_authorization_state()
        if type(state).__name__ == 'AuthorizationStateWaitCode':
            await client.api.check_authentication_code(code=code)
            state = await wait_auth_state(client,{'AuthorizationStateWaitPassword','AuthorizationStateReady'})
        if type(state).__name__ == 'AuthorizationStateWaitPassword':
            if password_2fa is None:
                return {'status':'awaiting_password','phone':phone,'account_id':account_id}
            await client.api.check_authentication_password(password=password_2fa)
            state = await wait_auth_state(client,{'AuthorizationStateReady'})
        if type(state).__name__ != 'AuthorizationStateReady':
            raise RuntimeError('Authorization is not ready')
        me = await client.api.get_me()
        try:
            await client.stop()
            blob = pack_session(flow['directory'])
            db = await get_db()
            await db.update_account(phone,session_blob_base64=blob,status='active',tg_user_id=me.id,
                first_name=me.first_name,last_name=me.last_name or '',username=_username(me))
        except BaseException:
            flow['expiry'].cancel()
            _PHONE_SESSIONS.pop(phone,None)
            raise RuntimeError('Authenticated session requires recovery from its private RAM directory; persistence did not complete') from None
        cleanup_session(account_id)
        del _PHONE_SESSIONS[phone]
        flow['expiry'].cancel()
        await db.log_auth_event(account_id,'auth_complete')
        return {'status':'active','phone':phone,'account_id':account_id,'user_id':me.id,'first_name':me.first_name,'username':_username(me)}


def _username(me):
    names = getattr(getattr(me,'usernames',None),'active_usernames',None)
    return names[0] if names else getattr(me,'username','') or ''


async def close_phone_sessions():
    await asyncio.gather(*(_discard(phone,flow) for phone,flow in list(_PHONE_SESSIONS.items())),return_exceptions=True)


async def _expire_after(phone, flow):
    await asyncio.sleep(AUTH_TTL)
    async with _FLOW_LOCK:
        if _PHONE_SESSIONS.get(phone) is flow:
            await _discard(phone,flow)
