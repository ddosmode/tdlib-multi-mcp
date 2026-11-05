"""
config.py — Configuration loader for tdlib-multi-mcp.

Loads api_id / api_hash from ~/.config/telegram-mcp/config.env (chmod 600).
Values are NEVER logged, printed or committed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.resolve()
CONFIG_ENV_PATH = Path.home() / ".config" / "telegram-mcp" / "config.env"

DATA_DIR = Path.home() / ".agents" / "mcp" / "telegram-mcp" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNTS_DB_PATH = DATA_DIR / "accounts.db"
PROXIES_DB_PATH  = DATA_DIR / "proxies.db"

RAM_DISK_BASE = Path("/dev/shm/tg_mcp")
RAM_DISK_BASE.mkdir(parents=True, exist_ok=True)

LOGS_DIR = Path.home() / "bin" / "logs" / "telegram-mcp"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── API Credentials ────────────────────────────────────────────────────────────

def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Ignores comments and blank lines."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _load_credentials() -> tuple[int, str]:
    """
    Load TG_API_ID and TG_API_HASH.

    Priority:
      1. Environment variables TG_API_ID / TG_API_HASH
      2. ~/.config/telegram-mcp/config.env

    Raises SystemExit if credentials are missing.
    """
    env = _load_env_file(CONFIG_ENV_PATH)

    raw_id   = os.environ.get("TG_API_ID")   or env.get("TG_API_ID")   or ""
    raw_hash = os.environ.get("TG_API_HASH")  or env.get("TG_API_HASH") or ""

    if not raw_id or not raw_hash:
        print(
            "[telegram-mcp] FATAL: TG_API_ID and TG_API_HASH are required.\n"
            f"  Put them in {CONFIG_ENV_PATH} (chmod 600):\n"
            "    TG_API_ID=12345678\n"
            "    TG_API_HASH=abcdef...",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        api_id = int(raw_id)
    except ValueError:
        print("[telegram-mcp] FATAL: TG_API_ID must be an integer.", file=sys.stderr)
        sys.exit(1)

    return api_id, raw_hash


# Loaded once at import time — do NOT re-export these values anywhere!
TG_API_ID: int
TG_API_HASH: str
TG_API_ID, TG_API_HASH = _load_credentials()

# ── TDLib settings ─────────────────────────────────────────────────────────────

TDLIB_VERBOSITY = 0          # 0 = no TDLib logs to stderr
TDLIB_TIMEOUT   = 30.0       # seconds for TDLib responses

# ── Proxy settings ─────────────────────────────────────────────────────────────

PROXY_CHECK_URL     = "https://api.ipify.org"
PROXY_CHECK_TIMEOUT = 8.0    # seconds
PROXY_MIN_POOL_SIZE = 20     # collect more proxies when pool drops below this

# ── Session RAM-disk ───────────────────────────────────────────────────────────

SESSION_COMPRESS_LEVEL = 9   # gzip compression level for session blobs

# ── MCP server ─────────────────────────────────────────────────────────────────

MCP_SERVER_NAME    = "telegram-mcp"
MCP_SERVER_VERSION = "0.1.0"
