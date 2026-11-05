# proxy — Proxy collection, validation and rotation

## Critical security rule

> [!CAUTION]
> Proxies are ALWAYS validated in isolation before being assigned to a Telegram account.
> The validator (validator.py) connects ONLY to `api.ipify.org` — never to Telegram.
> This guarantees that a malicious or intercepting proxy CANNOT see MTProto session keys.

## Files

| File | Purpose |
|---|---|
| `collector.py` | Fetches SOCKS5/HTTP proxy lists from 5 public GitHub/API sources |
| `validator.py` | Checks each proxy by connecting to `api.ipify.org` via SOCKS5. No Telegram contact. |
| `rotator.py` | Assigns proxies to accounts; auto-replaces dead proxies; triggers background refresh |

## Proxy lifecycle

```
collect_proxies()          → DB status: 'unchecked'
validate_all_unchecked()   → DB status: 'alive' | 'dead'
assign_proxy_to_account()  → accounts.proxy_id = proxy.id
rotate_proxy_for_account() → old proxy → 'dead', new proxy assigned
```

## Background refresh

When `rotate_proxy_for_account()` finds no replacement, it automatically spawns
`_background_refresh()` as an asyncio task. This calls `ensure_pool_has_proxies()`
which re-runs collection and validation without blocking the MCP response.
