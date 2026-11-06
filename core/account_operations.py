"""One operation owner per account, shared by MCP and session transfer."""
from contextlib import asynccontextmanager
import asyncio
import logging

from core.fingerprints import fingerprint_from_json
from core.account_lock import acquire_account_lock, release_account_lock
from db.database import get_db
from proxy.rotator import ensure_ready_proxy, rotate_if_failed

_LOCKS: dict[str, asyncio.Lock] = {}
log = logging.getLogger(__name__)


@asynccontextmanager
async def account_client(phone: str):
    from core.tdlib_client import TDLibClient
    async with _LOCKS.setdefault(phone, asyncio.Lock()):
        db = await get_db()
        account = await db.get_account(phone)
        if not account or account["status"] != "active":
            raise ValueError("Account is not authorized")
        ownership = await acquire_account_lock(account["id"])
        try:
            # Another MCP process may have saved a newer session while we waited.
            account = await db.get_account(phone)
            if not account or account["status"] != "active":
                raise ValueError("Account is not authorized")
            proxy = await ensure_ready_proxy(phone) if account.get("proxy_id") else None
            try:
                async with TDLibClient(account["id"], phone,
                        fingerprint_from_json(account["fingerprint_json"]),
                        account.get("session_blob_base64"), proxy) as client:
                    yield client
            except (TimeoutError, ConnectionError, OSError):
                if proxy:
                    try:
                        await rotate_if_failed(phone)
                    except Exception:
                        log.error("Proxy recovery could not reserve a replacement")
                raise
        finally:
            release_account_lock(ownership)
