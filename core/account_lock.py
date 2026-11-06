"""Cross-process account ownership for every MCP client on this computer."""
import asyncio
import fcntl
import os
import stat
import time

import config


async def acquire_account_lock(account_id: int, timeout: float = 30) -> int:
    if type(account_id) is not int or account_id <= 0:
        raise ValueError('Invalid account ID')
    path = config.DATA_DIR / f'account-{account_id}.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise PermissionError('Invalid account lock owner or type')
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Account is busy in another process') from None
                await asyncio.sleep(.05)
    except BaseException:
        os.close(fd)
        raise


def release_account_lock(fd: int) -> None:
    os.close(fd)
