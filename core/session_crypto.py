"""Authenticated session envelopes and private per-installation key storage."""
from __future__ import annotations

import base64
import fcntl
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "tg-session:v1:"


class SessionCipher:
    def __init__(self, key_path: Path, *, require_existing: bool):
        self.key_path = key_path
        if key_path.parent.is_symlink() or key_path.parent.stat().st_uid != os.getuid():
            raise RuntimeError("Session key directory must be owned by this user")
        key_path.parent.chmod(0o700)
        lock_fd = os.open(str(key_path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                key_fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                if require_existing:
                    raise RuntimeError("Session encryption key is missing; restore its private backup") from None
                key_fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    key = AESGCM.generate_key(bit_length=256)
                    if os.write(key_fd, key) != len(key):
                        raise RuntimeError("Unable to persist the session encryption key")
                    os.fsync(key_fd)
                finally:
                    os.close(key_fd)
                key_fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
                directory_fd = os.open(key_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            try:
                info = os.fstat(key_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise RuntimeError("Session encryption key must be a private user-owned regular file with mode 600")
                key = os.read(key_fd, 33)
                if len(key) != 32:
                    raise RuntimeError("Session encryption key is invalid")
            finally:
                os.close(key_fd)
        except OSError:
            raise RuntimeError("Unable to access the private session encryption key") from None
        finally:
            os.close(lock_fd)
        self._cipher = AESGCM(key)

    @staticmethod
    def _context(account_id: int) -> bytes:
        return f"tdlib-multi-mcp:session:v1:{account_id}".encode()

    def encrypt(self, value: str, account_id: int) -> str:
        if not isinstance(value, str):
            raise ValueError("Session must be text")
        nonce = os.urandom(12)
        encrypted = self._cipher.encrypt(nonce, value.encode(), self._context(account_id))
        return PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def decrypt(self, value: str, account_id: int) -> str:
        try:
            if not value.startswith(PREFIX):
                raise ValueError
            raw = base64.b64decode(value[len(PREFIX):], altchars=b"-_", validate=True)
            if len(raw) < 28:
                raise ValueError
            return self._cipher.decrypt(raw[:12], raw[12:], self._context(account_id)).decode()
        except (ValueError, InvalidTag, UnicodeError):
            raise RuntimeError("Stored session authentication failed; preserve the database and restore its matching key/backup") from None
