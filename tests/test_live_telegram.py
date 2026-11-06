"""Opt-in integration against the local native library and real Telegram."""
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live_telegram,
    pytest.mark.skipif(os.getenv('TG_MCP_LIVE') != '1', reason='set TG_MCP_LIVE=1 locally'),
]
ROOT = Path(__file__).resolve().parents[1]


def manage(*args, env):
    result = subprocess.run([str(ROOT / 'scripts/manage'), *args], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=420)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_live_tdlib_requests_qr_from_telegram(tmp_path):
    env = dict(os.environ, TG_MCP_TESTING='1', TG_MCP_TEST_TMPDIR=str(tmp_path))
    assert 'Live QR token issued' in manage('probe-qr', env=env)


def test_live_encrypted_session_restart_and_passwordless_conversion():
    env = {k: v for k, v in os.environ.items() if k not in {'TG_MCP_TESTING', 'TG_MCP_TEST_TMPDIR'}}
    assert '"close_save_restore": true' in manage('verify', env=env)
    report = manage('verify', '--transfer', env=env)
    assert '"telethon_passwordless_export_import": true' in report
    assert '"pyrogram_passwordless_export_import": true' in report
    assert '"original_authorization_preserved": true' in report


@pytest.mark.skipif(os.getenv('TG_MCP_LIVE_QR') != '1', reason='requires a user to scan a new QR')
def test_live_interactive_qr_login_and_restart(tmp_path):
    env = dict(os.environ, TG_MCP_TESTING='1', TG_MCP_TEST_TMPDIR=str(tmp_path))
    print('Scan the QR in Telegram and finish any 2FA locally.', flush=True)
    assert 'Login saved.' in manage('login-qr', '--open-qr', env=env)
    assert '"close_save_restore": true' in manage('verify', env=env)
