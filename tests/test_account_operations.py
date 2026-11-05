import asyncio
import json
from unittest.mock import AsyncMock, patch
import pytest

@pytest.mark.asyncio
async def test_account_operations_read_latest_blob_inside_lock():
    import server
    state = {'blob': 'initial'}
    reads = []
    db = AsyncMock()
    async def account(phone):
        reads.append(state['blob'])
        return {'id': 87654, 'phone': phone, 'status': 'active', 'proxy_id': None,
                'fingerprint_json': json.dumps({'lang_pack': 'tdesktop'}),
                'session_blob_base64': state['blob']}
    db.get_account.side_effect = account
    class Client:
        def __init__(self, account_id, phone, fp, blob, proxy):
            self.blob = blob
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            await asyncio.sleep(.01)
            state['blob'] = self.blob + ':saved'
        async def get_dialogs(self, limit):
            await asyncio.sleep(.01)
            return []
    with patch('server.get_db', return_value=db), patch('core.tdlib_client.TDLibClient', Client):
        await asyncio.gather(*(server._dispatch('tg_get_dialogs', {'phone': '+123456789'}) for _ in range(2)))
    assert reads == ['initial', 'initial:saved']

@pytest.mark.asyncio
async def test_dead_assigned_proxy_does_not_fall_back_to_direct_connection():
    import server
    db = AsyncMock()
    db.get_account.return_value = {'status': 'active', 'proxy_id': 44}
    db.get_proxy.return_value = {'status': 'dead'}
    with patch('server.get_db', return_value=db):
        with pytest.raises(ValueError, match='proxy'):
            async with server._account_client('+999999999'):
                pytest.fail('Connection must not start')
