# tests — pytest suite for tdlib-multi-mcp

## Running all tests

```bash
cd ~/.agents/mcp/telegram-mcp
source venv/bin/activate
pytest tests/ -v
```

## Individual test files

| File | What it tests | Network? |
|---|---|---|
| `test_fingerprints.py` | Determinism, serialisation, profile validity | No |
| `test_session_base64.py` | pack/unpack roundtrip, security (no absolute paths) | No |
| `test_proxy_isolated.py` | Parser, collector (mocked), validator (mocked + dead host) | Only to 192.0.2.1 (RFC test, guaranteed unreachable) |
| `test_auth_modes.py` | Telethon/Pyrogram decode/encode, key file read/write | No |
| `test_mcp_tools.py` | MCP dispatch layer, blob stripping, error handling | No |

## Coverage report

```bash
pytest tests/ --cov=. --cov-report=term-missing
```

## Notes

- Tests that require actual TDLib are integration tests and must be run manually
  with real `api_id`/`api_hash` and an active Telegram account.
- Never commit `.env` or `config.env` files.
- `test_proxy_isolated.py` uses RFC5737 addresses (192.0.2.x) which are guaranteed
  unreachable — this tests the "dead" code path without touching real proxies.
