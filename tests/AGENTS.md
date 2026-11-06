# Verification

Run the local suite with `venv/bin/python -m pytest -q` on Python 3.14.4.
It includes offline native-library tests. For real Telegram integration run:

```bash
TG_MCP_LIVE=1 venv/bin/python -m pytest -m live_telegram -q
```

The live suite requests a real QR token in an isolated store and closes that flow.
It also opens the user's existing encrypted session in separate processes, converts
the current key to both formats and back, and checks the original with Telegram.
QR scanning and any account login 2FA remain local user actions. Use
`scripts/manage login-qr --open-qr` for a new account. Never treat skipped live
tests or a generated QR as a completed login. Don't start a second flow, delete a
pending account or stop an existing process merely because verification fails.

For a fresh interactive login and a process restart in an isolated store, run:

```bash
TG_MCP_LIVE=1 TG_MCP_LIVE_QR=1 venv/bin/python -m pytest -m live_telegram -k interactive -s
```

This creates a new Telegram device session with the user's QR approval. Use a
separate test account when appropriate. The normal live suite reuses the saved
account and does not need another QR scan or any password.
