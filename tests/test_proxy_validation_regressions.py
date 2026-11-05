from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from proxy.validator import check_proxy

@pytest.mark.asyncio
@pytest.mark.parametrize('status, body', [(407, 'authentication required'), (200, '<html>blocked</html>'), (500, '1.2.3.4')])
async def test_proxy_http_failure_is_not_alive(status, body):
    response = AsyncMock()
    response.status = status
    response.text.return_value = body
    response.__aenter__.return_value = response
    session = MagicMock()
    session.get.return_value = response
    session.__aenter__.return_value = session
    with patch('proxy.validator.aiohttp.ClientSession', return_value=session), patch('proxy.validator.ProxyConnector'):
        result = await check_proxy('1.2.3.4', 1080)
    assert result['alive'] is False
