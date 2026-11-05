# auth — Authorization subsystem

All authorization modes for adding Telegram accounts to tdlib-multi-mcp.

## Supported flows

| File | Mode | MCP Tools |
|---|---|---|
| `phone_auth.py` | Phone number + auth code + 2FA | `tg_add_account_phone`, `tg_add_account_code` |
| `qr_auth.py` | QR-code deep link | `tg_add_account_qr`, `tg_add_account_qr_check` |
| `session_import.py` | Import Telethon/Pyrogram StringSession | `tg_import_telethon`, `tg_import_pyrogram` |
| `session_export.py` | Export TDLib → Telethon/Pyrogram | `tg_export_telethon`, `tg_export_pyrogram` |

## Important rules

1. **Never log session strings, auth keys, or passwords** — even at DEBUG level.
2. **Phone auth is stateful**: `start_phone_auth` creates the account record and
   sends the code; `submit_auth_code` must be called with the SAME phone.
3. **QR auth uses in-memory state** (`_QR_SESSIONS` dict in `qr_auth.py`).
   The token is valid for ~30 seconds. If the server restarts, the QR token is lost.
4. **Telethon/Pyrogram import** extracts only `dc_id` and `auth_key` bytes.
   The session will be re-verified on the first actual TDLib connection.
5. **Session export** requires `td_auth_key.bin` to be present in the session dir.
   Accounts authorised via native TDLib phone flow will need an extended export
   implementation that reads `td.binlog` directly (TODO).
