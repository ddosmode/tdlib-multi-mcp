"""Process-isolated native ABI/auth-initialization smoke, without requesting login."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

NATIVE = Path.home()/'.local/share/tdlib-multi-mcp/native/usr/lib/x86_64-linux-gnu'


@pytest.mark.skipif(not (NATIVE/'libc++.so.1').exists(),reason='Local native dependency bundle is not installed')
def test_native_initialization_and_close_without_credentials_input():
    env=dict(os.environ)
    env['LD_LIBRARY_PATH']=str(NATIVE)+(':'+env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    source='''
import asyncio,tempfile
from pathlib import Path
from core.tdlib_client import make_client,wait_auth_state
fp=dict(device_model='Desktop',system_version='Linux',app_version='1',system_language_code='en',lang_pack='tdesktop',lang_code='en')
async def main():
 with tempfile.TemporaryDirectory(prefix='tdlib-native-') as tmp:
  client=make_client(fp,Path(tmp))
  try:
   await asyncio.wait_for(client.start(),10)
   state=await asyncio.wait_for(wait_auth_state(client,{'AuthorizationStateWaitPhoneNumber'}),10)
   assert type(state).__name__=='AuthorizationStateWaitPhoneNumber'
  finally:
   await asyncio.wait_for(client.stop(),10)
  assert not client._running
  print('native initialize + close passed')
asyncio.run(main())
'''
    result=subprocess.run([sys.executable,'-c',source],env=env,cwd=Path(__file__).parents[1],capture_output=True,text=True,timeout=35)
    assert result.returncode==0,result.stderr[-2000:]
    assert 'native initialize + close passed' in result.stdout


@pytest.mark.skipif(not (NATIVE/'libc++.so.1').exists(),reason='Local native dependency bundle is not installed')
def test_two_native_clients_share_dispatcher_and_close_independently():
    env=dict(os.environ)
    env['LD_LIBRARY_PATH']=str(NATIVE)+(':'+env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    source='''
import asyncio,tempfile
from pathlib import Path
from core.tdlib_client import make_client,wait_auth_state
fp=dict(device_model='Desktop',system_version='Linux',app_version='1',system_language_code='en',lang_pack='tdesktop',lang_code='en')
async def main():
 with tempfile.TemporaryDirectory(prefix='tdlib-native-multi-') as tmp:
  clients=[make_client(fp,Path(tmp)/str(i)) for i in range(2)]
  assert clients[0].tdjson_client.td_json is clients[1].tdjson_client.td_json
  try:
   await asyncio.wait_for(asyncio.gather(*(client.start() for client in clients)),10)
   await asyncio.wait_for(asyncio.gather(*(wait_auth_state(client,{'AuthorizationStateWaitPhoneNumber'}) for client in clients)),10)
   await asyncio.wait_for(clients[0].stop(),10)
   state=await asyncio.wait_for(clients[1].api.get_authorization_state(),10)
   assert type(state).__name__=='AuthorizationStateWaitPhoneNumber'
  finally:
   await asyncio.gather(*(client.stop() for client in clients))
  print('two native clients close independently passed')
asyncio.run(main())
'''
    result=subprocess.run([sys.executable,'-c',source],env=env,cwd=Path(__file__).parents[1],capture_output=True,text=True,timeout=35)
    assert result.returncode==0,result.stderr[-2000:]
    assert 'two native clients close independently passed' in result.stdout
