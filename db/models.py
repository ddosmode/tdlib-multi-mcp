"""
db/models.py — SQLite schema initialisation for tdlib-multi-mcp.

Tables:
  accounts  — Telegram accounts with session blobs and device fingerprints
  proxies   — SOCKS5 proxy pool with health status
  auth_log  — Audit log of auth events
"""

from __future__ import annotations

CREATE_ACCOUNTS_TABLE = """
CREATE TABLE IF NOT EXISTS accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    phone               TEXT    UNIQUE,
    tg_user_id          INTEGER UNIQUE,
    username            TEXT,
    first_name          TEXT,
    last_name           TEXT,
    fingerprint_json    TEXT    NOT NULL,
    session_blob_base64 TEXT,
    -- Proxies live in a separate SQLite database; cross-database FKs are invalid.
    proxy_id            INTEGER,
    status              TEXT    NOT NULL DEFAULT 'pending',
    -- pending | active | awaiting_code | awaiting_password | qr_pending | banned | revoked
    created_at          DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_active         DATETIME,
    notes               TEXT
);
"""

CREATE_PROXIES_TABLE = """
CREATE TABLE IF NOT EXISTS proxies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    host         TEXT    NOT NULL,
    port         INTEGER NOT NULL,
    protocol     TEXT    NOT NULL DEFAULT 'socks5',
    -- socks5 | http
    username     TEXT,
    password     TEXT,
    latency_ms   INTEGER,
    status       TEXT    NOT NULL DEFAULT 'unchecked',
    -- unchecked | alive | dead | checking
    source       TEXT,
    last_checked DATETIME,
    created_at   DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(host, port, protocol)
);
"""

CREATE_AUTH_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS auth_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
    event      TEXT    NOT NULL,
    details    TEXT,
    created_at DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

ALL_TABLES = [
    CREATE_PROXIES_TABLE,   # proxies first (accounts FK → proxies)
    CREATE_ACCOUNTS_TABLE,
    CREATE_AUTH_LOG_TABLE,
]
