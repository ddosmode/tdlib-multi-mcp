import pytest
import config
from core.fingerprints import generate_fingerprint

@pytest.mark.parametrize('platform', ['android', 'ios', 'macos', 'tdesktop'])
def test_credentials_are_selected_by_platform(platform, monkeypatch):
    expected = config.credentials_for_lang_pack(platform)
    monkeypatch.setenv('TG_API_ID', '999999')
    monkeypatch.setenv('TG_API_HASH', 'incorrect')
    assert config.credentials_for_lang_pack(platform) == expected
    assert expected[0] > 0
    assert len(expected[1]) == 32


def test_unknown_platform_rejected():
    with pytest.raises(ValueError):
        config.credentials_for_lang_pack('unknown')
    with pytest.raises(ValueError):
        generate_fingerprint('+10000000000', 'unknown')
