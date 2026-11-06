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
4. Telethon/Pyrogram conversion follows OpenTele UseCurrentSession. Keep the
   existing authorization key, original official app ID and lang_pack. It
   uses the pinned TDLib binlog codec and checks the session against Telegram.
   Never ask for 2FA, a code or QR during conversion. Close copies without
   logout because they share the original authorization.
5. Preserve private session state on persistence failures; never claim that a
   mocked test proves live Telegram authorization.
