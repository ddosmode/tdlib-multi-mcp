"""
auth/phone_auth.py — Phone number + code + 2FA authorization flow for tdlib-multi-mcp.

Implements a two-step async state machine:
  Step 1: tg_add_account_phone(phone)   → sends auth code via Telegram
  Step 2: tg_add_account_code(phone, code, password_2fa?)  → completes login

Both steps persist state in the accounts DB.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

try:
    from aiotdlib import Client
    from aiotdlib.api import (
        AuthorizationStateWaitPhoneNumber,
        AuthorizationStateWaitCode,
        AuthorizationStateWaitPassword,
        AuthorizationStateReady,
        SetAuthenticationPhoneNumber,
        CheckAuthenticationCode,
        CheckAuthenticationPassword,
    )
    AIOTDLIB_AVAILABLE = True
except ImportError:
    AIOTDLIB_AVAILABLE = False

import config
from core.fingerprints import generate_fingerprint, fingerprint_to_json
from core.session_manager import session_dir, pack_session, cleanup_session
from db.database import get_db


async def start_phone_auth(
    phone: str,
    device_type: Optional[str] = None,
) -> dict[str, Any]:
    """
    Step 1: Register phone number, generate fingerprint, return status.

    Returns:
        {"status": "awaiting_code", "phone": phone, "account_id": int}
    """
    if not AIOTDLIB_AVAILABLE:
        raise RuntimeError("aiotdlib not installed")

    db = await get_db()

    # Generate and persist fingerprint
    fp = generate_fingerprint(phone, device_type)
    account_id = await db.create_account(phone=phone, fingerprint=fp)

    # Reserve best available proxy
    proxy = await db.get_best_available_proxy()
    if proxy:
        await db.update_account(phone, proxy_id=proxy["id"])

    # Build a temporary Client just to send the phone number.
    # We use a fresh session dir (empty).
    sdir = session_dir(account_id)
    sdir.mkdir(parents=True, exist_ok=True)

    proxy_settings = None
    if proxy:
        proxy_settings = {
            "server": proxy["host"],
            "port":   proxy["port"],
            "type":   {"@type": "proxyTypeSocks5"},
        }

    client = Client(
        api_id=config.TG_API_ID,
        api_hash=config.TG_API_HASH,
        phone_number=phone,
        database_encryption_key="",
        files_directory=str(sdir),
        device_model=fp["device_model"],
        system_version=fp["system_version"],
        application_version=fp["app_version"],
        system_language_code=fp["system_language_code"],
        proxy_server=proxy_settings,
        verbosity_level=config.TDLIB_VERBOSITY,
    )

    # Start the client without waiting for full auth — it will stop at WaitCode
    await client.start(login=False)
    state = await client.api.get_authorization_state()

    if hasattr(state, "__class__") and "WaitPhoneNumber" in type(state).__name__:
        await client.api.set_authentication_phone_number(phone_number=phone)

    # Persist current session snapshot while awaiting code
    await client.stop()
    blob = pack_session(sdir)
    cleanup_session(account_id)

    await db.update_account(phone, session_blob_base64=blob, status="awaiting_code")
    await db.log_auth_event(account_id, "phone_sent", f"fp={fp['device_model']}")

    log.info("Auth code requested for %s (account_id=%d)", phone, account_id)
    return {"status": "awaiting_code", "phone": phone, "account_id": account_id}


async def submit_auth_code(
    phone: str,
    code: str,
    password_2fa: Optional[str] = None,
) -> dict[str, Any]:
    """
    Step 2: Submit the received auth code (and optional 2FA password).

    Returns:
        {"status": "active", "phone": phone, "account_id": int, "user_id": int}
        or {"status": "awaiting_password", ...} if 2FA is required and no password given.
    """
    if not AIOTDLIB_AVAILABLE:
        raise RuntimeError("aiotdlib not installed")

    db  = await get_db()
    row = await db.get_account(phone)
    if not row:
        raise ValueError(f"Account not found: {phone}")

    account_id = row["id"]
    from core.fingerprints import fingerprint_from_json
    from core.session_manager import unpack_session
    fp = fingerprint_from_json(row["fingerprint_json"])

    sdir = session_dir(account_id)
    if row.get("session_blob_base64"):
        unpack_session(row["session_blob_base64"], sdir)
    else:
        sdir.mkdir(parents=True, exist_ok=True)

    proxy_row = await db.get_proxy(row["proxy_id"]) if row.get("proxy_id") else None
    proxy_settings = None
    if proxy_row:
        proxy_settings = {
            "server": proxy_row["host"],
            "port":   proxy_row["port"],
            "type":   {"@type": "proxyTypeSocks5"},
        }

    client = Client(
        api_id=config.TG_API_ID,
        api_hash=config.TG_API_HASH,
        phone_number=phone,
        database_encryption_key="",
        files_directory=str(sdir),
        device_model=fp["device_model"],
        system_version=fp["system_version"],
        application_version=fp["app_version"],
        system_language_code=fp["system_language_code"],
        proxy_server=proxy_settings,
        verbosity_level=config.TDLIB_VERBOSITY,
    )
    await client.start(login=False)

    state = await client.api.get_authorization_state()
    state_name = type(state).__name__

    if "WaitCode" in state_name:
        await client.api.check_authentication_code(code=code)
        state = await client.api.get_authorization_state()
        state_name = type(state).__name__

    if "WaitPassword" in state_name:
        if not password_2fa:
            await client.stop()
            blob = pack_session(sdir)
            cleanup_session(account_id)
            await db.update_account(phone, session_blob_base64=blob, status="awaiting_password")
            return {"status": "awaiting_password", "phone": phone, "account_id": account_id}
        await client.api.check_authentication_password(password=password_2fa)
        state = await client.api.get_authorization_state()
        state_name = type(state).__name__

    if "Ready" not in state_name:
        await client.stop()
        raise RuntimeError(f"Unexpected auth state after code submit: {state_name}")

    # Auth complete — get user info and save session
    me = await client.api.get_me()
    await client.stop()

    blob = pack_session(sdir)
    cleanup_session(account_id)

    await db.update_account(
        phone,
        session_blob_base64=blob,
        status="active",
        tg_user_id=me.id,
        first_name=me.first_name,
        last_name=me.last_name or "",
        username=getattr(me, "username", "") or "",
    )
    await db.log_auth_event(account_id, "auth_complete", f"user_id={me.id}")

    log.info("Account %s authorised successfully (user_id=%d)", phone, me.id)
    return {
        "status":     "active",
        "phone":      phone,
        "account_id": account_id,
        "user_id":    me.id,
        "first_name": me.first_name,
        "username":   getattr(me, "username", "") or "",
    }
