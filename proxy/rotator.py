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
    """
    Assign the best available alive proxy to an account.

    Args:
        phone: Account phone number.

    Returns:
        The proxy dict that was assigned, or None if no proxy available.
    """
    db    = await get_db()
    proxy = await db.get_best_available_proxy()

    if proxy:
        await db.update_account(phone, proxy_id=proxy["id"])
        log.info("Assigned proxy %s:%d to account %s", proxy["host"], proxy["port"], phone)
    else:
        log.warning("No alive proxy available for account %s", phone)

    return proxy


async def rotate_proxy_for_account(phone: str) -> Optional[dict[str, Any]]:
    """
    Mark the current proxy as dead (failed) and assign a new one.

    Call this when a TDLib connection reports a proxy failure.

    Args:
        phone: Account phone number.

    Returns:
        The new proxy dict, or None if no replacement available.
    """
    db  = await get_db()
    row = await db.get_account(phone)
    if not row:
        raise ValueError(f"Account not found: {phone}")

    old_proxy_id = row.get("proxy_id")
    if old_proxy_id:
        await db.update_proxy_status(old_proxy_id, status="dead")
        log.info("Marked proxy %d as dead for account %s", old_proxy_id, phone)

    # Try to assign a new proxy
    new_proxy = await db.get_best_available_proxy()
    if new_proxy:
        await db.update_account(phone, proxy_id=new_proxy["id"])
        log.info(
            "Rotated proxy for %s → %s:%d (latency %s ms)",
            phone,
            new_proxy["host"],
            new_proxy["port"],
            new_proxy.get("latency_ms", "?"),
        )
    else:
        await db.update_account(phone, proxy_id=None)
        log.warning("No replacement proxy available for account %s", phone)

        # Trigger background refresh
        asyncio.create_task(_background_refresh())

    return new_proxy


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
