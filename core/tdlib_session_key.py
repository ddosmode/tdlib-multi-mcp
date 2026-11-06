"""Read and seed the bundled TDLib's native authorization binlog.

Binary layout is pinned to TDLib d266c7c5fdd9d121ba951cf9885b6eb70d262063.
The relevant upstream implementations are BinlogEvent.cpp, BinlogEventsProcessor.cpp,
BinlogKeyValue.h, AuthDataShared.cpp, AuthKey.h and AuthManager.cpp. Read only
closed databases while holding the account lock. Never log or persist extracted
keys outside the private session directories and encrypted session store.
"""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import stat
import struct
import zlib
from pathlib import Path
from typing import BinaryIO

TDLIB_VERSION = '1.8.46'
TDLIB_COMMIT = 'd266c7c5fdd9d121ba951cf9885b6eb70d262063'
_SUPPORTED_DCS = frozenset({1, 2, 3, 4, 5})
_HEADER = struct.Struct('<IQiiQ')
_UINT32 = struct.Struct('<I')
_AUTH_HEADER = struct.Struct('<QI')
_MIN_EVENT_SIZE = _HEADER.size + _UINT32.size
_MAX_EVENT_SIZE = 1 << 24
_PMC = 0x4327
_REWRITE = 1
_ERASED = -2
_ENCRYPTED = -3
_AUTHORIZED = 1
_CREATED_AT = 4
_WANTED_PMC_KEYS = frozenset({b'auth', b'auth_is_bot', b'main_dc_id', *(f'auth{dc}'.encode() for dc in _SUPPORTED_DCS)})


def require_supported_tdlib(library_path: str | Path | None = None) -> None:
    """Gate direct database access against the actual native revision."""
    if library_path is None:
        from aiotdlib.tdjson import _get_bundled_tdjson_lib_path
        library_path = _get_bundled_tdjson_lib_path()
    lib = ctypes.CDLL(str(Path(library_path).resolve()))
    execute = lib.td_execute
    execute.argtypes = [ctypes.c_char_p]
    execute.restype = ctypes.c_char_p
    for name, expected in (('version', TDLIB_VERSION), ('commit_hash', TDLIB_COMMIT)):
        query = json.dumps({'@type': 'getOption', 'name': name}).encode()
        response = json.loads(execute(query) or b'{}')
        if response.get('@type') != 'optionValueString' or response.get('value') != expected:
            raise RuntimeError('Passwordless session conversion requires the pinned TDLib build')


def _tl_bytes(value: bytes) -> bytes:
    length = len(value)
    prefix = bytes([length]) if length < 254 else b'\xfe' + length.to_bytes(3, 'little')
    encoded = prefix + value
    return encoded + bytes(-len(encoded) % 4)


def _take_tl_bytes(payload: bytes, offset: int) -> tuple[bytes, int]:
    if offset >= len(payload):
        raise ValueError('Truncated TDLib TL string')
    marker = payload[offset]
    prefix_size = 1 if marker < 254 else (4 if marker == 254 else 8)
    start = offset + prefix_size
    if start > len(payload):
        raise ValueError('Truncated TDLib TL string length')
    length = marker if marker < 254 else int.from_bytes(payload[offset + 1:start], 'little')
    end = start + length
    aligned_end = end + (-(end - offset) % 4)
    if aligned_end > len(payload) or any(payload[end:aligned_end]):
        raise ValueError('Invalid TDLib TL string length or padding')
    return payload[start:end], aligned_end


def _binlog_decryptor(payload: bytes):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(payload) < _UINT32.size or _UINT32.unpack_from(payload)[0] != 0:
        raise ValueError('Invalid TDLib encrypted binlog header')
    salt, offset = _take_tl_bytes(payload, _UINT32.size)
    iv, offset = _take_tl_bytes(payload, offset)
    expected_hash, end = _take_tl_bytes(payload, offset)
    if len(salt) < 16 or len(iv) != 16 or len(expected_hash) != 32 or end != len(payload):
        raise ValueError('Invalid TDLib encrypted binlog metadata')
    # d266 TdDb::as_db_key maps our empty TDLib database key to the
    # upstream default raw key. A user-supplied native key is unsupported.
    key = hashlib.pbkdf2_hmac('sha256', b'cucumber', salt, 2, dklen=32)
    actual_hash = hmac.digest(key, b'cucumbers everywhere', 'sha256')
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise ValueError('Unsupported TDLib binlog encryption key')
    return Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()


def _read_pmc(stream: BinaryIO) -> dict[bytes, bytes]:
    # Keep only the relevant values in memory, but replay every event ID so
    # replacement and tombstone semantics match TDLib's log processor.
    events: dict[int, tuple[bytes, bytes] | None] = {}
    last_id = 0
    decryptor = None

    def read(size: int) -> bytes:
        data = stream.read(size)
        return data if decryptor is None else decryptor.update(data)

    while prefix := read(_UINT32.size):
        if len(prefix) != _UINT32.size:
            raise ValueError('Truncated TDLib binlog event size')
        size = _UINT32.unpack(prefix)[0]
        if not _MIN_EVENT_SIZE <= size <= _MAX_EVENT_SIZE or size % 4:
            raise ValueError('Invalid TDLib binlog event size')
        tail = read(size - _UINT32.size)
        if len(tail) != size - _UINT32.size:
            raise ValueError('Truncated TDLib binlog event')
        raw = prefix + tail
        if zlib.crc32(raw[:-_UINT32.size]) != _UINT32.unpack_from(raw, size - _UINT32.size)[0]:
            raise ValueError('TDLib binlog checksum mismatch')
        _, event_id, event_type, flags, _ = _HEADER.unpack_from(raw)
        if event_type == _ENCRYPTED:
            if decryptor is not None or last_id or event_id or flags:
                raise ValueError('Unexpected TDLib encrypted binlog header')
            decryptor = _binlog_decryptor(raw[_HEADER.size:-_UINT32.size])
            continue
        if flags & ~_REWRITE:
            raise ValueError('Unsupported TDLib binlog event flags')
        if flags & _REWRITE and event_id <= last_id:
            if event_id not in events:
                raise ValueError('Invalid TDLib binlog rewrite target')
            if event_type == _ERASED:
                if size != _MIN_EVENT_SIZE:
                    raise ValueError('Invalid TDLib binlog tombstone')
                del events[event_id]
                continue
        elif event_type < 0:
            continue
        elif event_id <= last_id:
            raise ValueError('Out-of-order TDLib binlog event ID')
        else:
            last_id = event_id

        record = None
        if event_type == _PMC:
            payload = raw[_HEADER.size:-_UINT32.size]
            key, offset = _take_tl_bytes(payload, 0)
            value, end = _take_tl_bytes(payload, offset)
            if not key or end != len(payload):
                raise ValueError('Invalid TDLib binlog key/value record')
            if key in _WANTED_PMC_KEYS:
                record = (key, value)
        events[event_id] = record

    result = {}
    for record in events.values():
        if record is not None:
            key, value = record
            if key in result:
                raise ValueError('Duplicate authorization field in TDLib binlog')
            result[key] = value
    return result


def _parse_authorization_key(value: bytes) -> bytes:
    if len(value) < _AUTH_HEADER.size:
        raise ValueError('Missing or truncated TDLib authorization key')
    key_id, flags = _AUTH_HEADER.unpack_from(value)
    if not flags & _AUTHORIZED or flags & ~(_AUTHORIZED | _CREATED_AT):
        raise ValueError('TDLib main DC key is not a permanent authorized key')
    key, end = _take_tl_bytes(value, _AUTH_HEADER.size)
    expected_end = end + (8 if flags & _CREATED_AT else 0)
    if len(key) != 256 or expected_end != len(value) or key_id != _key_id(key):
        raise ValueError('Invalid TDLib authorization key length or identifier')
    return key


def _key_id(key: bytes) -> int:
    return int.from_bytes(hashlib.sha1(key).digest()[12:], 'little')


def read_native_session_key(directory: Path) -> tuple[int, bytes]:
    """Extract the effective permanent main DC key from a closed database."""
    require_supported_tdlib()
    directory = Path(directory)
    if directory.is_symlink() or (directory / 'td.binlog.new').exists():
        raise ValueError('TDLib database directory is not a stable closed snapshot')
    fd = os.open(directory / 'td.binlog', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('TDLib binlog must be an owned regular file')
        fields = _read_pmc(stream)
    if fields.get(b'auth') != b'ok' or fields.get(b'auth_is_bot') == b'true':
        raise ValueError('TDLib binlog has no authorized user session')
    # NetQueryDispatcher.h defaults native builds to DC1 without a stored ID.
    raw_dc = fields.get(b'main_dc_id', b'1')
    if raw_dc not in {str(dc).encode() for dc in _SUPPORTED_DCS}:
        raise ValueError('Unsupported TDLib main data center')
    dc_id = int(raw_dc)
    return dc_id, _parse_authorization_key(fields.get(b'auth' + raw_dc, b''))


def _encode_pmc(event_id: int, key: bytes, value: bytes) -> bytes:
    payload = _tl_bytes(key) + _tl_bytes(value)
    raw = _HEADER.pack(_MIN_EVENT_SIZE + len(payload), event_id, _PMC, 0, 0) + payload
    return raw + _UINT32.pack(zlib.crc32(raw))


def write_native_session_key(directory: Path, dc_id: int, auth_key: bytes, user_id: int | None = None) -> None:
    """Seed a fresh database with an existing user authorization.

    Callers must authenticate the source and then verify the resulting native
    connection with a server-backed account read before persisting it.
    """
    require_supported_tdlib()
    if type(dc_id) is not int or dc_id not in _SUPPORTED_DCS:
        raise ValueError('Unsupported TDLib data center')
    if not isinstance(auth_key, bytes) or len(auth_key) != 256 or not any(auth_key):
        raise ValueError('Invalid MTProto authorization key')
    if user_id is not None and (type(user_id) is not int or not 0 < user_id < (1 << 40)):
        raise ValueError('Invalid Telegram user ID')
    dc_text = str(dc_id).encode()
    values = [
        (b'auth' + dc_text, _AUTH_HEADER.pack(_key_id(auth_key), _AUTHORIZED) + _tl_bytes(auth_key)),
        (b'main_dc_id', dc_text),
        (b'auth', b'ok'),
    ]
    if user_id is not None:
        values.append((b'my_id', str(user_id).encode()))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or any(directory.iterdir()):
        raise ValueError('TDLib authorization requires a fresh empty database directory')
    path = directory / 'td.binlog'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            for event_id, (key, value) in enumerate(values, 1):
                stream.write(_encode_pmc(event_id, key, value))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
