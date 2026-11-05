"""
core/fingerprints.py — Device fingerprint generator for tdlib-multi-mcp.

Each Telegram account gets a unique, immutable hardware profile that is stored
in accounts.db and passed to TDLib on every connection so the server always
sees the same "device".

Profile fields match TDLib's TdlibParameters:
  device_model, system_version, app_version, system_language_code, lang_pack
"""

from __future__ import annotations

import json
import random
import hashlib
from typing import TypedDict


class DeviceFingerprint(TypedDict):
    device_model: str
    system_version: str
    app_version: str
    system_language_code: str
    lang_pack: str
    lang_code: str


# ── Profile database ───────────────────────────────────────────────────────────
# Each entry: (device_model, system_version, app_version, system_language_code, lang_pack)

_ANDROID_PROFILES = [
    ("Google Pixel 8 Pro",          "Android 14; arm64-v8a",   "10.3.2 (4753)",      "en",    "android"),
    ("Google Pixel 7",              "Android 14; arm64-v8a",   "10.2.9 (4702)",      "en",    "android"),
    ("Samsung Galaxy S24 Ultra",    "Android 14; arm64-v8a",   "10.3.1 (4740)",      "en",    "android"),
    ("Samsung Galaxy S23",          "Android 13; arm64-v8a",   "10.1.5 (4648)",      "en",    "android"),
    ("Xiaomi 13 Ultra",             "Android 13; arm64-v8a",   "10.0.9 (4598)",      "zh",    "android"),
    ("Xiaomi Redmi Note 12 Pro",    "Android 13; arm64-v8a",   "10.1.0 (4612)",      "ru",    "android"),
    ("OnePlus 12",                  "Android 14; arm64-v8a",   "10.3.0 (4731)",      "en",    "android"),
    ("OPPO Find X7 Ultra",          "Android 14; arm64-v8a",   "10.2.5 (4688)",      "zh",    "android"),
    ("Realme GT5 Pro",              "Android 14; arm64-v8a",   "10.2.8 (4699)",      "en",    "android"),
    ("Vivo X100 Pro",               "Android 14; arm64-v8a",   "10.3.0 (4731)",      "zh",    "android"),
]

_IOS_PROFILES = [
    ("iPhone 15 Pro Max",           "iOS 17.4.1",              "10.3.2",             "en",    "ios"),
    ("iPhone 15 Pro",               "iOS 17.3.1",              "10.3.0",             "en",    "ios"),
    ("iPhone 14 Pro Max",           "iOS 17.2",                "10.2.7",             "en",    "ios"),
    ("iPhone 14",                   "iOS 16.7.4",              "10.1.8",             "ru",    "ios"),
    ("iPad Pro 12.9-inch (6th gen)","iPadOS 17.4",             "10.3.1",             "en",    "ios"),
]

_DESKTOP_PROFILES = [
    ("Apple MacBook Pro 16,1",      "macOS 14.4.1",            "5.0.5 (5218)",       "en",    "macos"),
    ("Apple MacBook Air 15,2",      "macOS 14.3",              "5.0.4 (5212)",       "en",    "macos"),
    ("Windows PC",                  "Windows 11 Build 22631",  "5.0.5 (5218)",       "en",    "tdesktop"),
    ("Ubuntu 24.04 LTS",            "Ubuntu 24.04",            "5.0.5 (5218)",       "en",    "tdesktop"),
    ("Fedora Linux 40",             "Fedora Linux 40",         "5.0.3 (5200)",       "en",    "tdesktop"),
]

_ALL_PROFILES = _ANDROID_PROFILES + _IOS_PROFILES + _DESKTOP_PROFILES

_LANG_PACKS = {
    "android":  "android",
    "ios":      "ios",
    "macos":    "macos",
    "tdesktop": "tdesktop",
}


def generate_fingerprint(phone: str, device_type: str | None = None) -> DeviceFingerprint:
    """
    Generate a deterministic but unique device fingerprint for a given phone number.

    The fingerprint is derived from a hash of the phone number so that the same
    account always gets the same profile (idempotent).

    Args:
        phone:       Normalized phone number (e.g. "+79001234567").
        device_type: Optional category hint: "android", "ios", "desktop".
                     If None, chosen deterministically from the full pool.

    Returns:
        A DeviceFingerprint TypedDict ready to be JSON-serialised and stored in the DB.
    """
    if device_type not in (None, "android", "ios", "desktop"):
        raise ValueError("Unsupported device type")
    # Build a seeded random based on the phone number so it's reproducible.
    seed = int(hashlib.sha256(phone.encode()).hexdigest(), 16)
    rng  = random.Random(seed)

    if device_type == "android":
        pool = _ANDROID_PROFILES
    elif device_type == "ios":
        pool = _IOS_PROFILES
    elif device_type == "desktop":
        pool = _DESKTOP_PROFILES
    else:
        pool = _ALL_PROFILES

    model, sys_ver, app_ver, lang_code, lang_pack = rng.choice(pool)

    return DeviceFingerprint(
        device_model=model,
        system_version=sys_ver,
        app_version=app_ver,
        system_language_code=lang_code,
        lang_pack=_LANG_PACKS.get(lang_pack, "tdesktop"),
        lang_code=lang_code,
    )


def fingerprint_from_json(raw: str) -> DeviceFingerprint:
    """Deserialise a fingerprint stored as JSON in the DB."""
    return DeviceFingerprint(**json.loads(raw))


def fingerprint_to_json(fp: DeviceFingerprint) -> str:
    """Serialise a fingerprint for storage in the DB."""
    return json.dumps(dict(fp))


def list_all_profiles() -> list[tuple]:
    """Return the full profile database (for diagnostics)."""
    return list(_ALL_PROFILES)
