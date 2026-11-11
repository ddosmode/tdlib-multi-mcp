"""
tests/test_mcp_tools.py — Integration tests for MCP tool dispatch (mocked DB).

Tests the tool dispatch layer in server.py without actual TDLib or Telegram.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_tg_list_accounts_empty():
    """tg_list_accounts returns empty list when DB has no accounts."""
    mock_db = AsyncMock()
    mock_db.list_accounts = AsyncMock(return_value=[])

    with patch("server.get_db", return_value=mock_db):
        from server import _dispatch
        result = await _dispatch("tg_list_accounts", {})

    assert result["total"] == 0
    assert result["accounts"] == []


@pytest.mark.asyncio
async def test_tg_list_accounts_strips_blob():
    """Session blob must NOT appear in tg_list_accounts output."""
    mock_db = AsyncMock()
    mock_db.list_accounts = AsyncMock(return_value=[
        {
            "id": 1,
            "phone": "+79001234567",
            "status": "active",
            "session_blob_base64": "VERY_SECRET_BLOB",
            "fingerprint_json": '{"device_model": "Pixel 8", "system_version": "Android 14", '
                                '"app_version": "10.3.2", "system_language_code": "en", '
                                '"lang_pack": "android", "lang_code": "en"}',
            "proxy_id": None,
        }
    ])

    with patch("server.get_db", return_value=mock_db):
        from server import _dispatch
        result = await _dispatch("tg_list_accounts", {})

    account = result["accounts"][0]
    assert "session_blob_base64" not in account
    assert "VERY_SECRET_BLOB"    not in json.dumps(result)
    assert account["fingerprint"]["device_model"] == "Pixel 8"


@pytest.mark.asyncio
async def test_tg_get_account_status_not_found():
    mock_db = AsyncMock()
    mock_db.get_account = AsyncMock(return_value=None)

    with patch("server.get_db", return_value=mock_db):
        from server import _dispatch
        result = await _dispatch("tg_get_account_status", {"phone": "+70000000000"})

    assert "error" in result


@pytest.mark.asyncio
async def test_tg_proxy_pool_status_called():
    """tg_proxy_pool_status should call get_pool_status."""
    mock_status = {"alive": 42, "dead": 5, "unchecked": 10, "total": 57,
                   "avg_latency_ms": 320.5, "sufficient": True}

    with patch("server.get_pool_status", new=AsyncMock(return_value=mock_status)):
        from server import _dispatch
        result = await _dispatch("tg_proxy_pool_status", {})

    assert result["alive"] == 42
    assert result["sufficient"] is True


@pytest.mark.asyncio
async def test_unknown_tool_raises():
    from server import _dispatch
    with pytest.raises(ValueError, match="Unknown tool"):
        await _dispatch("tg_does_not_exist", {})


@pytest.mark.asyncio
async def test_call_tool_returns_text_content():
    """call_tool wraps results in TextContent and never raises."""
    mock_status = {"alive": 10, "dead": 2, "unchecked": 0, "total": 12,
                   "avg_latency_ms": 150.0, "sufficient": False}

    with patch("server.get_pool_status", new=AsyncMock(return_value=mock_status)):
        from server import call_tool
        result = await call_tool("tg_proxy_pool_status", {})

    assert len(result) == 1
    assert result[0].type == "text"
    parsed = json.loads(result[0].text)
    assert parsed["alive"] == 10


@pytest.mark.asyncio
async def test_tg_delete_account_success():
    """tg_delete_account removes the account and cleans up the RAM session."""
    mock_db = AsyncMock()
    mock_db.get_account_by_id = AsyncMock(return_value={
        "id": 1,
        "phone": "qr_fixture",
        "status": "qr_pending",
    })
    mock_db.delete_account = AsyncMock()

    with (
        patch("server.get_db", return_value=mock_db),
        patch("core.session_manager.cleanup_session") as mock_cleanup,
    ):
        from server import _dispatch
        result = await _dispatch("tg_delete_account", {"account_id": 1, "confirm": True})

    assert result["deleted"] is True
    assert result["account_id"] == 1
    mock_db.delete_account.assert_awaited_once_with("qr_fixture")
    mock_cleanup.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_tg_delete_account_not_found():
    """tg_delete_account returns error dict when account_id is unknown."""
    mock_db = AsyncMock()
    mock_db.get_account_by_id = AsyncMock(return_value=None)

    with patch("server.get_db", return_value=mock_db):
        from server import _dispatch
        result = await _dispatch("tg_delete_account", {"account_id": 999, "confirm": True})

    assert "error" in result
    assert "999" in result["error"]


@pytest.mark.asyncio
async def test_paid_media_tool_schema_and_dispatch():
    from server import list_tools, _dispatch
    tools = {tool.name: tool for tool in await list_tools()}
    schema = tools["tg_send_paid_media"].inputSchema
    assert schema["properties"]["star_count"]["minimum"] == 1
    assert schema["properties"]["media_paths"]["maxItems"] == 10
    client = AsyncMock()
    client.send_paid_media = AsyncMock(return_value={"id": 123})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch("server._account_client", return_value=cm):
        result = await _dispatch("tg_send_paid_media", {
            "phone": "+123", "chat_id": -1001234, "star_count": 15,
            "media_type": "photo", "media_paths": ["/tmp/picture.jpg"]})
    assert result["sent"] is True
    client.send_paid_media.assert_awaited_once()
