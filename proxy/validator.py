"""
proxy/validator.py — Isolated SOCKS5/HTTP proxy health checker for tdlib-multi-mcp.

CRITICAL SECURITY RULE:
  Proxies are validated by connecting to a NEUTRAL external endpoint
  (api.ipify.org) WITHOUT any Telegram session active.
  This guarantees that:
    1. Telegram never sees an unverified proxy.
    2. If a proxy is malicious/intercepting, it cannot compromise TDLib sessions.
    3. The auth_key and session data remain safe.

Validation flow:
  1. Try to connect to PROXY_CHECK_URL via the proxy within PROXY_CHECK_TIMEOUT seconds.
  2. If successful: mark proxy as 'alive', record latency_ms.
  3. If failed: mark proxy as 'dead'.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType

from db.database import get_db
import config

log = logging.getLogger(__name__)

CHECK_URL     = config.PROXY_CHECK_URL      # "https://api.ipify.org"
CHECK_TIMEOUT = config.PROXY_CHECK_TIMEOUT  # 8 seconds


async def check_proxy(
    host: str,
    port: int,
    protocol: str = "socks5",
    timeout: float = CHECK_TIMEOUT,
) -> dict[str, Any]:
    """
    Validate a single proxy by connecting to CHECK_URL through it.

    Returns:
        {"alive": bool, "latency_ms": int | None, "error": str | None}
    """
    proxy_type = ProxyType.SOCKS5 if protocol == "socks5" else ProxyType.HTTP

    try:
        connector = ProxyConnector(
            proxy_type=proxy_type,
            host=host,
            port=port,
            rdns=True,
        )
        t0 = time.monotonic()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                CHECK_URL,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"User-Agent": "curl/8.0"},
            ) as resp:
                await resp.text()
                latency_ms = int((time.monotonic() - t0) * 1000)

        return {"alive": True, "latency_ms": latency_ms, "error": None}

    except asyncio.TimeoutError:
        return {"alive": False, "latency_ms": None, "error": "timeout"}
    except Exception as exc:
        return {"alive": False, "latency_ms": None, "error": str(exc)[:120]}


async def validate_proxy_record(proxy_row: dict[str, Any]) -> dict[str, Any]:
    """
    Validate a proxy DB record and update its status in the DB.

    Returns the updated status dict.
    """
    db      = await get_db()
    result  = await check_proxy(
        host=proxy_row["host"],
        port=proxy_row["port"],
        protocol=proxy_row.get("protocol", "socks5"),
    )
    status = "alive" if result["alive"] else "dead"
    await db.update_proxy_status(
        proxy_id=proxy_row["id"],
        status=status,
        latency_ms=result.get("latency_ms"),
    )
    log.debug(
        "Proxy %s:%d → %s (%s ms)",
        proxy_row["host"],
        proxy_row["port"],
        status,
        result.get("latency_ms", "—"),
    )
    return {"proxy_id": proxy_row["id"], "status": status, **result}


async def validate_all_unchecked(
    batch_size: int = 50,
    concurrency: int = 20,
) -> dict[str, int]:
    """
    Validate all unchecked/dead proxies in the DB in parallel batches.

    Args:
        batch_size:  How many proxies to fetch from DB at once.
        concurrency: Max parallel validation coroutines.

    Returns:
        {"alive": int, "dead": int, "total": int}
    """
    db      = await get_db()
    proxies = await db.get_unchecked_proxies(limit=batch_size)
    if not proxies:
        return {"alive": 0, "dead": 0, "total": 0}

    sem = asyncio.Semaphore(concurrency)

    async def _limited(proxy: dict) -> dict:
        async with sem:
            return await validate_proxy_record(proxy)

    results = await asyncio.gather(*[_limited(p) for p in proxies])

    alive = sum(1 for r in results if r["status"] == "alive")
    dead  = len(results) - alive
    log.info("Validated %d proxies: %d alive, %d dead", len(results), alive, dead)
    return {"alive": alive, "dead": dead, "total": len(results)}


async def ensure_pool_has_proxies(
    min_alive: int = config.PROXY_MIN_POOL_SIZE,
) -> dict[str, Any]:
    """
    Ensure the proxy pool has at least `min_alive` validated proxies.
    If the pool is too small, triggers collection + validation automatically.

    Returns pool status after the operation.
    """
    from proxy.collector import collect_proxies

    db    = await get_db()
    alive = await db.count_alive_proxies()

    if alive < min_alive:
        log.info("Pool too small (%d < %d), collecting new proxies...", alive, min_alive)
        await collect_proxies()
        await validate_all_unchecked(batch_size=200, concurrency=30)
        alive = await db.count_alive_proxies()

    return {"alive_count": alive, "min_required": min_alive, "sufficient": alive >= min_alive}
