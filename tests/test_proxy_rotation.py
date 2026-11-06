from unittest.mock import AsyncMock, patch
import pytest

@pytest.mark.asyncio
async def test_rotation_never_clears_failed_proxy_when_pool_empty():
    from proxy.rotator import rotate_proxy_for_account
    db = AsyncMock()
    db.get_account.return_value = {'proxy_id': 8}
    db.get_best_available_proxy.return_value = None
    db.reserve_proxy_for_account.return_value = None
    with patch('proxy.rotator.get_db', return_value=db):
        assert await rotate_proxy_for_account('+12345678') is None
    assert not any(call.kwargs.get('proxy_id', 'missing') is None for call in db.update_account.call_args_list)

@pytest.mark.asyncio
async def test_preflight_replaces_failed_proxy_and_validates_replacement():
    from proxy.rotator import ensure_ready_proxy
    db = AsyncMock()
    db.get_account.return_value = {'proxy_id': 8}
    bad = {'id': 8, 'host': '192.0.2.1', 'port': 1080, 'status': 'alive'}
    good = {'id': 9, 'host': '192.0.2.2', 'port': 1081, 'status': 'alive'}
    db.get_proxy.return_value = bad
    db.reserve_proxy_for_account.return_value = good
    with patch('proxy.rotator.get_db', return_value=db), patch('proxy.validator.validate_proxy_record', new=AsyncMock(side_effect=[{'alive':False}, {'alive':True}])):
        assert await ensure_ready_proxy('+12345678') == good
    db.update_proxy_status.assert_awaited_with(8, status='dead')

@pytest.mark.asyncio
async def test_healthy_proxy_is_not_rotated_after_timeout():
    from proxy.rotator import rotate_if_failed
    db = AsyncMock()
    db.get_account.return_value = {'proxy_id':8}
    db.get_proxy.return_value = {'id':8, 'host':'192.0.2.1', 'port':1080, 'status':'alive'}
    with patch('proxy.rotator.get_db', return_value=db), patch('proxy.validator.validate_proxy_record', new=AsyncMock(return_value={'alive':True})):
        assert await rotate_if_failed('+12345678') is False
    db.reserve_proxy_for_account.assert_not_awaited()
