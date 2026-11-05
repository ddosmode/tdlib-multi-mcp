"""
auth/qr_auth.py — QR-code authorization flow for tdlib-multi-mcp.

Flow:
  1. tg_add_account_qr()         → returns a tg://login?token=... deep link to show/scan
  2. tg_add_account_qr_check(token) → polls and returns "pending" | "active" | "expired"

The QR token is valid for ~30 seconds. The agent should call tg_add_account_qr_check
in a loop (every 2-3 seconds) until it gets "active" or "expired".
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

try:
    from aiotdlib import Client
    from aiotdlib.api import (
        AuthorizationStateWaitOtherDeviceConfirmation,
        AuthorizationStateReady,
        RequestQrCodeAuthentication,
    )
    AIOTDLIB_AVAILABLE = True
except ImportError:
    AIOTDLIB_AVAILABLE = False

import config
from core.fingerprints import generate_fingerprint
from core.session_manager import session_dir, pack_session, cleanup_session
from db.database import get_db

# In-memory map: qr_token (str) → {client, account_id, phone_placeholder}
_QR_SESSIONS: dict[str, dict[str, Any]] = {}


async def start_qr_auth(
    device_type: Optional[str] = None,
) -> dict[str, Any]:
    """
    Initiate QR-code authentication.

    Returns:
        {
          "status": "qr_pending",
          "qr_link": "tg://login?token=XXXX",
          "token": "XXXX",           ← pass to tg_add_account_qr_check
          "account_id": int
        }
    """
    if not AIOTDLIB_AVAILABLE:
        raise RuntimeError("aiotdlib not installed")

    db = await get_db()

    # Use a placeholder phone for QR auth (phone not known until authorised)
    import uuid
    placeholder_phone = f"qr_{uuid.uuid4().hex[:12]}"

    fp         = generate_fingerprint(placeholder_phone, device_type)
    account_id = await db.create_account(phone=placeholder_phone, fingerprint=fp)
    proxy      = await db.get_best_available_proxy()
    if proxy:
        await db.update_account(placeholder_phone, proxy_id=proxy["id"])

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
    # Request QR code
    await client.api.request_qr_code_authentication(other_user_ids=[])

    state = await client.api.get_authorization_state()
    if not hasattr(state, "link"):
        await client.stop()
        raise RuntimeError(f"Unexpected state after QR request: {type(state).__name__}")

    qr_link: str = state.link          # e.g. "tg://login?token=ABCDEF..."
    token = qr_link.split("token=")[-1] if "token=" in qr_link else qr_link

    _QR_SESSIONS[token] = {
        "client":    client,
        "account_id": account_id,
        "placeholder_phone": placeholder_phone,
        "sdir":      sdir,
    }

    await db.update_account(placeholder_phone, status="qr_pending")
    await db.log_auth_event(account_id, "qr_requested", f"token={token[:8]}...")

    log.info("QR auth started (account_id=%d token=%s...)", account_id, token[:8])
    return {
        "status":     "qr_pending",
        "qr_link":    qr_link,
        "token":      token,
        "account_id": account_id,
        "hint":       "Scan this link in Telegram on another device. Call tg_add_account_qr_check to poll.",
    }


async def check_qr_auth(token: str) -> dict[str, Any]:
    """
    Poll the status of a pending QR authentication.

    Returns:
        {"status": "pending"}   — still waiting for scan
        {"status": "active", "phone": ..., "user_id": ...}  — success
        {"status": "expired"}   — QR code expired; call tg_add_account_qr again
    """
    if not AIOTDLIB_AVAILABLE:
        raise RuntimeError("aiotdlib not installed")

    session = _QR_SESSIONS.get(token)
    if not session:
        return {"status": "expired", "hint": "Token not found or already consumed."}

    client: Client      = session["client"]
    account_id: int     = session["account_id"]
    placeholder: str    = session["placeholder_phone"]
    sdir: "Path"        = session["sdir"]

    db = await get_db()

    state = await client.api.get_authorization_state()
    state_name = type(state).__name__

    if "WaitOtherDeviceConfirmation" in state_name:
        return {"status": "pending", "token": token}

    if "Ready" in state_name:
        me = await client.api.get_me()
        await client.stop()

        blob = pack_session(sdir)
        cleanup_session(account_id)

        # Update account with real phone
        real_phone = str(me.phone_number) if hasattr(me, "phone_number") else placeholder
        await db.update_account(
            placeholder,
            session_blob_base64=blob,
            status="active",
            tg_user_id=me.id,
            first_name=me.first_name,
            last_name=me.last_name or "",
            username=getattr(me, "username", "") or "",
        )
        await db.log_auth_event(account_id, "qr_auth_complete", f"user_id={me.id}")
        del _QR_SESSIONS[token]

        log.info("QR auth complete for account %d (user_id=%d)", account_id, me.id)
        return {
            "status":     "active",
            "account_id": account_id,
            "user_id":    me.id,
            "first_name": me.first_name,
            "username":   getattr(me, "username", "") or "",
        }

    # Expired or error
    await client.stop()
    cleanup_session(account_id)
    del _QR_SESSIONS[token]
    await db.update_account(placeholder, status="revoked")
    return {"status": "expired", "hint": "Start a new QR auth with tg_add_account_qr."}
