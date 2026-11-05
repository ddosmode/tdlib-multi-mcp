# tdlib-multi-mcp

Multi-account Telegram MCP server using TDLib, with project knowledge indexed in the local Ubuntu RAG.

## Runtime

The supported and locally tested interpreter is **CPython 3.14.4**.
The installed native TDLib reports **1.8.46**. Dependencies are pinned in `requirements.txt`.

```bash
python3 --version  # must be 3.14.4
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
scripts/install-native-runtime  # tested Ubuntu x86_64, user directory, no sudo
scripts/install-agent-rules
scripts/install-cli-guards
scripts/rag-install.sh
scripts/run-server
```

No user-issued API ID/API Hash file is needed. `config.credentials_for_lang_pack()` selects
an inseparable public application identity matching the account's stored `lang_pack`.
`TG_API_ID`, `TG_API_HASH` and `config.env` do not override that mapping.
The profile registry cites its published source. Application identity and wire-level behavior
still need verification in the live integration tests.

## Current capabilities

- Phone/code/2FA and QR authorization state machines with live, expiring clients.
- Fixed per-account device profile and platform-matched application identity.
- Serialized account operations, SQLite session persistence and private RAM work directories.
- Dialogs, history and sending through an authenticated account.
- Public SOCKS5 collection and isolated HTTP validation before assignment.
- Stdio MCP schemas with validation and sanitized errors.
- Incremental project RAG with deleted-file reconciliation, a 60-second user timer,
  post-commit refresh and project-scoped MCP search/sync tools.

**Release status: integration candidate, not production verified.** Native loading and offline
tests do not establish a successful Telegram login, messaging or proxy failover.
The four Telethon/Pyrogram import/export tools return an explicit unsupported error.
The old implementation wrote a fictitious `td_auth_key.bin`; this does not initialize a TDLib
session and has been removed. A real conversion needs an authorized session-transfer design
and live integration tests. QR accounts with 2FA support explicit password submission in `tg_add_account_qr_check`. Email authorization and registration are not implemented.

## Agent workflow and RAG

Read `AGENTS.md`. Before planning, run:

```bash
scripts/rag sync
scripts/rag query 'the subsystem and change being considered'
```

After edits, stage new source files and run `scripts/rag sync` again. The timer independently
refreshes tracked working-tree changes while the user service manager is running.
Kilo, Antigravity, Claude and AGENTS-compatible agents have project rule files. Any other agent
must read `AGENTS.md`; instructions cannot force an arbitrary third-party client to obey.
See [docs/rag.md](docs/rag.md) for indexing and recovery details.

```bash
mcp-hub add telegram-mcp "$PWD/scripts/run-server"
mcp-hub add tdlib-project-rag "$PWD/venv/bin/python" "$PWD/scripts/rag_mcp.py"
```

## MCP tools

| Tools | Behavior |
|---|---|
| `tg_list_accounts`, `tg_get_account_status` | Account metadata, excluding session blobs |
| `tg_add_account_phone`, `tg_add_account_code` | Explicit phone/code/2FA flow |
| `tg_add_account_qr`, `tg_add_account_qr_check` | QR link and opaque polling handle |
| `tg_send_message`, `tg_get_dialogs`, `tg_get_chat_history` | Authenticated account operations |
| `tg_proxy_pool_status`, `tg_proxy_refresh_pool` | Proxy collection/validation |
| `tg_import_telethon`, `tg_import_pyrogram`, `tg_export_telethon`, `tg_export_pyrogram` | Unsupported native conversion, explicit error |
| `project_knowledge_query`, `project_knowledge_sync` | Separate project RAG MCP server |

## Verification

```bash
venv/bin/python -m pytest -q
venv/bin/python -m pip check
scripts/check-runtime
```

The test suite covers actual SQLite operations and migration, archive safety, RAG replacement
transactions, identity selection and mocked MCP/auth boundaries. No test sends a user message.
A production release also requires live phone/QR/2FA login, restore after restart, proxy failure,
message delivery, recovery and session-transfer tests with an authorized test account.

## Storage and security

Session archives are gzip/tar encoded as Base64 in SQLite. **Base64 is not encryption**.
Database files are mode `0600`; session directories/files are private and under
`/dev/shm/tg_mcp_<uid>/`. Memory may be swapped by the operating system.
Back up the database securely. API hashes, codes, passwords and session blobs must never appear
in logs, RAG, ordinary account-list output or Git. Library exceptions are sanitized at the MCP boundary.
RAG indexes tracked text only, excludes data and symlinks, and redacts common serialized secrets.
Never commit secrets; the sanitizer is an additional precaution, not a guarantee for arbitrary text.

## License

MIT
