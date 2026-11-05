"""
config.py — Configuration loader for tdlib-multi-mcp.

Selects public application credentials by the stored fingerprint lang_pack.
Values are NEVER logged, printed or committed.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.resolve()

_TEST_ROOT = os.environ.get("TG_MCP_TEST_TMPDIR") if os.environ.get("TG_MCP_TESTING") == "1" else None
DATA_DIR = Path(_TEST_ROOT) / "data" if _TEST_ROOT else PROJECT_DIR / "data"
def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("Private application directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise RuntimeError("Private application directory has a different owner")
    path.chmod(0o700)


_private_directory(DATA_DIR)

ACCOUNTS_DB_PATH = DATA_DIR / "accounts.db"
PROXIES_DB_PATH  = DATA_DIR / "proxies.db"

RAM_DISK_BASE = Path(_TEST_ROOT) / "ram" if _TEST_ROOT else Path(f"/dev/shm/tg_mcp_{os.getuid()}")
_private_directory(RAM_DISK_BASE)

LOGS_DIR = Path.home() / "bin" / "logs" / "telegram-mcp"
_private_directory(LOGS_DIR)

# Public application identity profiles, not user-issued credentials.
# Provenance: https://github.com/thedemons/opentele/blob/main/src/api.py
# Never mix an application's API pair with another platform's lang_pack.
_OFFICIAL_APPLICATIONS = {
    "tdesktop": (2040, "b18441a1ff607e10a989891a5462e627"),
    "android": (6, "eb06d4abfb49dc3eeb1aeb98ae0f581e"),
    "ios": (10840, "33c45224029d59cb3ad0c16134215aeb"),
    "macos": (2834, "68875f756c9b437a8b916ca3de215815"),
}


def credentials_for_lang_pack(lang_pack: str) -> tuple[int, str]:
    """Return the inseparable public API identity for a supported platform.

    User TG_API_ID/TG_API_HASH overrides and config.env are intentionally unused.
    An unknown platform must fail before any network connection.
    """
    try:
        return _OFFICIAL_APPLICATIONS[lang_pack]
    except KeyError:
        raise ValueError("Unsupported application language pack") from None


# Compatibility for offline session encoders. Runtime selects by fingerprint.
TG_API_ID, TG_API_HASH = credentials_for_lang_pack("tdesktop")

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
