"""Open an existing user authorization in a version-matched native TDLib session."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import struct
from typing import Any


def _decode_telethon(string_session: str) -> dict[str,Any]:
    if not string_session.startswith('1'):
        raise ValueError('Unsupported Telethon session version')
    raw=base64.urlsafe_b64decode(string_session[1:]+'='*(-len(string_session[1:])%4))
    if len(raw) not in (263,275):
        raise ValueError('Telethon session too short or invalid length')
    ip_length=len(raw)-259
    return dict(dc_id=raw[0],ip=str(ipaddress.ip_address(raw[1:1+ip_length])),
                port=struct.unpack('>H',raw[1+ip_length:3+ip_length])[0],auth_key=raw[-256:])


def _decode_pyrogram(string_session: str) -> dict[str,Any]:
    # Pyrogram v2 storage.SESSION_STRING_FORMAT is >BI?256sQ?.
    raw=base64.urlsafe_b64decode(string_session+'='*(-len(string_session)%4))
    if len(raw)!=struct.calcsize('>BI?256sQ?'):
        raise ValueError('Pyrogram session too short or invalid length')
    dc_id,api_id,test_mode,auth_key,user_id,is_bot=struct.unpack('>BI?256sQ?',raw)
    return dict(dc_id=dc_id,api_id=api_id,test_mode=test_mode,auth_key=auth_key,user_id=user_id,is_bot=is_bot)


def _normalize_phone(value: str | None) -> str:
    digits = (value or '').removeprefix('+')
    if not digits.isascii() or not digits.isdecimal() or len(digits) > 15:
        raise ValueError('Source user does not have a valid phone number')
    return '+' + digits


async def _validate_source(string_session: str, decoded: dict[str, Any], source_lang_pack: str,
                           phone: str | None, device_type: str | None,
                           expected_user_id: int | None):
    from telethon.tl.functions.account import GetAuthorizationsRequest

    import config
    from auth.session_transfer import fingerprint_for_pack, telethon_client

    fingerprint = fingerprint_for_pack('import:' + source_lang_pack, source_lang_pack, device_type)
    source = telethon_client(string_session, fingerprint)
    try:
        await source.connect()
        if not await source.is_user_authorized():
            raise ValueError('Source session is not authorized')
        user = await source.get_me()
        if user is None or user.id <= 0 or getattr(user, 'bot', False):
            raise ValueError('Only existing user authorizations can be imported')
        if expected_user_id is not None and user.id != expected_user_id:
            raise ValueError('Pyrogram user ID does not match the source authorization')
        actual_phone = _normalize_phone(user.phone)
        if phone is not None and _normalize_phone(phone) != actual_phone:
            raise ValueError('Requested phone does not match the source account')

        authorizations = await source(GetAuthorizationsRequest())
        current = [item for item in authorizations.authorizations if item.current]
        api_id, _ = config.credentials_for_lang_pack(source_lang_pack)
        if len(current) != 1 or current[0].api_id != api_id:
            raise ValueError('Source authorization application ID does not match source_lang_pack')
        source_key = source.session.auth_key
        if source.session.dc_id != decoded['dc_id'] or source_key is None or source_key.key != decoded['auth_key']:
            raise ValueError('Source authorization changed during validation')
        for field in ('device_model', 'system_version', 'app_version'):
            reported = getattr(current[0], field, None)
            if isinstance(reported, str) and reported.strip():
                fingerprint[field] = reported
        return user.id, actual_phone, fingerprint
    finally:
        # A native client must never overlap this validation connection.
        await source.disconnect()


async def _import_authorized(string_session: str, decoded: dict[str, Any],
                             source_lang_pack: str, phone: str | None,
                             device_type: str | None, *, expected_user_id: int | None = None):
    import config
    from auth.phone_auth import _username
    from auth.session_transfer import TRANSFER_TIMEOUT
    from core.account_lock import acquire_account_lock, release_account_lock
    from core.session_manager import cleanup_session, pack_session, session_dir
    from core.tdlib_client import make_client, wait_auth_state
    from core.tdlib_session_key import require_supported_tdlib, write_native_session_key
    from db.database import get_db

    require_supported_tdlib()
    if decoded['dc_id'] not in _PRODUCTION_DC_ADDRESSES:
        raise ValueError('Unsupported source data center')

    async with asyncio.timeout(TRANSFER_TIMEOUT):
        user_id, actual_phone, fingerprint = await _validate_source(
            string_session, decoded, source_lang_pack, phone, device_type, expected_user_id
        )
        db = await get_db()
        account_id = await db.create_account(
            phone=actual_phone, fingerprint=fingerprint, reject_existing=True
        )
        ownership = await acquire_account_lock(account_id)
        directory = session_dir(account_id)
        created_directory = False
        client = None
        stopped = False
        verified = False
        try:
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            created_directory = True
            write_native_session_key(
                directory / 'database', decoded['dc_id'], decoded['auth_key'], user_id=user_id
            )
            client = make_client(fingerprint, directory, phone=actual_phone)
            try:
                await client.start()
                state = await wait_auth_state(client, {
                    'AuthorizationStateReady', 'AuthorizationStateWaitPhoneNumber',
                    'AuthorizationStateWaitPassword', 'AuthorizationStateWaitCode',
                    'AuthorizationStateClosed',
                })
                if type(state).__name__ != 'AuthorizationStateReady':
                    raise RuntimeError('The existing authorization could not be opened by TDLib')
                user = await client.api.get_me()
                if user.id != user_id or _normalize_phone(user.phone_number) != actual_phone:
                    raise ValueError('Native session identity does not match the source authorization')
                sessions = await client.api.get_active_sessions()
                current = [item for item in sessions.sessions if item.is_current]
                api_id, _ = config.credentials_for_lang_pack(fingerprint['lang_pack'])
                if len(current) != 1 or current[0].api_id != api_id:
                    raise ValueError('Native authorization application ID does not match source_lang_pack')
                verified = True
            finally:
                # Logging out this client would revoke the original key too.
                await client.stop()
                stopped = True

            await db.update_account(
                actual_phone, session_blob_base64=pack_session(directory), status='active',
                tg_user_id=user.id, first_name=user.first_name, last_name=user.last_name or '',
                username=_username(user),
            )
            cleanup_session(account_id)
            return {
                'status': 'active', 'account_id': account_id, 'phone': actual_phone,
                'user_id': user.id, 'transfer': 'reuse_current_session',
            }
        except BaseException:
            if not verified and (client is None or stopped):
                if created_directory:
                    cleanup_session(account_id)
                await db.delete_account(actual_phone)
            # Keep a verified session in its private RAM directory if saving fails.
            raise
        finally:
            release_account_lock(ownership)


async def import_telethon_session(string_session: str, phone: str | None = None,
                                  device_type: str | None = None, *,
                                  source_lang_pack: str | None = None):
    """Import an existing Telethon authorization with its original application identity."""
    from auth.session_transfer import resolve_source_pack

    pack = resolve_source_pack(source_lang_pack)
    decoded = _decode_telethon(string_session)
    canonical_session = encode_telethon_session(decoded['dc_id'], decoded['auth_key'])
    return await _import_authorized(canonical_session, decoded, pack, phone, device_type)


_PRODUCTION_DC_ADDRESSES = {
    1: '149.154.175.53',
    2: '149.154.167.51',
    3: '149.154.175.100',
    4: '149.154.167.91',
    5: '91.108.56.130',
}


def telethon_string_from_pyrogram(decoded: dict[str, Any]) -> str:
    """Serialize a production Pyrogram user authorization for Telethon transport."""
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    if decoded['test_mode'] or decoded['is_bot']:
        raise ValueError('Only production user sessions can be imported')
    return encode_telethon_session(decoded['dc_id'], decoded['auth_key'])


def encode_telethon_session(dc_id: int, auth_key: bytes) -> str:
    from telethon.sessions import StringSession
    from telethon.crypto import AuthKey

    address = _PRODUCTION_DC_ADDRESSES.get(dc_id)
    if address is None:
        raise ValueError('Unsupported source data center')
    session = StringSession()
    session.set_dc(dc_id, address, 443)
    session.auth_key = AuthKey(auth_key)
    return session.save()


async def import_pyrogram_session(string_session: str, phone: str | None = None,
                                  device_type: str | None = None, *,
                                  source_lang_pack: str | None = None):
    """Import an existing Pyrogram authorization with its embedded application ID."""
    from auth.session_transfer import resolve_source_pack

    decoded = _decode_pyrogram(string_session)
    pack = resolve_source_pack(source_lang_pack, decoded['api_id'])
    source_session = telethon_string_from_pyrogram(decoded)
    return await _import_authorized(
        source_session, decoded, pack, phone, device_type, expected_user_id=decoded['user_id']
    )
