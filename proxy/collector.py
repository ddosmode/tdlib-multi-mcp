"""
proxy/collector.py — Automatic SOCKS5 proxy list collector for tdlib-multi-mcp.

Sources (all public, regularly updated, no auth required):
  - monosans/proxy-list (GitHub raw)
  - proxifly/free-proxy-list (GitHub raw)
  - TheSpeedX/PROXY-List (GitHub raw)
  - proxy-list.download API

Collected proxies are stored in proxies.db with status='unchecked'.
The validator (proxy/validator.py) checks them before they are used.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from db.database import get_db
import config

log = logging.getLogger(__name__)

# ── Proxy sources ──────────────────────────────────────────────────────────────

PROXY_SOURCES = [
    {
        "name":     "monosans-socks5",
        "url":      "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "protocol": "socks5",
        "format":   "host:port",
    },
    {
        "name":     "monosans-http",
        "url":      "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
        "protocol": "http",
        "format":   "host:port",
    },
    {
        "name":     "TheSpeedX-socks5",
        "url":      "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "protocol": "socks5",
        "format":   "host:port",
    },
    {
        "name":     "proxifly-socks5",
        "url":      "https://cdn.proxifly.dev/free-proxy-list/all/socks5/format=text",
        "protocol": "socks5",
        "format":   "host:port",
    },
    {
        "name":     "proxy-list-download-socks5",
        "url":      "https://www.proxy-list.download/api/v1/get?type=socks5",
        "protocol": "socks5",
        "format":   "host:port",
    },
]

_HOST_PORT_RE = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})$")


def _parse_host_port(line: str) -> tuple[str, int] | None:
    """Parse 'host:port' lines. Returns (host, port) or None on failure."""
    line = line.strip()
    m = _HOST_PORT_RE.match(line)
    if not m:
        return None
    host = m.group(1)
    port = int(m.group(2))
    if not (1 <= port <= 65535):
        return None
    return host, port


async def _fetch_source(
    session: aiohttp.ClientSession,
    source: dict[str, Any],
) -> list[tuple[str, int, str]]:
    """Fetch a single proxy source and return list of (host, port, protocol)."""
    try:
        async with session.get(source["url"], timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                log.warning("Source %s returned HTTP %d", source["name"], resp.status)
                return []
            text = await resp.text()
    except Exception as exc:
        log.warning("Failed to fetch source %s: %s", source["name"], exc)
        return []

    results: list[tuple[str, int, str]] = []
    for line in text.splitlines():
        parsed = _parse_host_port(line)
        if parsed:
            host, port = parsed
            results.append((host, port, source["protocol"]))

    log.info("Source %s: fetched %d proxies", source["name"], len(results))
    return results


async def collect_proxies(sources: list[dict] | None = None) -> int:
    """
    Fetch proxies from all configured sources and insert new ones into the DB.

    Args:
        sources: Override the default PROXY_SOURCES list (for testing).

    Returns:
        Number of newly inserted proxy records.
    """
    sources = sources or PROXY_SOURCES
    db      = await get_db()

    async with aiohttp.ClientSession(
        headers={"User-Agent": "tdlib-multi-mcp/0.1 proxy-collector"}
    ) as session:
        tasks   = [_fetch_source(session, src) for src in sources]
        results = await asyncio.gather(*tasks)

    inserted = 0
    for batch in results:
        for host, port, protocol in batch:
            try:
                await db.upsert_proxy(host, port, protocol, source="auto")
                inserted += 1
            except Exception:
                pass  # Duplicate or DB error — skip silently

    log.info("collect_proxies: %d proxy records upserted into DB", inserted)
    return inserted
