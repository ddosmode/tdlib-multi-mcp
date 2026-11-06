"""Export the current native authorization without starting a new login."""
from __future__ import annotations

import base64
import struct

import config
from auth.session_import import encode_telethon_session
from core.account_operations import account_client
from core.session_manager import session_dir
from db.database import get_db


async def _export_authorized(account_id: int) -> tuple[dict, bytes, int]:
    from core.tdlib_session_key import read_native_session_key, require_supported_tdlib

    require_supported_tdlib()
    db = await get_db()
    row = await db.get_account_by_id(account_id)
    if row is None or row['status'] != 'active':
        raise ValueError('Account has no authorized native session')
    async with account_client(row['phone']) as native:
        pack = native.fingerprint['lang_pack']
        api_id, _ = config.credentials_for_lang_pack(pack)
        sessions = await native.raw.api.get_active_sessions()
        current = [item for item in sessions.sessions if item.is_current]
        if len(current) != 1 or current[0].api_id != api_id:
            raise ValueError('Native authorization application ID does not match the stored language pack')
        owner = await native.raw.api.get_me()
        if type(owner.type_).__name__ == 'UserTypeBot':
            raise ValueError('Only user sessions can be exported')
        # The account lock remains held until its stopped database has been read
        # and the snapshot has been persisted again by the account context.
        await native.raw.stop()
        dc_id, key = read_native_session_key(session_dir(account_id) / 'database')
        result = {
            'status': 'active', 'account_id': account_id, 'user_id': owner.id,
            'api_id': api_id, 'lang_pack': pack, 'transfer': 'use_current_session',
        }
    return result, key, dc_id


async def export_telethon_session(account_id: int):
    """Serialize the existing authorized key in the Telethon StringSession format."""
    result, key, dc_id = await _export_authorized(account_id)
    result['string_session'] = encode_telethon_session(dc_id, key)
    result['format'] = 'telethon'
    return result


async def export_pyrogram_session(account_id: int):
    """Serialize the existing authorized key in the Pyrogram v2 format."""
    result, key, dc_id = await _export_authorized(account_id)
    payload = struct.pack('>BI?256sQ?', dc_id, result['api_id'], False,
                          key, result['user_id'], False)
    result['string_session'] = base64.urlsafe_b64encode(payload).decode().rstrip('=')
    result['format'] = 'pyrogram'
    return result
