"""Noninteractive aiotdlib adapter with isolated per-account session ownership."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import config
from core.fingerprints import DeviceFingerprint
from core.session_manager import session_dir, unpack_session, pack_session, cleanup_session

try:
    from aiotdlib import Client, ClientSettings
    from pydantic import field_validator, model_validator
    from aiotdlib.client_settings import ClientOptions, ClientProxySettings
    from aiotdlib.api import API, Close, GetAuthorizationState
    from aiotdlib import tdjson as _tdjson
    from ctypes import CDLL, c_char_p, c_double, c_int
    import logging
    import weakref
    _DISPATCHERS = weakref.WeakKeyDictionary()
    _NATIVE_LOG_CALLBACK = _tdjson.LogMessageCallback(lambda verbosity, message: None)

    class _SafeTDJson(_tdjson.TDJson):
        """Correct aiotdlib 0.27.6's native log-callback ABI.

        TDLib expects (int max_verbosity, callback); upstream passes only
        a temporary callback. Bind the exact ABI without ever registering
        that invalid pointer. The callback stays alive for the process.
        """
        def __new__(cls, library_path):
            # td_receive is global across native client IDs. Every client on
            # this event loop must share the same subscription dispatcher.
            dispatchers = _DISPATCHERS.setdefault(asyncio.get_running_loop(),{})
            path = str(Path(library_path).resolve())
            if path not in dispatchers:
                dispatchers[path] = super().__new__(cls)
            return dispatchers[path]

        def __init__(self, library_path):
            if getattr(self,'_initialized',False):
                return
            self.logger = logging.getLogger(__name__)
            self.library_path = Path(library_path).resolve()
            if not self.library_path.is_file():
                raise FileNotFoundError('TDLib native library is unavailable')
            self._tdjson = CDLL(str(self.library_path))
            signatures = {
                '_td_create_client_id': ('td_create_client_id', c_int, []),
                '_td_receive': ('td_receive', c_char_p, [c_double]),
                '_td_send': ('td_send', None, [c_int,c_char_p]),
                '_td_execute': ('td_execute', c_char_p, [c_char_p]),
                '_td_set_log_message_callback': ('td_set_log_message_callback', None, [c_int,_tdjson.LogMessageCallback]),
            }
            for attribute, (symbol, result, arguments) in signatures.items():
                function = getattr(self._tdjson,symbol)
                function.restype = result
                function.argtypes = arguments
                setattr(self,attribute,function)
            self._td_set_log_message_callback(0,_NATIVE_LOG_CALLBACK)
            self._subscribed_clients = {}
            self._listen_task = None
            self._initialized = True

        def unsubscribe_updates(self, client_id):
            # Keep the process dispatcher alive when idle. Cancelling a
            # to_thread(td_receive) cannot cancel the underlying native call,
            # which could otherwise consume a new client's update and lose it.
            self._subscribed_clients.pop(client_id,None)

        async def _listen_updates(self):
            try:
                while True:
                    update = await asyncio.to_thread(self.receive,.1)
                    if update:
                        client = self._subscribed_clients.get(update.get('@client_id'))
                        if client is not None:
                            await client.enqueue_update(update)
            finally:
                self._listen_task = None

    _tdjson.TDJson = _SafeTDJson
    AIOTDLIB_AVAILABLE = True
except ImportError:
    AIOTDLIB_AVAILABLE = False
    Client = object


if AIOTDLIB_AVAILABLE:
    class ManagedSettings(ClientSettings):
        # The adapter drives authentication explicitly and owns the exact
        # per-account directory, including QR accounts without a phone yet.
        @model_validator(mode="before")
        @classmethod
        def check_phone_and_bot_token(cls, values):
            return values

        @field_validator("files_directory", mode="after")
        @classmethod
        def validator_files_directory(cls, value, info):
            return value


def build_client_settings(fp: DeviceFingerprint, directory: Path, proxy=None, phone=None):
    """Resolve the application identity from the stored localization target."""
    if not AIOTDLIB_AVAILABLE:
        raise RuntimeError('aiotdlib is not installed')
    api_id, api_hash = config.credentials_for_lang_pack(fp['lang_pack'])
    return ManagedSettings(
        api_id=api_id, api_hash=api_hash, phone_number=phone,
        database_encryption_key='', files_directory=directory,
        device_model=fp['device_model'], system_version=fp['system_version'],
        application_version=fp['app_version'],
        system_language_code=fp['system_language_code'],
        proxy_settings=ClientProxySettings(host=proxy['host'],port=proxy['port']) if proxy else None,
        tdlib_verbosity=config.TDLIB_VERBOSITY,
        options=ClientOptions(localization_target=fp['lang_pack'], language_pack_id=fp['lang_code']),
    )


class ManagedClient(Client):
    """Leave code/password entry to explicit MCP requests, never stdin."""
    async def authorize(self):
        await self.send(GetAuthorizationState())

    async def _on_authorization_state_update(self, state):
        # Setup is automatic; authentication itself is driven only by callers.
        if state.ID == API.Types.AUTHORIZATION_STATE_WAIT_TDLIB_PARAMETERS:
            await self._set_tdlib_parameters()
        elif state.ID == API.Types.AUTHORIZATION_STATE_READY:
            self._authorized_event.set()
        self.auth_state = state

    async def stop(self):
        """Wait for TDLib to close its database before taking a snapshot."""
        if not self._running:
            return
        try:
            await self.send(Close())
            async with asyncio.timeout(config.TDLIB_TIMEOUT):
                while not self.state_name.endswith('Closed'):
                    await asyncio.sleep(.05)
        finally:
            try:
                task = self._update_task
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                for task in self._handlers_tasks:
                    task.cancel()
                if self._handlers_tasks:
                    await asyncio.gather(*self._handlers_tasks, return_exceptions=True)
            finally:
                await self.tdjson_client.close()
                self._running = False

    @property
    def state_name(self):
        return type(getattr(self, 'auth_state', None)).__name__


async def wait_auth_state(client, names):
    """Await an explicit state or fail within the configured timeout."""
    async with asyncio.timeout(config.TDLIB_TIMEOUT):
        while True:
            state = await client.api.get_authorization_state()
            if type(state).__name__ in names:
                return state
            task = getattr(client, '_update_task', None)
            if task and task.done():
                task.result()
                raise RuntimeError('TDLib update loop stopped')
            await asyncio.sleep(.05)


def make_client(fp, directory, proxy=None, phone=None):
    return ManagedClient(build_client_settings(fp, directory, proxy, phone))


_ACCOUNT_LOCKS: dict[int, asyncio.Lock] = {}


class TDLibClient:
    def __init__(self, account_id: int, phone: str, fingerprint: DeviceFingerprint,
                 session_blob: str | None = None, proxy: dict[str, Any] | None = None):
        self.account_id, self.phone, self.fingerprint = account_id, phone, fingerprint
        self.session_blob, self.proxy = session_blob, proxy
        self._client = None
        self._sdir = session_dir(account_id)
        self._lock = _ACCOUNT_LOCKS.setdefault(account_id, asyncio.Lock())

    async def __aenter__(self):
        await self._lock.acquire()
        try:
            await self._start()
            return self
        except BaseException:
            try:
                if self._client:
                    await self._client.stop()
            finally:
                self._client = None
                # An existing private RAM directory may be the only updated
                # session after a failed DB save. Keep it for explicit recovery.
                self._lock.release()
            raise

    async def __aexit__(self, *_):
        try:
            await self._stop()
        finally:
            self._lock.release()

    async def _start(self):
        if self._sdir.exists():
            raise RuntimeError('A private RAM session requires recovery before reconnecting; do not overwrite it with the stored snapshot')
        if not self.session_blob:
            raise RuntimeError('Account has no authenticated TDLib session; authorize it first')
        unpack_session(self.session_blob, self._sdir)
        self._client = make_client(self.fingerprint, self._sdir, self.proxy, self.phone)
        await self._client.start()
        state = await wait_auth_state(self._client, {
            'AuthorizationStateReady', 'AuthorizationStateWaitPhoneNumber',
            'AuthorizationStateWaitCode', 'AuthorizationStateWaitPassword'})
        if type(state).__name__ != 'AuthorizationStateReady':
            raise RuntimeError('Stored session requires reauthorization')

    async def _stop(self):
        # Keep the private RAM directory available for recovery if closing,
        # packing or persistence fails. Never erase the only updated session.
        if self._client:
            await self._client.stop()
            self._client = None
        if self._sdir.exists():
            blob = pack_session(self._sdir)
            from db.database import get_db
            db = await get_db()
            await db.save_session_blob(self.phone,blob)
            self.session_blob = blob
            cleanup_session(self.account_id)

    @property
    def raw(self):
        if self._client is None:
            raise RuntimeError('TDLibClient is not started')
        return self._client

    async def get_me(self):
        return (await self.raw.api.get_me()).model_dump()

    async def send_message(self, chat_id, text):
        from aiotdlib.api import InputMessageText, FormattedText
        result = await self.raw.api.send_message(chat_id=chat_id,
            input_message_content=InputMessageText(text=FormattedText(text=text,entities=[]),clear_draft=False))
        return result.model_dump()

    async def send_paid_media(self, chat_id: int, star_count: int, media_paths: list[str],
                              media_type: str = "photo", caption: str = ""):
        """Send paid media into an eligible channel, without changing account state.

        TDLib user accounts support paid media in eligible channels only.
        Bot private-chat paid media requires a separate Bot API integration.
        """
        from aiotdlib.api import (InputMessagePaidMedia, InputPaidMedia,
                                  InputPaidMediaTypePhoto, InputPaidMediaTypeVideo,
                                  InputFileLocal, FormattedText)
        if not 1 <= star_count <= 25000:
            raise ValueError("star_count must be between 1 and 25000")
        if not 1 <= len(media_paths) <= 10:
            raise ValueError("Specify 1 to 10 media files")
        if media_type not in {"photo", "video"}:
            raise ValueError("media_type must be photo or video")
        if len(caption) > 1024:
            raise ValueError("caption exceeds 1024 characters")
        chat = await self.raw.api.get_chat(chat_id=chat_id)
        if type(chat.type).__name__ != "ChatTypeSupergroup" or not chat.type.is_channel:
            raise ValueError("Paid media with a user account requires a channel")
        full = await self.raw.api.get_supergroup_full_info(supergroup_id=chat.type.supergroup_id)
        if not full.has_paid_media_allowed:
            raise ValueError("Paid media is not enabled for this channel")
        paid_media = []
        for path in media_paths:
            file = Path(path).resolve(strict=True)
            if not file.is_file():
                raise ValueError("Media path must be a regular file")
            if media_type == "photo" and file.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("Paid photo exceeds TDLib 10 MB limit")
            kind = (InputPaidMediaTypePhoto() if media_type == "photo"
                    else InputPaidMediaTypeVideo(cover=None, start_timestamp=0, duration=0, supports_streaming=True))
            paid_media.append(InputPaidMedia(
                type=kind, media=InputFileLocal(path=str(file)), thumbnail=None,
                added_sticker_file_ids=[], width=0, height=0))
        result = await self.raw.api.send_message(
            chat_id=chat_id,
            input_message_content=InputMessagePaidMedia(
                star_count=star_count, paid_media=paid_media,
                caption=FormattedText(text=caption, entities=[]),
                show_caption_above_media=False, payload=""))
        return result.model_dump()

    async def get_dialogs(self, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        chats = await self.raw.api.get_chats(chat_list=None,limit=limit)
        return [{'id': cid} for cid in chats.chat_ids]

    async def get_chat_history(self, chat_id, limit=50):
        if not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        result = await self.raw.api.get_chat_history(chat_id=chat_id,from_message_id=0,offset=0,limit=limit,only_local=False)
        return [message.model_dump() for message in result.messages]
