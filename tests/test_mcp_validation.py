import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_validation_does_not_echo_input_secrets():
    from server import call_tool
    result = await call_tool('tg_send_message', {'phone': 'private', 'chat_id': 'invalid', 'text': 'SENSITIVE_TEXT'})
    assert 'SENSITIVE_TEXT' not in str(result)
    assert 'error' in result[0].text

@pytest.mark.asyncio
async def test_runtime_errors_are_sanitized():
    from server import call_tool
    with patch('server._dispatch', new=AsyncMock(side_effect=RuntimeError('SENSITIVE_TOKEN'))):
        result = await call_tool('tg_proxy_pool_status', {})
    assert 'SENSITIVE_TOKEN' not in str(result)
