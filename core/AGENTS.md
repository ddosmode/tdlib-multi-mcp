# core — TDLib client, session manager, fingerprints

This subdirectory contains the low-level engine of tdlib-multi-mcp:

## Files

| File | Purpose |
|---|---|
| `fingerprints.py` | Deterministic device profile generator — 20+ Android/iOS/Desktop profiles |
| `session_manager.py` | Packs TDLib directory → gzip+tar → Base64 string; unpacks to RAM-disk |
| `tdlib_client.py` | Async context-manager wrapper around `aiotdlib.Client` |

## Key Invariants

1. **One Client per account at a time** — do not start two TDLibClient instances for the same account simultaneously.
2. **Always use `async with TDLibClient(...):`** — this ensures the session is properly saved back to the DB on exit, even on exceptions.
3. **Never access `TG_API_ID`/`TG_API_HASH` outside `config.py`** — these values must not be passed to logs or error messages.
4. **RAM-disk cleanup** — `session_manager.cleanup_session(account_id)` is called automatically in `TDLibClient._stop()`. Do not leave stale directories in `/dev/shm/tg_mcp/`.

## Fingerprint assignment

Call `generate_fingerprint(phone, device_type=None)` once when creating a new account.
Store the result via `fingerprint_to_json(fp)` in `accounts.fingerprint_json`.
On subsequent connections, restore with `fingerprint_from_json(row["fingerprint_json"])`.
