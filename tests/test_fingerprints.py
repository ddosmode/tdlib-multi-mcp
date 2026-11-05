"""tests/test_fingerprints.py — Unit tests for core/fingerprints.py"""

import json
import pytest
from core.fingerprints import (
    generate_fingerprint,
    fingerprint_to_json,
    fingerprint_from_json,
    list_all_profiles,
)


def test_generate_is_deterministic():
    """Same phone always produces same fingerprint."""
    fp1 = generate_fingerprint("+79001234567")
    fp2 = generate_fingerprint("+79001234567")
    assert fp1 == fp2


def test_generate_different_phones_differ():
    """Different phones should (almost always) produce different fingerprints."""
    phones = [f"+7900{i:07d}" for i in range(20)]
    fps    = [generate_fingerprint(p) for p in phones]
    # At least half should be distinct
    unique = {json.dumps(fp, sort_keys=True) for fp in fps}
    assert len(unique) > 5, "Fingerprints are too uniform"


def test_device_type_android():
    fp = generate_fingerprint("+79001234567", device_type="android")
    assert "Android" in fp["system_version"]


def test_device_type_ios():
    fp = generate_fingerprint("+79001234568", device_type="ios")
    assert "iOS" in fp["system_version"] or "iPadOS" in fp["system_version"]


def test_device_type_desktop():
    fp = generate_fingerprint("+79001234569", device_type="desktop")
    assert fp["lang_pack"] in ("tdesktop", "macos")


def test_serialise_roundtrip():
    fp   = generate_fingerprint("+79001234570")
    raw  = fingerprint_to_json(fp)
    fp2  = fingerprint_from_json(raw)
    assert fp == fp2


def test_serialised_is_valid_json():
    fp  = generate_fingerprint("+79001234571")
    raw = fingerprint_to_json(fp)
    obj = json.loads(raw)
    assert "device_model" in obj
    assert "auth_key" not in obj   # Must NOT leak any auth material


def test_all_profiles_have_required_fields():
    for profile in list_all_profiles():
        assert len(profile) == 5, f"Profile tuple should have 5 elements: {profile}"
        model, sys_ver, app_ver, lang, pack = profile
        assert model and sys_ver and app_ver and lang and pack


def test_fingerprint_has_all_keys():
    fp = generate_fingerprint("+12125550100")
    for key in ("device_model", "system_version", "app_version",
                "system_language_code", "lang_pack", "lang_code"):
        assert key in fp, f"Missing key: {key}"
