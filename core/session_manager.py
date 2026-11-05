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
from pathlib import Path

import config

log = logging.getLogger(__name__)


def session_dir(account_id: int) -> Path:
    """Return the RAM-disk path for a given account's TDLib session directory."""
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

    buf = io.BytesIO()
    with tarfile.open(
        fileobj=buf,
        mode="w:gz",
        compresslevel=config.SESSION_COMPRESS_LEVEL,
        dereference=True,
    ) as tar:
        tar.add(directory, arcname="session")

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
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    blob_bytes = base64.b64decode(blob_b64)
    buf = io.BytesIO(blob_bytes)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        # Security: strip leading "session/" component and filter absolute paths
        members = []
        for member in tar.getmembers():
            # Strip top-level "session" directory from archive paths
            if member.name.startswith("session/"):
                member.name = member.name[len("session/"):]
            elif member.name == "session":
                continue  # skip root dir entry
            if Path(member.name).is_absolute():
                log.warning("Skipping absolute path in archive: %s", member.name)
                continue
            members.append(member)
        tar.extractall(path=target_dir, members=members)  # type: ignore[call-overload]

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
