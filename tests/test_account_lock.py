import asyncio
import os
from pathlib import Path
import subprocess
import sys
import pytest
from core.account_lock import acquire_account_lock, release_account_lock

@pytest.mark.asyncio
async def test_another_process_cannot_acquire_owned_account():
    account_id = 981231
    fd = await acquire_account_lock(account_id)
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', f'''import asyncio
from core.account_lock import acquire_account_lock
try:
 asyncio.run(acquire_account_lock({account_id}, timeout=.1))
except TimeoutError:
 raise SystemExit(0)
raise SystemExit(1)
''', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(proc.communicate(), 5)
        assert proc.returncode == 0
    finally:
        release_account_lock(fd)
    second = await acquire_account_lock(account_id, timeout=.1)
    release_account_lock(second)
