"""
auth/session_export.py — Export TDLib session to Telethon / Pyrogram StringSession.

Extracts the dc_id and auth_key from the stored TDLib session blob and encodes
them into the respective StringSession format.

NOTE: Exported sessions should be treated as equivalent to full account access.
Never log, print or transmit them outside the secure config path.
"""

from __future__ import annotations

import base64
import logging
import struct
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _read_tdlib_key_file(session_dir: Path) -> tuple[int, bytes]:
    """
    Read dc_id and auth_key from the TDLib key bootstrap file.

    Format: dc_id (4 bytes LE) + auth_key (256 bytes)
    """
    key_file = session_dir / "td_auth_key.bin"
    if not key_file.exists():
        raise FileNotFoundError(
            f"TDLib key file not found at {key_file}. "
            "Only sessions imported via session_import.py or bootstrapped by us can be exported. "
            "Sessions created by native TDLib auth flow require reading the td.binlog directly."
        )
    raw = key_file.read_bytes()
    if len(raw) < 260:
        raise ValueError(f"Key file too short: {len(raw)} bytes")
    dc_id    = struct.unpack("<I", raw[:4])[0]
    auth_key = raw[4:260]
    return dc_id, auth_key


async def export_telethon_session(account_id: int) -> dict[str, Any]:
    """
    Export a stored TDLib session as a Telethon StringSession string.

    Args:
        account_id: The DB id of the account to export.

    Returns:
        {"string_session": "1AAAA...", "dc_id": int, "account_id": int}

    CAUTION: The returned string_session grants full account access. Store securely.
    """
    from core.session_manager import session_dir, unpack_session, cleanup_session
    from db.database import get_db

    db  = await get_db()
    row = await db.get_account_by_id(account_id)
    if not row:
        raise ValueError(f"Account id {account_id} not found")
    if not row.get("session_blob_base64"):
        raise ValueError("No session blob stored for this account")

    sdir = session_dir(account_id)
    unpack_session(row["session_blob_base64"], sdir)

    try:
        dc_id, auth_key = _read_tdlib_key_file(sdir)
    finally:
        cleanup_session(account_id)

    # Telethon StringSession format:
    # version byte "1" + base64url(dc_id[1] + ip[4] + port[2] + auth_key[256])
    # We use a known DC IP table for the given dc_id.
    DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51",
              3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
    dc_ip   = DC_IPS.get(dc_id, "149.154.167.51")
    ip_bytes = bytes(int(x) for x in dc_ip.split("."))
    port    = 443

    payload = bytes([dc_id]) + ip_bytes + struct.pack(">H", port) + auth_key
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    string_session = "1" + encoded

    log.info("Exported Telethon session for account %d (dc_id=%d)", account_id, dc_id)
    return {
        "string_session": string_session,
        "dc_id":          dc_id,
        "account_id":     account_id,
        "format":         "telethon",
    }


async def export_pyrogram_session(account_id: int) -> dict[str, Any]:
    """
    Export a stored TDLib session as a Pyrogram v2 StringSession string.

    Args:
        account_id: The DB id of the account to export.

    Returns:
        {"string_session": "...", "dc_id": int, "account_id": int}
    """
    from core.session_manager import session_dir, unpack_session, cleanup_session
    from db.database import get_db

    db  = await get_db()
    row = await db.get_account_by_id(account_id)
    if not row:
        raise ValueError(f"Account id {account_id} not found")
    if not row.get("session_blob_base64"):
        raise ValueError("No session blob stored for this account")

    sdir = session_dir(account_id)
    unpack_session(row["session_blob_base64"], sdir)

    try:
        dc_id, auth_key = _read_tdlib_key_file(sdir)
    finally:
        cleanup_session(account_id)

    user_id = row.get("tg_user_id") or 0
    # Pyrogram v2 format:
    # dc_id (1 byte) + test_mode (1 byte, 0) + auth_key (256 bytes) + user_id (8 bytes LE) + is_bot (1 byte)
    payload = (
        bytes([dc_id, 0])
        + auth_key
        + struct.pack(">Q", user_id)
        + bytes([0])
    )
    string_session = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    log.info("Exported Pyrogram session for account %d (dc_id=%d)", account_id, dc_id)
    return {
        "string_session": string_session,
        "dc_id":          dc_id,
        "account_id":     account_id,
        "user_id":        user_id,
        "format":         "pyrogram",
    }
