"""
db/database.py — Async SQLite CRUD layer for tdlib-multi-mcp.

Uses aiosqlite for non-blocking I/O.
All writes go through explicit transactions for thread-safety.
"""

from __future__ import annotations

import json
import logging
import asyncio
import os
import stat
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from db.models import ALL_TABLES, CREATE_ACCOUNTS_TABLE
from core.session_crypto import SessionCipher, PREFIX
import config

log = logging.getLogger(__name__)

# The original v1 metadata did not prove that legacy WAL pages were removed.
_SESSION_ENCRYPTION_PENDING = "aes-gcm-v1"
_SESSION_ENCRYPTION_READY = "aes-gcm-v1-checkpointed"


def serialized(method):
    """Keep execute/commit pairs together on shared SQLite connections."""
    @wraps(method)
    async def locked(self, *args, **kwargs):
        async with self._write_lock:
            return await method(self, *args, **kwargs)
    return locked


class Database:
    """Async wrapper around two SQLite databases: accounts.db and proxies.db."""

    def __init__(
        self,
        accounts_path: Path = config.ACCOUNTS_DB_PATH,
        proxies_path: Path  = config.PROXIES_DB_PATH,
    ) -> None:
        self._accounts_path = accounts_path
        self._proxies_path  = proxies_path
        self._accounts_db: Optional[aiosqlite.Connection] = None
        self._proxies_db:  Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()
        self._session_cipher: Optional[SessionCipher] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _protect_sidecars(self) -> None:
        """Correct old SQLite WAL permissions without following symlinks."""
        for database_path in (self._accounts_path, self._proxies_path):
            for suffix in ("-wal", "-shm"):
                path = database_path.with_name(database_path.name + suffix)
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    continue
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError("SQLite sidecar must be a regular file")
                    os.fchmod(fd, 0o600)
                finally:
                    os.close(fd)

    async def connect(self) -> None:
        """Open both databases and ensure schema is initialised."""
        for path in (self._accounts_path, self._proxies_path):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            path.chmod(0o600)
        # Check legacy files before SQLite can open them, and again after WAL
        # initialization creates new sidecars.
        self._protect_sidecars()
        try:
            self._accounts_db = await aiosqlite.connect(self._accounts_path)
            self._proxies_db  = await aiosqlite.connect(self._proxies_path)
            for db in (self._accounts_db, self._proxies_db):
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("PRAGMA secure_delete=ON")
            await self._init_schema()
            await self._migrate_proxy_reference()
            await self._initialize_session_encryption()
            self._protect_sidecars()
        except BaseException:
            await self.close()
            raise
        log.debug("Database connected: %s, %s", self._accounts_path, self._proxies_path)

    async def close(self) -> None:
        for db in (self._accounts_db, self._proxies_db):
            if db:
                await db.close()
        self._accounts_db = self._proxies_db = None
        self._session_cipher = None

    async def _initialize_session_encryption(self) -> None:
        """Authenticate existing ciphertext and migrate legacy rows atomically."""
        await self._accounts_db.execute("CREATE TABLE IF NOT EXISTS storage_metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await self._accounts_db.commit()
        await self._accounts_db.execute("BEGIN IMMEDIATE")
        try:
            async with self._accounts_db.execute("SELECT value FROM storage_metadata WHERE name='session_encryption'") as cursor:
                version = await cursor.fetchone()
            if version and version[0] not in (_SESSION_ENCRYPTION_PENDING, _SESSION_ENCRYPTION_READY):
                raise RuntimeError("Unsupported session encryption version")
            async with self._accounts_db.execute("SELECT id,session_blob_base64 FROM accounts WHERE session_blob_base64 IS NOT NULL") as cursor:
                rows = await cursor.fetchall()
            has_encrypted = any(row[1].startswith("tg-session:") for row in rows)
            cipher = SessionCipher(self._accounts_path.with_name(self._accounts_path.name + ".session.key"),
                                   require_existing=bool(version) or has_encrypted)
            migrations = []
            for account_id, blob in rows:
                if blob.startswith("tg-session:") or version:
                    cipher.decrypt(blob, account_id)
                else:
                    migrations.append((cipher.encrypt(blob, account_id), account_id))
            checkpoint_pending = bool(migrations) or bool(version and version[0] == _SESSION_ENCRYPTION_PENDING)
            if migrations:
                await self._accounts_db.executemany("UPDATE accounts SET session_blob_base64=? WHERE id=?", migrations)
            if not version:
                status = _SESSION_ENCRYPTION_PENDING if checkpoint_pending else _SESSION_ENCRYPTION_READY
                await self._accounts_db.execute(
                    "INSERT INTO storage_metadata VALUES('session_encryption',?)",
                    (status,),
                )
            await self._accounts_db.commit()
        except BaseException:
            await self._accounts_db.rollback()
            raise

        if checkpoint_pending:
            async with self._accounts_db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
                checkpoint = await cursor.fetchone()
            if checkpoint is None or tuple(checkpoint) != (0, 0, 0):
                raise RuntimeError(
                    "Session migration checkpoint could not truncate the WAL. "
                    "Close other readers of the accounts database and retry."
                )
            await self._accounts_db.execute(
                "UPDATE storage_metadata SET value=? WHERE name='session_encryption' AND value=?",
                (_SESSION_ENCRYPTION_READY, _SESSION_ENCRYPTION_PENDING),
            )
            await self._accounts_db.commit()
        self._session_cipher = cipher

    def _decode_account(self, row) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        blob = result.get("session_blob_base64")
        if blob is not None:
            result["session_blob_base64"] = self._session_cipher.decrypt(blob, result["id"])
        return result

    async def _init_schema(self) -> None:
        """Create all tables if they don't exist."""
        for sql in ALL_TABLES:
            # accounts + auth_log go into accounts_db; proxies go into proxies_db
            target = self._proxies_db if "proxies" in sql.lower() and "accounts" not in sql.lower() else self._accounts_db
            await target.execute(sql)
        await self._accounts_db.commit()
        await self._proxies_db.commit()

    async def _migrate_proxy_reference(self) -> None:
        """Remove the invalid legacy FK without dropping account data."""
        async with self._accounts_db.execute("PRAGMA foreign_key_list(accounts)") as cursor:
            references = await cursor.fetchall()
        if not any(row[2] == "proxies" for row in references):
            return
        await self._accounts_db.execute("PRAGMA foreign_keys=OFF")
        try:
            await self._accounts_db.execute("BEGIN IMMEDIATE")
            await self._accounts_db.execute(CREATE_ACCOUNTS_TABLE.replace(
                "IF NOT EXISTS accounts", "accounts_new"))
            await self._accounts_db.execute("INSERT INTO accounts_new SELECT * FROM accounts")
            await self._accounts_db.execute("DROP TABLE accounts")
            await self._accounts_db.execute("ALTER TABLE accounts_new RENAME TO accounts")
            await self._accounts_db.commit()
        except BaseException:
            await self._accounts_db.rollback()
            raise
        finally:
            await self._accounts_db.execute("PRAGMA foreign_keys=ON")

    # ── Accounts ───────────────────────────────────────────────────────────────

    @serialized
    async def create_account(
        self,
        phone: str,
        fingerprint: dict,
        proxy_id: Optional[int] = None,
        session_blob_base64: Optional[str] = None,
        *,
        reject_existing: bool = False,
    ) -> int:
        """Create an account or return its existing ID unless strict creation was requested."""
        if session_blob_base64 is not None and not isinstance(session_blob_base64, str):
            raise ValueError("Session must be text")
        accounts = self._accounts_db
        try:
            await accounts.execute("BEGIN IMMEDIATE")
            async with accounts.execute("SELECT id FROM accounts WHERE phone=?", (phone,)) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                if reject_existing:
                    raise ValueError("Account already exists")
                account_id = existing[0]
            else:
                async with accounts.execute(
                    """INSERT INTO accounts (phone, fingerprint_json, proxy_id, status)
                       VALUES (?, ?, ?, 'pending')
                       RETURNING id""",
                    (phone, json.dumps(fingerprint), proxy_id),
                ) as cursor:
                    account_id = (await cursor.fetchone())[0]
                if session_blob_base64 is not None:
                    encrypted = self._session_cipher.encrypt(session_blob_base64, account_id)
                    await accounts.execute(
                        "UPDATE accounts SET session_blob_base64=? WHERE id=?",
                        (encrypted, account_id),
                    )
            await accounts.commit()
        except BaseException:
            await accounts.rollback()
            raise
        return account_id

    async def get_account(self, phone: str) -> Optional[dict[str, Any]]:
        async with self._accounts_db.execute(
            "SELECT * FROM accounts WHERE phone = ?", (phone,)
        ) as cur:
            row = await cur.fetchone()
            return self._decode_account(row)

    async def get_account_by_id(self, account_id: int) -> Optional[dict[str, Any]]:
        async with self._accounts_db.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ) as cur:
            row = await cur.fetchone()
            return self._decode_account(row)

    async def list_accounts(self) -> list[dict[str, Any]]:
        async with self._accounts_db.execute(
            "SELECT * FROM accounts ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
            return [self._decode_account(r) for r in rows]

    @serialized
    async def update_account(self, current_phone: str, /, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"phone", "tg_user_id", "username", "first_name", "last_name",
                   "session_blob_base64", "proxy_id", "status", "last_active", "notes"}
        if fields.keys() - allowed:
            raise ValueError("Unsupported or immutable account field")
        if fields.get("session_blob_base64") is not None:
            async with self._accounts_db.execute("SELECT id FROM accounts WHERE phone=?", (current_phone,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("Account does not exist")
            fields["session_blob_base64"] = self._session_cipher.encrypt(fields["session_blob_base64"], row[0])
        columns = ", ".join(f"{k} = ?" for k in fields)
        values  = list(fields.values()) + [current_phone]
        await self._accounts_db.execute(
            f"UPDATE accounts SET {columns} WHERE phone = ?", values
        )
        await self._accounts_db.commit()

    async def save_session_blob(self, phone: str, blob_b64: str) -> None:
        """Persist the packed TDLib session into the DB."""
        await self.update_account(
            phone,
            session_blob_base64=blob_b64,
            status="active",
            last_active=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        )

    @serialized
    async def delete_account(self, phone: str) -> None:
        await self._accounts_db.execute(
            "DELETE FROM accounts WHERE phone = ?", (phone,)
        )
        await self._accounts_db.commit()

    # ── Auth Log ───────────────────────────────────────────────────────────────

    @serialized
    async def log_auth_event(
        self, account_id: int, event: str, details: Optional[str] = None
    ) -> None:
        await self._accounts_db.execute(
            "INSERT INTO auth_log (account_id, event, details) VALUES (?, ?, ?)",
            (account_id, event, details),
        )
        await self._accounts_db.commit()

    # ── Proxies ────────────────────────────────────────────────────────────────

    @serialized
    async def upsert_proxy(
        self,
        host: str,
        port: int,
        protocol: str = "socks5",
        source: Optional[str] = None,
    ) -> int:
        async with self._proxies_db.execute(
            """INSERT INTO proxies (host, port, protocol, source)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(host, port, protocol) DO UPDATE SET source = excluded.source
               RETURNING id""",
            (host, port, protocol, source),
        ) as cur:
            row = await cur.fetchone()
            await self._proxies_db.commit()
            return row[0]

    @serialized
    async def update_proxy_status(
        self,
        proxy_id: int,
        status: str,
        latency_ms: Optional[int] = None,
    ) -> None:
        await self._proxies_db.execute(
            """UPDATE proxies
               SET status = ?,
                   latency_ms   = COALESCE(?, latency_ms),
                   last_checked = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
               WHERE id = ?""",
            (status, latency_ms, proxy_id),
        )
        await self._proxies_db.commit()

    async def get_alive_proxies(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._proxies_db.execute(
            "SELECT * FROM proxies WHERE status = 'alive' ORDER BY latency_ms LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_unchecked_proxies(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self._proxies_db.execute(
            "SELECT * FROM proxies WHERE status IN ('unchecked', 'dead') ORDER BY created_at LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def count_alive_proxies(self) -> int:
        async with self._proxies_db.execute(
            "SELECT COUNT(*) FROM proxies WHERE status = 'alive'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_proxy(self, proxy_id: int) -> Optional[dict[str, Any]]:
        async with self._proxies_db.execute(
            "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    @serialized
    async def get_best_available_proxy(self) -> Optional[dict[str, Any]]:
        """Return the fastest alive proxy not currently assigned to any account."""
        return await self._best_available_proxy()

    async def _best_available_proxy(self) -> Optional[dict[str, Any]]:
        async with self._accounts_db.execute(
            "SELECT proxy_id FROM accounts WHERE proxy_id IS NOT NULL AND status != 'revoked'"
        ) as cursor:
            assigned = {row[0] for row in await cursor.fetchall()}
        async with self._proxies_db.execute(
            "SELECT * FROM proxies WHERE status = 'alive' ORDER BY latency_ms"
        ) as cur:
            for row in await cur.fetchall():
                if row["id"] not in assigned:
                    return dict(row)
        return None

    @serialized
    async def reserve_proxy_for_account(self, phone: str) -> Optional[dict[str, Any]]:
        """Reserve a free proxy across server processes, preserving assignments on failure."""
        accounts = self._accounts_db
        try:
            await accounts.execute("BEGIN IMMEDIATE")
            async with accounts.execute("SELECT id FROM accounts WHERE phone=?", (phone,)) as cursor:
                if await cursor.fetchone() is None:
                    raise ValueError("Account does not exist")
            proxy = await self._best_available_proxy()
            if proxy is not None:
                await accounts.execute(
                    "UPDATE accounts SET proxy_id=? WHERE phone=?",
                    (proxy["id"], phone),
                )
            await accounts.commit()
        except BaseException:
            await accounts.rollback()
            raise
        return proxy


# ── Singleton ──────────────────────────────────────────────────────────────────

_db: Optional[Database] = None
_db_lock = asyncio.Lock()


async def get_db() -> Database:
    """Return the singleton Database instance (auto-connecting on first call)."""
    global _db
    async with _db_lock:
        if _db is None:
            db = Database()
            await db.connect()
            _db = db
    return _db
