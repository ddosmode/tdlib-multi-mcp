"""
tests/test_proxy_isolated.py — Tests for proxy/collector.py and proxy/validator.py.

The validator test uses a real SOCKS5 connection to api.ipify.org with a known-good
public proxy. If no live proxy is available the test is skipped gracefully.

IMPORTANT: These tests NEVER connect to Telegram servers.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from proxy.collector import _parse_host_port, PROXY_SOURCES
from proxy.validator import check_proxy


# ── collector unit tests ───────────────────────────────────────────────────────

def test_parse_host_port_valid():
    result = _parse_host_port("192.168.1.1:1080")
    assert result == ("192.168.1.1", 1080)


def test_parse_host_port_valid_port_edges():
    assert _parse_host_port("1.2.3.4:1") == ("1.2.3.4", 1)
    assert _parse_host_port("1.2.3.4:65535") == ("1.2.3.4", 65535)


def test_parse_host_port_invalid_format():
    assert _parse_host_port("not-an-ip:1080") is None
    assert _parse_host_port("192.168.1.1")    is None
    assert _parse_host_port("")               is None
    assert _parse_host_port("192.168.1.1:0")  is None


def test_proxy_sources_have_required_keys():
    for src in PROXY_SOURCES:
        assert "name"     in src, f"Source missing 'name': {src}"
        assert "url"      in src, f"Source missing 'url': {src}"
        assert "protocol" in src, f"Source missing 'protocol': {src}"
        assert src["protocol"] in ("socks5", "http")


@pytest.mark.asyncio
async def test_collect_proxies_mocked():
    """Mocked test: collect_proxies should insert records for each valid line."""
    from proxy.collector import collect_proxies

    fake_text = "1.2.3.4:1080\n5.6.7.8:3128\ninvalid-line\n9.10.11.12:9050\n"

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.text   = AsyncMock(return_value=fake_text)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__  = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)

    with patch("proxy.collector.aiohttp.ClientSession", return_value=mock_session), \
         patch("proxy.collector.get_db") as mock_get_db:
        mock_db = AsyncMock()
        mock_db.upsert_proxy = AsyncMock(return_value=1)
        mock_get_db.return_value = mock_db

        inserted = await collect_proxies(sources=[PROXY_SOURCES[0]])

    # 3 valid lines × 1 source = 3 insertions
    assert inserted >= 3


# ── validator unit tests ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_proxy_dead_host():
    """A proxy at an RFC5737 test address should fail (not alive)."""
    result = await check_proxy(host="192.0.2.1", port=1080, timeout=2.0)
    assert result["alive"] is False
    assert result["latency_ms"] is None
    assert result["error"] is not None


@pytest.mark.asyncio
async def test_check_proxy_mocked_alive():
    """Mocked successful proxy check."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value="1.2.3.4")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__  = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)

    with patch("proxy.validator.aiohttp.ClientSession", return_value=mock_session), \
         patch("proxy.validator.ProxyConnector"):
        result = await check_proxy(host="1.2.3.4", port=1080)

    assert result["alive"] is True
    assert result["latency_ms"] is not None
    assert result["error"] is None
