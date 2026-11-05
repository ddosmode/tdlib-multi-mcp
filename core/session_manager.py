"""
core/session_manager.py — TDLib session serialiser for tdlib-multi-mcp.

TDLib stores its state in a local directory (db.sqlite, td.binlog, …).
This module provides bidirectional conversion:

  pack_session(dir_path)  → Base64 string  (gzip-compressed tar archive)
  unpack_session(blob_b64, target_dir)      (extract to RAM-disk)

Sessions are stored in accounts.db as the `session_blob_base64` TEXT field
and unpacked to /dev/shm/tg_mcp/<account_id>/ for fast I/O with zero disk
traces after the session is closed.
"""

from __future__ import annotations

import base64
import io
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path

import config

log = logging.getLogger(__name__)


def session_dir(account_id: int) -> Path:
    """Return the RAM-disk path for a given account's TDLib session directory."""
    if type(account_id) is not int or account_id <= 0:
        raise ValueError("Account id must be a positive integer")
    return config.RAM_DISK_BASE / str(account_id)


def pack_session(directory: Path) -> str:
    """
    Pack a TDLib session directory into a Base64-encoded gzip-tar blob.

    Args:
        directory: Path to the TDLib database directory for one account.

    Returns:
        A Base64-encoded string suitable for storing in SQLite TEXT field.
    """
    if not directory.exists():
        raise FileNotFoundError(f"Session directory not found: {directory}")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Session must be a directory, not a link")

    def safe_member(member):
        if not (member.isfile() or member.isdir()):
            raise ValueError("Session archive cannot contain links or special files")
        return member

    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf,
        mode="w:gz",
        compresslevel=config.SESSION_COMPRESS_LEVEL,
        dereference=False,
    ) as tar:
        tar.add(directory, arcname="session", filter=safe_member)

    blob_bytes = buf.getvalue()
    blob_b64   = base64.b64encode(blob_bytes).decode("ascii")
    log.debug(
        "Packed session %s → %d bytes → %d base64 chars",
        directory,
        len(blob_bytes),
        len(blob_b64),
    )
    return blob_b64


def unpack_session(blob_b64: str, target_dir: Path) -> None:
    """
    Extract a Base64 session blob into the target directory (usually on RAM-disk).

    Args:
        blob_b64:   The Base64-encoded gzip-tar blob from the DB.
        target_dir: Destination path (created if it doesn't exist).
    """
    if target_dir.is_symlink():
        raise ValueError("Session target cannot be a symlink")
    if len(blob_b64) > 384 * 1024 * 1024:
        raise ValueError("Session archive is too large")
    blob_bytes = base64.b64decode(blob_b64, validate=True)
    buf = io.BytesIO(blob_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        members = []
        total_size = 0
        seen = set()
        for member in tar:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "session":
                raise ValueError("Unsafe path in session archive")
            if not (member.isfile() or member.isdir()):
                raise ValueError("Session archive cannot contain links or special files")
            if member.name == "session":
                continue
            relative = Path(*path.parts[1:])
            if relative in seen:
                raise ValueError("Duplicate path in session archive")
            seen.add(relative)
            total_size += member.size
            if total_size > 256 * 1024 * 1024 or len(members) >= 10000:
                raise ValueError("Session archive exceeds extraction limits")
            members.append((member, relative))

        # Validate everything before replacing a previous session. A staging
        # directory also prevents partially restored sessions after failures.
        target_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=target_dir.parent))
        try:
            for member, relative in members:
                destination = staging / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = tar.extractfile(member)
                    with source, destination.open("xb") as output:
                        destination.chmod(0o600)
                        shutil.copyfileobj(source, output)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            staging.rename(target_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    log.debug("Unpacked session → %s (%d files)", target_dir, len(members))


def cleanup_session(account_id: int) -> None:
    """
    Remove the RAM-disk session directory for a given account.
    Call this after packing the session back to the DB.
    """
    sdir = session_dir(account_id)
    if sdir.exists():
        shutil.rmtree(sdir)
        log.debug("Cleaned up RAM-disk session for account %d", account_id)


def session_exists_on_ramdisk(account_id: int) -> bool:
    """Return True if the session is currently unpacked on the RAM-disk."""
    return session_dir(account_id).exists()
