"""Offline StringSession parsers. Native TDLib conversion is unsupported.

Parsing another client's session format does not create a usable TDLib database.
Public import endpoints fail before reading or storing any session material.
"""
from __future__ import annotations
import base64
import ipaddress
import struct
from typing import Any


def _decode_telethon(string_session: str) -> dict[str,Any]:
    if not string_session.startswith('1'):
        raise ValueError('Unsupported Telethon session version')
    raw=base64.urlsafe_b64decode(string_session[1:]+'='*(-len(string_session[1:])%4))
    if len(raw) not in (263,275):
        raise ValueError('Telethon session too short or invalid length')
    ip_length=len(raw)-259
    return dict(dc_id=raw[0],ip=str(ipaddress.ip_address(raw[1:1+ip_length])),
                port=struct.unpack('>H',raw[1+ip_length:3+ip_length])[0],auth_key=raw[-256:])


def _decode_pyrogram(string_session: str) -> dict[str,Any]:
    # Pyrogram v2 storage.SESSION_STRING_FORMAT is >BI?256sQ?.
    raw=base64.urlsafe_b64decode(string_session+'='*(-len(string_session)%4))
    if len(raw)!=struct.calcsize('>BI?256sQ?'):
        raise ValueError('Pyrogram session too short or invalid length')
    dc_id,api_id,test_mode,auth_key,user_id,is_bot=struct.unpack('>BI?256sQ?',raw)
    return dict(dc_id=dc_id,api_id=api_id,test_mode=test_mode,auth_key=auth_key,user_id=user_id,is_bot=is_bot)


async def import_telethon_session(string_session: str, phone: str | None = None,
                                  device_type: str | None = None):
    raise NotImplementedError('Native TDLib session conversion is unsupported. Authorize using phone or QR; no synthetic key file is accepted.')


async def import_pyrogram_session(string_session: str, phone: str | None = None,
                                  device_type: str | None = None):
    raise NotImplementedError('Native TDLib session conversion is unsupported. Authorize using phone or QR; no synthetic key file is accepted.')
