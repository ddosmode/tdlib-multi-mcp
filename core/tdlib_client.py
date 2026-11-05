"""
core/tdlib_client.py — Async TDLib client wrapper for tdlib-multi-mcp.

Wraps aiotdlib.Client to integrate:
  - Device fingerprints (from core/fingerprints.py)
  - Session storage on RAM-disk (from core/session_manager.py)
  - SOCKS5 proxy support

Each account gets its own Client instance. Clients are managed by the
AccountPool singleton so only one Client per account is active at a time.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import config
from core.fingerprints import DeviceFingerprint, fingerprint_from_json
from core.session_manager import session_dir, unpack_session, pack_session, cleanup_session

log = logging.getLogger(__name__)

try:
    from aiotdlib import Client
    from aiotdlib.api import API, AuthorizationState
    AIOTDLIB_AVAILABLE = True
except ImportError:
    AIOTDLIB_AVAILABLE = False
    log.warning("aiotdlib not installed — TDLib features unavailable until 'pip install aiotdlib'")


class TDLibClient:
    """
    Async context-managed wrapper around one aiotdlib.Client instance.

    Usage:
        async with TDLibClient(account_id=1, phone="+79001234567",
                               fingerprint=fp, session_blob=blob,
                               proxy={"host":..., "port":...}) as client:
            await client.send_message(chat_id=..., text="hello")
    """

    def __init__(
        self,
        account_id: int,
        phone: str,
        fingerprint: DeviceFingerprint,
        session_blob: Optional[str] = None,
        proxy: Optional[dict[str, Any]] = None,
    ) -> None:
        self.account_id   = account_id
        self.phone        = phone
        self.fingerprint  = fingerprint
        self.session_blob = session_blob
        self.proxy        = proxy
        self._client: Optional["Client"] = None
        self._sdir: Path = session_dir(account_id)

    async def __aenter__(self) -> "TDLibClient":
        await self._start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._stop()

    async def _start(self) -> None:
        """Unpack session blob to RAM-disk, then start the TDLib client."""
        if not AIOTDLIB_AVAILABLE:
            raise RuntimeError("aiotdlib is not installed. Run: pip install aiotdlib")

        # Restore session from DB blob if we have one
        if self.session_blob:
            log.debug("Unpacking session for account %d to %s", self.account_id, self._sdir)
            unpack_session(self.session_blob, self._sdir)
        else:
            self._sdir.mkdir(parents=True, exist_ok=True)

        # Build proxy settings for aiotdlib
        proxy_settings = None
        if self.proxy:
            proxy_settings = {
                "server": self.proxy["host"],
                "port":   self.proxy["port"],
                "type":   {"@type": "proxyTypeSocks5"},
            }

        fp = self.fingerprint
        self._client = Client(
            api_id=config.TG_API_ID,
            api_hash=config.TG_API_HASH,
            phone_number=self.phone,
            database_encryption_key="",
            files_directory=str(self._sdir),
            device_model=fp["device_model"],
            system_version=fp["system_version"],
            application_version=fp["app_version"],
            system_language_code=fp["system_language_code"],
            proxy_server=proxy_settings,
            use_message_database=True,
            use_file_database=True,
            verbosity_level=config.TDLIB_VERBOSITY,
        )
        await self._client.start()
        log.info("TDLib client started for account %d (%s)", self.account_id, self.phone)

    async def _stop(self) -> None:
        """Stop the TDLib client, pack session back to blob, clean up RAM-disk."""
        if self._client:
            await self._client.stop()
            self._client = None
        # Pack updated session back to blob
        if self._sdir.exists():
            self.session_blob = pack_session(self._sdir)
            cleanup_session(self.account_id)
        log.info("TDLib client stopped for account %d", self.account_id)

    # ── Public helpers ─────────────────────────────────────────────────────────

    @property
    def raw(self) -> "Client":
        """Access the underlying aiotdlib.Client."""
        if not self._client:
            raise RuntimeError("TDLibClient is not started. Use as async context manager.")
        return self._client

    async def get_me(self) -> dict[str, Any]:
        result = await self.raw.api.get_me()
        return result.dict()

    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        from aiotdlib.api import InputMessageText, FormattedText
        msg = await self.raw.api.send_message(
            chat_id=chat_id,
            input_message_content=InputMessageText(
                text=FormattedText(text=text, entities=[]),
                link_preview_options=None,
                clear_draft=False,
            ),
        )
        return msg.dict()

    async def get_dialogs(self, limit: int = 20) -> list[dict[str, Any]]:
        chats = await self.raw.api.get_chats(chat_list=None, limit=limit)
        return [{"id": cid} for cid in chats.chat_ids]

    async def get_chat_history(
        self, chat_id: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        result = await self.raw.api.get_chat_history(
            chat_id=chat_id,
            from_message_id=0,
            offset=0,
            limit=limit,
            only_local=False,
        )
        return [m.dict() for m in result.messages]
