# tdlib-multi-mcp

**Multi-account Telegram MCP server** built on the official [TDLib](https://core.telegram.org/tdlib) library.

Provides a full [Model Context Protocol](https://modelcontextprotocol.io/) server
that lets AI agents (Antigravity CLI, Kilo, Codex/ChatGPT) manage multiple Telegram
accounts simultaneously, each with its own device fingerprint and isolated SOCKS5 proxy.

## Features

- 🔐 **Multiple auth modes**: phone+code+2FA, QR-code scan, Telethon/Pyrogram StringSession import
- 💾 **Sessions as Base64 strings in SQLite** — TDLib directory packed with tar+gzip, stored in `accounts.db`, unpacked to RAM-disk on use
- 🎭 **Device fingerprints** — each account gets a unique, immutable hardware profile (Android, iOS, Desktop)
- 🔄 **Proxy rotation** — automatic SOCKS5 pool collection, isolated pre-validation (no Telegram contact), auto-swap on failure
- 🔁 **Session conversion** — import/export between TDLib ↔ Telethon ↔ Pyrogram StringSession
- 📋 **15 MCP tools** — full CRUD over accounts, dialogs, messages, proxy pool
- 🧪 **Full test coverage** — pytest suite for all subsystems

## Quick Start

```bash
# 1. Configure API credentials (never logged or printed)
mkdir -p ~/.config/telegram-mcp
cat > ~/.config/telegram-mcp/config.env << 'EOF'
TG_API_ID=YOUR_API_ID
TG_API_HASH=YOUR_API_HASH
EOF
chmod 600 ~/.config/telegram-mcp/config.env

# 2. Create venv and install
cd ~/.agents/mcp/telegram-mcp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Register with mcp-hub (syncs to agy, kilo, codex automatically)
mcp-hub add telegram-mcp \
  ~/.agents/mcp/telegram-mcp/venv/bin/python \
  ~/.agents/mcp/telegram-mcp/server.py

# 4. Test
pytest tests/ -v
```

## MCP Tools

| Tool | Description |
|---|---|
| `tg_list_accounts` | List all accounts with status, fingerprint, proxy |
| `tg_add_account_phone` | Start phone+code auth flow |
| `tg_add_account_code` | Submit auth code (+2FA) |
| `tg_add_account_qr` | Get QR-code login link |
| `tg_add_account_qr_check` | Poll QR auth status |
| `tg_import_telethon` | Import from Telethon StringSession |
| `tg_import_pyrogram` | Import from Pyrogram StringSession |
| `tg_export_telethon` | Export to Telethon StringSession |
| `tg_export_pyrogram` | Export to Pyrogram StringSession |
| `tg_get_account_status` | Check session validity |
| `tg_send_message` | Send a message |
| `tg_get_dialogs` | List dialogs/chats |
| `tg_get_chat_history` | Read chat/channel history |
| `tg_proxy_pool_status` | Proxy pool stats |
| `tg_proxy_refresh_pool` | Collect and validate fresh proxies |

## Architecture

```
~/.agents/mcp/telegram-mcp/
├── server.py          # MCP stdio entry point (15 tools)
├── config.py          # Paths, timeouts, env loader
├── core/              # TDLib client, session manager, fingerprints
├── auth/              # phone_auth, qr_auth, session import/export
├── proxy/             # collector, validator (isolated), rotator
├── db/                # SQLite models + CRUD
└── tests/             # pytest suite
```

## Requirements

- Python 3.11+
- `aiotdlib` (wraps `libtdjson` — installed automatically via pip)
- Telegram `api_id` + `api_hash` from [my.telegram.org](https://my.telegram.org)

## Security

- API credentials loaded from `~/.config/telegram-mcp/config.env` (chmod 600)
- Credentials are **never** printed, logged or committed
- Sessions stored encrypted in SQLite, unpacked only to `/dev/shm/` (RAM-disk, no traces on disk)
- Proxy validation strictly isolated — no Telegram packets through unverified proxies

## License

MIT
