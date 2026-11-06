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
The profile registry cites its published source. Transfer checks the application ID of the current server authorization against that profile.

## Current capabilities

- Phone/code/2FA and QR authorization state machines with live, expiring clients.
- Fixed per-account device profile and platform-matched application identity.
- Serialized account operations across processes, encrypted SQLite session storage and private RAM work directories.
- Dialogs, history and sending through an authenticated account.
- Public SOCKS5 collection, isolated HTTP validation and replacement of failed assigned routes.
- Stdio MCP schemas with validation and sanitized errors.
- Incremental project RAG with deleted-file reconciliation, a 60-second user timer,
  post-commit refresh and project-scoped MCP search/sync tools.

The import/export tools preserve the current authorization using the same
key and application identity, following OpenTele's `UseCurrentSession` semantics.
Conversion requires neither QR nor 2FA. Telethon imports require the original
`source_lang_pack` because the serialized format omits the application ID.
Pyrogram v2 imports resolve the original profile from that ID. Imports verify the
existing authorization before persisting the native copy.

Native storage conversion is pinned to TDLib 1.8.46 at commit
`d266c7c5fdd9d121ba951cf9885b6eb70d262063`. It checks the binlog format and
rejects unsupported builds or custom native encryption keys. SQLite archives remain
encrypted at rest. Treat exported strings as credentials. Close a client before
connecting another copy of the same authorization. Logging out from any copy
revokes the shared authorization.

QR login, native session restoration and passwordless Telethon/Pyrogram round trips
were verified with an authorized account on 2026-11-06. See [TODO.md](TODO.md) for the remaining live release checks.

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
| `tg_import_telethon`, `tg_import_pyrogram`, `tg_export_telethon`, `tg_export_pyrogram` | Conversion of the current authorization between native TDLib and client session formats |
| `project_knowledge_query`, `project_knowledge_sync` | Separate project RAG MCP server |

## Launching agents

From your home directory, plain `agy` and `kilo` enter this checkout and run
the RAG launcher first. Use `agy --home` or `kilo --home` for unrelated work.
Existing conversations can use the MCP names `telegram-mcp` and `tdlib-project-rag`.
The canonical GitHub repository is `ddosmode/tdlib-multi-mcp`.

For a model served by local Ollama on port 11434:

```bash
scripts/ollama-agent --require-tool 'Сколько активных аккаунтов?'
```

Read-only tools are enabled by default. Use `--allow-tool NAME` for each
additional action authorized for that run. To sign in interactively use
`scripts/manage login-qr --open-qr`; enter any 2FA directly on your computer.

## Verification

```bash
venv/bin/python -m pytest -q
TG_MCP_LIVE=1 venv/bin/python -m pytest -m live_telegram -q
venv/bin/python -m pip check
scripts/check-runtime
scripts/manage login-qr --open-qr
scripts/manage verify --transfer
```

The test suite covers SQLite migration and encryption, archive safety, RAG replacement
transactions, identity selection, authorization and proxy routing. The local verification
command reconnects and checks the saved account. With `--transfer` it exercises both
formats against Telegram and closes each isolated copy before opening the next. The
local 2FA prompt is used only for account login or recovery of an interrupted login. Use `scripts/manage recover --account-id ID` to
retry saving a retained native session after a persistence failure.

## Storage and security

Session archives are encrypted with AES-GCM in SQLite. Back up the private
`data/accounts.db.session.key` together with the database. The key is required for
restoration. The server stops startup if a legacy migration cannot clear the SQLite WAL.
Database and key files use mode `0600`. The native working directories use private
paths under `/dev/shm/tg_mcp_<uid>/`. API hashes, codes, passwords and session blobs must never appear
in logs, RAG, ordinary account-list output or Git. Library exceptions are sanitized at the MCP boundary.
RAG indexes tracked text only, excludes data and symlinks, and redacts common serialized secrets.
Never commit secrets; the sanitizer is an additional precaution, not a guarantee for arbitrary text.

## License

MIT
