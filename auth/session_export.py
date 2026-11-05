"""Native TDLib to StringSession conversion is unavailable.

TDLib's database is not a standalone MTProto auth-key file. Export endpoints
fail explicitly rather than fabricate a session that appears authenticated.
"""


async def export_telethon_session(account_id: int):
    raise NotImplementedError('Native TDLib session conversion is unsupported. Use the original client session or authorize using phone or QR.')


async def export_pyrogram_session(account_id: int):
    raise NotImplementedError('Native TDLib session conversion is unsupported. Use the original client session or authorize using phone or QR.')
