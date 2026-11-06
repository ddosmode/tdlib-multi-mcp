"""
proxy/rotator.py — Proxy assignment and auto-rotation for tdlib-multi-mcp.

Responsibilities:
  - Assign a fresh validated proxy to a new account.
  - Detect when an account's proxy has died.
  - Automatically replace a dead proxy with a live one from the pool.
  - Trigger background pool refresh when pool falls below minimum size.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from db.database import get_db
import config

log = logging.getLogger(__name__)


async def assign_proxy_to_account(phone: str) -> Optional[dict[str, Any]]:
    db = await get_db()
    return await db.reserve_proxy_for_account(phone)


async def rotate_proxy_for_account(phone: str) -> Optional[dict[str, Any]]:
    """Reserve a replacement without ever falling back to a direct connection."""
    db = await get_db()
    row = await db.get_account(phone)
    if not row:
        raise ValueError("Account not found")
    if row.get("proxy_id"):
        await db.update_proxy_status(row["proxy_id"], status="dead")
    return await db.reserve_proxy_for_account(phone)


async def ensure_ready_proxy(phone: str, max_attempts: int = 3) -> Optional[dict[str, Any]]:
    """Validate an assigned route before TDLib starts; replace failed routes."""
    from proxy.validator import validate_proxy_record
    db = await get_db()
    account = await db.get_account(phone)
    if not account:
        raise ValueError("Account not found")
    if not account.get("proxy_id"):
        return None
    row = await db.get_proxy(account["proxy_id"])
    for _ in range(max_attempts):
        if row and row["status"] == "alive":
            if (await validate_proxy_record(row))["alive"]:
                return row
        row = await rotate_proxy_for_account(phone)
        if row is None:
            break
    raise ConnectionError("No validated replacement proxy is available; direct fallback is disabled")


async def rotate_if_failed(phone: str) -> bool:
    """After a connection error, rotate only if the isolated probe also fails.

    Sending is never retried automatically: a timeout may occur after delivery.
    """
    from proxy.validator import validate_proxy_record
    db = await get_db()
    account = await db.get_account(phone)
    if not account or not account.get("proxy_id"):
        return False
    row = await db.get_proxy(account["proxy_id"])
    if row and (await validate_proxy_record(row))["alive"]:
        return False
    return await rotate_proxy_for_account(phone) is not None


async def get_proxy_for_account(phone: str) -> Optional[dict[str, Any]]:
    """
    Return the currently assigned proxy for an account, or None.
    """
    db  = await get_db()
    row = await db.get_account(phone)
    if not row or not row.get("proxy_id"):
        return None
    return await db.get_proxy(row["proxy_id"])


async def get_pool_status() -> dict[str, Any]:
    """
    Return the current proxy pool statistics.

    Returns:
        {
          "alive": int,
          "dead": int,
          "unchecked": int,
          "total": int,
          "avg_latency_ms": float | None,
          "sufficient": bool
        }
    """
    db = await get_db()

    alive_proxies = await db.get_alive_proxies(limit=10000)
    alive_count   = len(alive_proxies)

    all_proxies    = await db._proxies_db.execute("SELECT status, latency_ms FROM proxies")
    rows           = await all_proxies.fetchall()

    dead_count      = sum(1 for r in rows if r[0] == "dead")
    unchecked_count = sum(1 for r in rows if r[0] in ("unchecked", "checking"))

    latencies = [r[1] for r in rows if r[0] == "alive" and r[1] is not None]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None

    return {
        "alive":          alive_count,
        "dead":           dead_count,
        "unchecked":      unchecked_count,
        "total":          len(rows),
        "avg_latency_ms": avg_latency,
        "sufficient":     alive_count >= config.PROXY_MIN_POOL_SIZE,
    }


async def _background_refresh() -> None:
    """Background task: collect new proxies and validate them."""
    try:
        from proxy.validator import ensure_pool_has_proxies
        await ensure_pool_has_proxies()
    except Exception as exc:
        log.error("Background proxy refresh failed: %s", exc)
