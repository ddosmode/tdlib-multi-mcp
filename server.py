"""
server.py — MCP server entry point for tdlib-multi-mcp.

Registers 16 tools over stdio transport (JSON-RPC 2.0, version negotiated by the MCP SDK).

Usage (invoked by mcp-hub / agy / kilo):
    python ~/.agents/mcp/telegram-mcp/server.py

All stdout is reserved for the MCP protocol. Logs go to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any
from contextlib import asynccontextmanager

# Configure logging before anything else — MUST go to stderr only!
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[telegram-mcp] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Eager import of config (validates credentials on startup)
try:
    import config  # noqa: F401 — validates TG_API_ID/TG_API_HASH at import
except SystemExit:
    # config.py already printed a user-friendly message; propagate the exit.
    sys.exit(1)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
except ImportError:
    log.error(
        "mcp package not found. Run: pip install mcp  (inside the venv)"
    )
    sys.exit(1)

# ── Lazy imports from subsystems ───────────────────────────────────────────────
from auth.phone_auth     import start_phone_auth, submit_auth_code
from auth.qr_auth        import start_qr_auth, check_qr_auth
from auth.session_import import import_telethon_session, import_pyrogram_session
from auth.session_export import export_telethon_session, export_pyrogram_session
from core.fingerprints   import fingerprint_from_json
from db.database         import get_db
from proxy.collector     import collect_proxies
from proxy.rotator       import get_pool_status, rotate_proxy_for_account
from proxy.validator     import validate_all_unchecked

# ── MCP server instance ────────────────────────────────────────────────────────

server = Server(config.MCP_SERVER_NAME)


# ── Tool definitions ───────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    tools = [
        # ── Account management ──────────────────────────────────────────────
        types.Tool(
            name="tg_list_accounts",
            description=(
                "List all Telegram accounts in the manager. "
                "Returns phone, status, device fingerprint, assigned proxy and last active time."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="tg_add_account_phone",
            description=(
                "Start phone-number authorization. Sends a Telegram auth code to the given number. "
                "Follow up with tg_add_account_code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phone":       {"type": "string", "description": "Phone in international format, e.g. +79001234567"},
                    "device_type": {"type": "string", "enum": ["android", "ios", "desktop"],
                                   "description": "Optional device profile category. Chosen automatically if omitted."},
                },
                "required": ["phone"],
            },
        ),
        types.Tool(
            name="tg_add_account_code",
            description=(
                "Submit the Telegram auth code received by phone. "
                "Optionally provide 2FA password if the account has one enabled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "phone":        {"type": "string"},
                    "code":         {"type": "string", "description": "5-digit auth code from Telegram"},
                    "password_2fa": {"type": "string", "description": "2FA cloud password (if enabled)"},
                },
                "required": ["phone", "code"],
            },
        ),
        types.Tool(
            name="tg_add_account_qr",
            description=(
                "Generate a QR-code login link (tg://login?token=...). "
                "Scan it with another Telegram client. "
                "Then call tg_add_account_qr_check with the returned token to confirm."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "device_type": {"type": "string", "enum": ["android", "ios", "desktop"]},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="tg_add_account_qr_check",
            description="Poll QR authorization status. Returns pending | active | expired.",
            inputSchema={
                "type": "object",
                "properties": {
                    "token": {"type": "string", "description": "Token returned by tg_add_account_qr"},
                    "password_2fa": {"type": "string", "description": "Explicit password for an awaiting_password QR flow"},
                },
                "required": ["token"],
            },
        ),
        types.Tool(
            name="tg_import_telethon",
            description="Unavailable: native TDLib session transfer is not implemented.",
            inputSchema={
                "type": "object",
                "properties": {
                    "string_session": {"type": "string"},
                    "phone":          {"type": "string", "description": "Optional phone number label"},
                    "device_type":    {"type": "string", "enum": ["android", "ios", "desktop"]},
                },
                "required": ["string_session"],
            },
        ),
        types.Tool(
            name="tg_import_pyrogram",
            description="Unavailable: native TDLib session transfer is not implemented.",
            inputSchema={
                "type": "object",
                "properties": {
                    "string_session": {"type": "string"},
                    "phone":          {"type": "string"},
                    "device_type":    {"type": "string", "enum": ["android", "ios", "desktop"]},
                },
                "required": ["string_session"],
            },
        ),
        types.Tool(
            name="tg_export_telethon",
            description=(
                "Export a stored TDLib session as a Telethon StringSession. "
                "CAUTION: the returned string grants full account access."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                },
                "required": ["account_id"],
            },
        ),
        types.Tool(
            name="tg_export_pyrogram",
            description=(
                "Export a stored TDLib session as a Pyrogram v2 StringSession. "
                "CAUTION: the returned string grants full account access."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer"},
                },
                "required": ["account_id"],
            },
        ),
        types.Tool(
            name="tg_get_account_status",
            description="Check the status and proxy info for a specific account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                },
                "required": ["phone"],
            },
        ),
        types.Tool(
            name="tg_delete_account",
            description=(
                "Permanently remove an account from the manager. "
                "Clears the RAM session, auth log entries, and the DB record. "
                "Cannot be undone. Use account_id from tg_list_accounts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "confirm": {"const": True, "description": "True only after explicit user authorization to delete this exact account"},
                    "account_id": {
                        "type": "integer",
                        "description": "Numeric account ID as returned by tg_list_accounts",
                    },
                },
                "required": ["account_id", "confirm"],
            },
        ),
        # ── Messaging ───────────────────────────────────────────────────────
        types.Tool(
            name="tg_send_message",
            description="Send a text message from a managed account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone":   {"type": "string", "description": "Sender account phone"},
                    "chat_id": {"type": "integer", "description": "Telegram chat/user/channel ID"},
                    "text":    {"type": "string"},
                },
                "required": ["phone", "chat_id", "text"],
            },
        ),
        types.Tool(
            name="tg_get_dialogs",
            description="Return a list of recent dialogs (chats/channels/groups) for an account.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "limit": {"type": "integer", "default": 20, "maximum": 100},
                },
                "required": ["phone"],
            },
        ),
        types.Tool(
            name="tg_get_chat_history",
            description="Fetch recent messages from a chat or channel.",
            inputSchema={
                "type": "object",
                "properties": {
                    "phone":   {"type": "string"},
                    "chat_id": {"type": "integer"},
                    "limit":   {"type": "integer", "default": 50, "maximum": 100},
                },
                "required": ["phone", "chat_id"],
            },
        ),
        # ── Proxy pool ──────────────────────────────────────────────────────
        types.Tool(
            name="tg_proxy_pool_status",
            description="Return proxy pool statistics: alive/dead/unchecked counts and average latency.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="tg_proxy_refresh_pool",
            description=(
                "Collect fresh proxies from public sources and validate them. "
                "Returns updated pool statistics."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]

    for tool in tools:
        if tool.name in {"tg_import_telethon", "tg_import_pyrogram", "tg_export_telethon", "tg_export_pyrogram"}:
            tool.inputSchema["additionalProperties"] = False
            if tool.name.startswith("tg_import"):
                tool.description = "Import an existing authorization into compatible TDLib storage without a new login"
                tool.inputSchema["properties"]["source_lang_pack"] = {"type": "string", "enum": ["android", "ios", "macos", "tdesktop"], "description": "Original source application identity"}
                if tool.name == "tg_import_telethon":
                    tool.inputSchema["required"].append("source_lang_pack")
    return tools


# ── Tool dispatch ──────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """Route tool calls to the appropriate handler."""
    try:
        from jsonschema import validate, ValidationError
        definitions = {tool.name: tool for tool in await list_tools()}
        if name not in definitions:
            raise ValueError("Unknown tool")
        try:
            validate(arguments, definitions[name].inputSchema)
        except ValidationError:
            return [types.TextContent(type="text", text=json.dumps({"error": "Invalid tool arguments", "tool": name}))]
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except Exception as exc:
        # Exception messages can contain codes, session strings or passwords.
        log.error("Tool failed (%s)", type(exc).__name__)
        message = "Operation failed; check configuration and account authorization"
        if isinstance(exc, NotImplementedError):
            message = "Native TDLib session conversion is not implemented"
        error = {"error": message, "tool": name}
        return [types.TextContent(type="text", text=json.dumps(error, ensure_ascii=False))]


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    # ── Account management ─────────────────────────────────────────────────
    if name == "tg_list_accounts":
        db       = await get_db()
        accounts = await db.list_accounts()
        # Strip the raw session blob from output
        for acc in accounts:
            acc.pop("session_blob_base64", None)
            fp_json = acc.pop("fingerprint_json", "{}")
            try:
                acc["fingerprint"] = json.loads(fp_json)
            except Exception:
                acc["fingerprint"] = {}
        return {"accounts": accounts, "total": len(accounts)}

    if name == "tg_add_account_phone":
        return await start_phone_auth(
            phone=args["phone"],
            device_type=args.get("device_type"),
        )

    if name == "tg_add_account_code":
        return await submit_auth_code(
            phone=args["phone"],
            code=args["code"],
            password_2fa=args.get("password_2fa"),
        )

    if name == "tg_add_account_qr":
        return await start_qr_auth(device_type=args.get("device_type"))

    if name == "tg_add_account_qr_check":
        return await check_qr_auth(token=args["token"], password_2fa=args.get("password_2fa"))

    if name in {"tg_import_telethon", "tg_import_pyrogram"}:
        handler = import_telethon_session if name == "tg_import_telethon" else import_pyrogram_session
        return await handler(string_session=args["string_session"], phone=args.get("phone"),
            device_type=args.get("device_type"), source_lang_pack=args.get("source_lang_pack"))

    if name in {"tg_export_telethon", "tg_export_pyrogram"}:
        handler = export_telethon_session if name == "tg_export_telethon" else export_pyrogram_session
        return await handler(account_id=args["account_id"])

    if name == "tg_get_account_status":
        db  = await get_db()
        acc = await db.get_account(args["phone"])
        if not acc:
            return {"error": f"Account not found: {args['phone']}"}
        acc.pop("session_blob_base64", None)
        fp_json = acc.pop("fingerprint_json", "{}")
        try:
            acc["fingerprint"] = json.loads(fp_json)
        except Exception:
            acc["fingerprint"] = {}
        proxy_id = acc.get("proxy_id")
        acc["proxy"] = await db.get_proxy(proxy_id) if proxy_id else None
        return acc

    if name == "tg_delete_account":
        if args.get("confirm") is not True:
            raise ValueError("Deletion requires explicit confirmation for this account")
        from core.account_lock import acquire_account_lock, release_account_lock
        from core.session_manager import cleanup_session
        account_id = args["account_id"]
        db = await get_db()
        fd = await acquire_account_lock(account_id, timeout=0)
        try:
            acc = await db.get_account_by_id(account_id)
            if not acc:
                return {"error": f"Account {account_id} not found"}
            await db.delete_account(acc["phone"])
            cleanup_session(account_id)
            return {"deleted": True, "account_id": account_id}
        finally:
            release_account_lock(fd)

    # Serialize before reading the blob so concurrent operations restore fresh state.
    if name in {"tg_send_message", "tg_get_dialogs", "tg_get_chat_history"}:
        async with _account_client(args["phone"]) as client:
            if name == "tg_send_message":
                result = await client.send_message(chat_id=args["chat_id"], text=args["text"])
                return {"sent": True, "message": result}
            if name == "tg_get_dialogs":
                return {"dialogs": await client.get_dialogs(limit=args.get("limit", 20))}
            messages = await client.get_chat_history(chat_id=args["chat_id"], limit=args.get("limit", 50))
            return {"messages": messages, "count": len(messages)}

    # ── Proxy pool ──────────────────────────────────────────────────────────
    if name == "tg_proxy_pool_status":
        return await get_pool_status()

    if name == "tg_proxy_refresh_pool":
        log.info("tg_proxy_refresh_pool: collecting proxies...")
        collected = await collect_proxies()
        log.info("tg_proxy_refresh_pool: validating %d proxies...", collected)
        validation = await validate_all_unchecked(batch_size=200, concurrency=30)
        pool       = await get_pool_status()
        return {"collected": collected, "validation": validation, "pool": pool}

    raise ValueError(f"Unknown tool: {name}")


from core.account_operations import account_client as _account_client


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("Starting %s v%s", config.MCP_SERVER_NAME, config.MCP_SERVER_VERSION)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        from auth.phone_auth import close_phone_sessions
        from auth.qr_auth import close_qr_sessions
        from db import database
        try:
            await asyncio.gather(close_phone_sessions(), close_qr_sessions(), return_exceptions=True)
        finally:
            if database._db is not None:
                await database._db.close()
                database._db = None


if __name__ == "__main__":
    asyncio.run(main())
