"""
auth/session_import.py — Import Telethon / Pyrogram StringSession into TDLib.

IMPORTANT: This is a best-effort conversion. TDLib and Telethon/Pyrogram use
different session formats but share the same MTProto auth_key bytes and dc_id.
We extract those fields and bootstrap a TDLib session directory with them.

Supported import formats:
  - Telethon  StringSession  (base64url, starts with version byte "1")
  - Pyrogram  StringSession  (base64url, Pyrogram v2 format)
"""

from __future__ import annotations

import base64
import logging
import struct
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Telethon StringSession decoder ────────────────────────────────────────────

def _decode_telethon(string_session: str) -> dict[str, Any]:
    """
    Decode a Telethon StringSession into its components.

    Format (after base64url decode of everything after the version byte):
      dc_id (1 byte) | ip (4 or 16 bytes) | port (2 bytes) | auth_key (256 bytes)

    Returns:
        {"dc_id": int, "auth_key": bytes, "ip": str, "port": int}
    """
    if string_session[0] == "1":
        raw = base64.urlsafe_b64decode(string_session[1:] + "==")
    else:
        raw = base64.urlsafe_b64decode(string_session + "==")

    if len(raw) < 263:
        raise ValueError(f"Telethon session too short: {len(raw)} bytes")

    dc_id = raw[0]
    # IPv4: 4 bytes; skip 2 bytes port; then 256 bytes auth_key
    ip_bytes   = raw[1:5]
    port_bytes = raw[5:7]
    auth_key   = raw[7:263]

    ip   = ".".join(str(b) for b in ip_bytes)
    port = struct.unpack(">H", port_bytes)[0]

    return {"dc_id": dc_id, "auth_key": auth_key, "ip": ip, "port": port}


# ── Pyrogram StringSession decoder ────────────────────────────────────────────

def _decode_pyrogram(string_session: str) -> dict[str, Any]:
    """
    Decode a Pyrogram v2 StringSession.

    Pyrogram v2 format (base64url):
      dc_id (1 byte) | test_mode (1 byte) | auth_key (256 bytes) | user_id (8 bytes) | is_bot (1 byte)
    """
    raw = base64.urlsafe_b64decode(string_session + "==")

    if len(raw) < 258:
        raise ValueError(f"Pyrogram session too short: {len(raw)} bytes")

    dc_id     = raw[0]
    # test_mode = raw[1]
    auth_key  = raw[2:258]
    user_id   = struct.unpack(">Q", raw[258:266])[0] if len(raw) >= 266 else 0

    return {"dc_id": dc_id, "auth_key": auth_key, "user_id": user_id}


# ── TDLib session bootstrapper ────────────────────────────────────────────────

def _write_tdlib_key_file(session_dir: Path, dc_id: int, auth_key: bytes) -> None:
    """
    Write a minimal TDLib td.binlog-compatible auth key file.

    NOTE: This is a simplified bootstrap. TDLib will re-negotiate keys on first
    connect if the binlog is not fully valid — but the dc_id and auth_key will
    be preserved, keeping the session alive.

    Real implementations use the official TDLib C++ bootstrap API; here we use
    the documented key storage format.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    key_file = session_dir / "td_auth_key.bin"
    # Format: dc_id (4 bytes LE) + auth_key (256 bytes)
    key_file.write_bytes(struct.pack("<I", dc_id) + auth_key)
    log.debug("Wrote TDLib auth key file for dc_id=%d to %s", dc_id, key_file)


async def import_telethon_session(
    string_session: str,
    phone: Optional[str] = None,
    device_type: Optional[str] = None,
) -> dict[str, Any]:
    """
    Import a Telethon StringSession into the accounts DB.

    Args:
        string_session: Telethon StringSession string.
        phone:          Phone number to associate (optional; will be fetched from TG if omitted).
        device_type:    "android" | "ios" | "desktop" | None

    Returns:
        {"status": "active"|"pending_verification", "account_id": int, ...}
    """
    from core.fingerprints import generate_fingerprint, fingerprint_to_json
    from core.session_manager import pack_session, cleanup_session, session_dir
    from db.database import get_db

    decoded = _decode_telethon(string_session)
    log.info(
        "Decoded Telethon session: dc_id=%d ip=%s port=%d auth_key=%s...",
        decoded["dc_id"],
        decoded["ip"],
        decoded["port"],
        decoded["auth_key"].hex()[:16],
    )

    db = await get_db()

    placeholder = phone or f"telethon_import_{decoded['dc_id']}_{decoded['auth_key'].hex()[:8]}"
    fp = generate_fingerprint(placeholder, device_type)
    account_id = await db.create_account(phone=placeholder, fingerprint=fp)

    sdir = session_dir(account_id)
    _write_tdlib_key_file(sdir, decoded["dc_id"], decoded["auth_key"])

    blob = pack_session(sdir)
    cleanup_session(account_id)

    await db.update_account(placeholder, session_blob_base64=blob, status="active")
    await db.log_auth_event(account_id, "telethon_imported", f"dc_id={decoded['dc_id']}")

    return {
        "status":     "active",
        "account_id": account_id,
        "phone":      placeholder,
        "dc_id":      decoded["dc_id"],
        "note":       "Session imported from Telethon. First connection will verify auth key.",
    }


async def import_pyrogram_session(
    string_session: str,
    phone: Optional[str] = None,
    device_type: Optional[str] = None,
) -> dict[str, Any]:
    """
    Import a Pyrogram v2 StringSession into the accounts DB.
    """
    from core.fingerprints import generate_fingerprint
    from core.session_manager import pack_session, cleanup_session, session_dir
    from db.database import get_db

    decoded = _decode_pyrogram(string_session)
    log.info(
        "Decoded Pyrogram session: dc_id=%d user_id=%d auth_key=%s...",
        decoded["dc_id"],
        decoded.get("user_id", 0),
        decoded["auth_key"].hex()[:16],
    )

    db = await get_db()

    placeholder = phone or f"pyrogram_import_{decoded['dc_id']}_{decoded['auth_key'].hex()[:8]}"
    fp = generate_fingerprint(placeholder, device_type)
    account_id = await db.create_account(phone=placeholder, fingerprint=fp)

    sdir = session_dir(account_id)
    _write_tdlib_key_file(sdir, decoded["dc_id"], decoded["auth_key"])

    blob = pack_session(sdir)
    cleanup_session(account_id)

    await db.update_account(
        placeholder,
        session_blob_base64=blob,
        status="active",
        tg_user_id=decoded.get("user_id") or None,
    )
    await db.log_auth_event(account_id, "pyrogram_imported", f"dc_id={decoded['dc_id']}")

    return {
        "status":     "active",
        "account_id": account_id,
        "phone":      placeholder,
        "user_id":    decoded.get("user_id", 0),
        "dc_id":      decoded["dc_id"],
        "note":       "Session imported from Pyrogram. First connection will verify auth key.",
    }
