"""
db/database.py — Async SQLite CRUD layer for tdlib-multi-mcp.

Uses aiosqlite for non-blocking I/O.
All writes go through explicit transactions for thread-safety.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from db.models import ALL_TABLES
import config

log = logging.getLogger(__name__)


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

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open both databases and ensure schema is initialised."""
        self._accounts_db = await aiosqlite.connect(self._accounts_path)
        self._proxies_db  = await aiosqlite.connect(self._proxies_path)
        for db in (self._accounts_db, self._proxies_db):
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA foreign_keys=ON")
        await self._init_schema()
        log.debug("Database connected: %s, %s", self._accounts_path, self._proxies_path)

    async def close(self) -> None:
        for db in (self._accounts_db, self._proxies_db):
            if db:
                await db.close()

    async def _init_schema(self) -> None:
        """Create all tables if they don't exist."""
        for sql in ALL_TABLES:
            # accounts + auth_log go into accounts_db; proxies go into proxies_db
            target = self._proxies_db if "proxies" in sql.lower() and "accounts" not in sql.lower() else self._accounts_db
            await target.execute(sql)
        await self._accounts_db.commit()
        await self._proxies_db.commit()

    # ── Accounts ───────────────────────────────────────────────────────────────

    async def create_account(
        self,
        phone: str,
        fingerprint: dict,
        proxy_id: Optional[int] = None,
    ) -> int:
        """Insert a new account record. Returns the new row id."""
        async with self._accounts_db.execute(
            """INSERT INTO accounts (phone, fingerprint_json, proxy_id, status)
               VALUES (?, ?, ?, 'pending')
               ON CONFLICT(phone) DO UPDATE SET
                 fingerprint_json = excluded.fingerprint_json,
                 proxy_id         = excluded.proxy_id,
                 status           = 'pending'
            """,
            (phone, json.dumps(fingerprint), proxy_id),
        ) as cur:
            await self._accounts_db.commit()
            return cur.lastrowid  # type: ignore[return-value]

    async def get_account(self, phone: str) -> Optional[dict[str, Any]]:
        async with self._accounts_db.execute(
            "SELECT * FROM accounts WHERE phone = ?", (phone,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_account_by_id(self, account_id: int) -> Optional[dict[str, Any]]:
        async with self._accounts_db.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def list_accounts(self) -> list[dict[str, Any]]:
        async with self._accounts_db.execute(
            "SELECT * FROM accounts ORDER BY id"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def update_account(self, phone: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{k} = ?" for k in fields)
        values  = list(fields.values()) + [phone]
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
            last_active="strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
        )

    async def delete_account(self, phone: str) -> None:
        await self._accounts_db.execute(
            "DELETE FROM accounts WHERE phone = ?", (phone,)
        )
        await self._accounts_db.commit()

    # ── Auth Log ───────────────────────────────────────────────────────────────

    async def log_auth_event(
        self, account_id: int, event: str, details: Optional[str] = None
    ) -> None:
        await self._accounts_db.execute(
            "INSERT INTO auth_log (account_id, event, details) VALUES (?, ?, ?)",
            (account_id, event, details),
        )
        await self._accounts_db.commit()

    # ── Proxies ────────────────────────────────────────────────────────────────

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

    async def get_best_available_proxy(self) -> Optional[dict[str, Any]]:
        """Return the fastest alive proxy not currently assigned to any account."""
        async with self._proxies_db.execute(
            """SELECT p.* FROM proxies p
               LEFT JOIN (
                   SELECT proxy_id FROM accounts WHERE status = 'active'
               ) used ON used.proxy_id = p.id
               WHERE p.status = 'alive' AND used.proxy_id IS NULL
               ORDER BY p.latency_ms
               LIMIT 1"""
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── Singleton ──────────────────────────────────────────────────────────────────

_db: Optional[Database] = None


async def get_db() -> Database:
    """Return the singleton Database instance (auto-connecting on first call)."""
    global _db
    if _db is None:
        _db = Database()
        await _db.connect()
    return _db
