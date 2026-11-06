"""Shared application identity and native authorization helpers."""
from __future__ import annotations

import hashlib

import config
from core.fingerprints import list_all_profiles

SUPPORTED_PACKS = ('tdesktop','android','ios','macos')
TRANSFER_TIMEOUT = 60


def resolve_source_pack(source_lang_pack: str | None, api_id: int | None = None) -> str:
    if source_lang_pack is None:
        if api_id is None:
            raise ValueError('Telethon sessions require an explicit source_lang_pack; the format does not contain application identity')
        matches=[pack for pack in SUPPORTED_PACKS if config.credentials_for_lang_pack(pack)[0]==api_id]
        if len(matches)!=1:
            raise ValueError('Source application ID is not a supported official application profile')
        source_lang_pack=matches[0]
    expected_id,_=config.credentials_for_lang_pack(source_lang_pack)
    if api_id is not None and api_id!=expected_id:
        raise ValueError('Source application ID does not match source_lang_pack')
    return source_lang_pack


def fingerprint_for_pack(seed: str, lang_pack: str, device_type: str | None = None) -> dict:
    expected={'android':'android','ios':'ios','tdesktop':'desktop','macos':'desktop'}[lang_pack]
    if device_type is not None and device_type!=expected:
        raise ValueError('Requested device_type does not match source application identity')
    profiles=[profile for profile in list_all_profiles() if profile[4]==lang_pack]
    index=int(hashlib.sha256(seed.encode()).hexdigest(),16)%len(profiles)
    model,system,version,language,pack=profiles[index]
    return dict(device_model=model,system_version=system,app_version=version,
                system_language_code=language,lang_pack=pack,lang_code=language)


def telethon_client(session_string: str | None, fp: dict, proxy: dict | None = None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    api_id,api_hash=config.credentials_for_lang_pack(fp['lang_pack'])
    proxy_settings=None
    if proxy:
        proxy_settings=dict(proxy_type='socks5',addr=proxy['host'],port=proxy['port'],rdns=True)
    client=TelegramClient(StringSession(session_string or ''),api_id,api_hash,
        device_model=fp['device_model'],system_version=fp['system_version'],app_version=fp['app_version'],
        lang_code=fp['lang_code'],system_lang_code=fp['system_language_code'],proxy=proxy_settings,
        connection_retries=1,request_retries=1,timeout=10,auto_reconnect=False,flood_sleep_threshold=0)
    # Telethon 1.43.2 initializes lang_pack as empty. This profile registry only
    # supports official identities; set it before the first network handshake.
    client._init_request.lang_pack=fp['lang_pack']
    return client


def release_flow_lock(flow: dict) -> None:
    fd=flow.pop('process_lock',None)
    if fd is not None:
        from core.account_lock import release_account_lock
        release_account_lock(fd)


async def discard_native_flow(flow: dict) -> None:
    """Revoke a new abandoned authorization; retain RAM if revocation fails."""
    from core.session_manager import cleanup_session
    safe_to_remove=False
    try:
        state=await flow['client'].api.get_authorization_state()
        if type(state).__name__=='AuthorizationStateReady':
            await flow['client'].api.log_out()
        safe_to_remove=True
    finally:
        try:
            await flow['client'].stop()
        finally:
            if safe_to_remove:
                cleanup_session(flow['account_id'])
            release_flow_lock(flow)
