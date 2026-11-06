"""Local account setup and live verification. Secrets never go into chat or argv."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.umask(0o077)
logging.basicConfig(level=logging.WARNING)


def private_write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def local_password():
    """Read 2FA entered by the account owner in a local window."""
    if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        try:
            prompt = subprocess.run(
                ['zenity', '--password', '--title=Telegram 2FA'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=120, check=True,
            )
            return prompt.stdout.removesuffix(chr(10))
        except FileNotFoundError:
            pass
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise RuntimeError('Local 2FA entry cancelled or expired') from None
    if not sys.stdin.isatty():
        raise RuntimeError('Complete 2FA in a local interactive terminal')
    return getpass.getpass('Telegram 2FA password (local terminal only): ')


async def login_qr(args):
    import config
    import qrcode
    from auth.qr_auth import start_qr_auth, check_qr_auth
    result = await start_qr_auth(device_type=args.device_type)
    token = result['token']
    with tempfile.TemporaryDirectory(prefix='login-qr-', dir=config.RAM_DISK_BASE) as directory:
        image = Path(directory) / 'login.png'
        link = None
        while True:
            if result.get('qr_link') and link != result['qr_link']:
                link = result['qr_link']
                qrcode.make(link).save(image)
                image.chmod(0o600)
                print(f'QR code: {image}', flush=True)
                if args.open_qr:
                    subprocess.Popen(['xdg-open', str(image)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.sleep(2)
            result = await check_qr_auth(token)
            if result['status'] == 'awaiting_password':
                password = await asyncio.to_thread(local_password)
                result = await check_qr_auth(token, password_2fa=password)
                del password
            if result['status'] == 'active':
                print('Login saved. Run verify for restart and authorization checks.', flush=True)
                return
            if result['status'] == 'expired':
                raise RuntimeError('QR login expired; start a new login')


async def probe_qr(_args):
    if os.environ.get('TG_MCP_TESTING') != '1' or not os.environ.get('TG_MCP_TEST_TMPDIR'):
        raise ValueError('QR probe requires an isolated test store')
    from auth.qr_auth import start_qr_auth, cancel_qr_auth
    result = await start_qr_auth(device_type='desktop')
    try:
        if result['status'] != 'qr_pending' or not result['qr_link'].startswith('tg://login?token='):
            raise RuntimeError('Telegram did not return a QR link')
    finally:
        await cancel_qr_auth(result['token'])
    print('Live QR token issued and isolated flow closed.')


async def verify(args):
    import config
    from core.account_operations import account_client
    from db.database import get_db

    db = await get_db()
    phone = args.phone
    if phone is None:
        accounts = [row for row in await db.list_accounts() if row['status'] == 'active']
        if len(accounts) != 1:
            raise ValueError('Specify --phone when there is not exactly one active account')
        phone = accounts[0]['phone']

    user_id = None
    for _ in range(2):
        async with account_client(phone) as client:
            me = await client.get_me()
            if user_id is not None and user_id != me['id']:
                raise RuntimeError('Restored session belongs to a different account')
            user_id = me['id']
    print(json.dumps({'native_login': True, 'close_save_restore': True}), flush=True)
    if not args.transfer:
        return

    from auth.session_export import export_telethon_session, export_pyrogram_session
    from auth.session_import import _decode_pyrogram, _decode_telethon

    account = await db.get_account(phone)
    fp = json.loads(account['fingerprint_json'])
    if account.get('proxy_id'):
        raise ValueError('Live clone verification requires a single direct network route')
    with tempfile.TemporaryDirectory(prefix='transfer-verify-', dir=config.RAM_DISK_BASE) as directory:
        work = Path(directory)
        reference_key = None
        formats = (
            ('telethon', export_telethon_session, _decode_telethon),
            ('pyrogram', export_pyrogram_session, _decode_pyrogram),
        )
        for kind, export, decode in formats:
            result = await export(account['id'])
            if result['status'] != 'active' or result['transfer'] != 'use_current_session':
                raise RuntimeError('Export did not preserve the current authorization')
            encoded = result['string_session']
            dc = decode(encoded)
            key = dc['dc_id'], dc['auth_key']
            if reference_key is not None and key != reference_key:
                raise RuntimeError('The two formats do not contain the same native authorization')
            reference_key = key

            session_file = work / (kind + '.txt')
            private_write(session_file, encoded)
            env = dict(os.environ, TG_MCP_TESTING='1', TG_MCP_TEST_TMPDIR=str(work / ('isolated-' + kind)))
            process = await asyncio.create_subprocess_exec(
                sys.executable, __file__, '_verify-import',
                '--format', kind, '--session-file', str(session_file),
                '--source-lang-pack', fp['lang_pack'], '--user-id', str(user_id),
                env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), 180)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                raise
            if process.returncode:
                raise RuntimeError('Isolated native session import verification failed')
            print(json.dumps({kind + '_passwordless_export_import': True}), flush=True)

    async with account_client(phone) as original:
        sessions = await original.raw.api.get_active_sessions()
        current = [session for session in sessions.sessions if session.is_current]
        api_id, _ = config.credentials_for_lang_pack(fp['lang_pack'])
        if (await original.get_me())['id'] != user_id or len(current) != 1 or current[0].api_id != api_id:
            raise RuntimeError('Original authorization was not preserved')
    print(json.dumps({'original_authorization_preserved': True}), flush=True)


async def isolated_import(args):
    from auth.session_import import import_telethon_session, import_pyrogram_session
    from core.account_operations import account_client

    handler = import_telethon_session if args.format == 'telethon' else import_pyrogram_session
    result = await handler(Path(args.session_file).read_text(), source_lang_pack=args.source_lang_pack)
    if result['status'] != 'active':
        raise RuntimeError('Import did not complete')
    async with account_client(result['phone']) as client:
        me = await client.get_me()
        if me['id'] != args.user_id:
            raise RuntimeError('Imported native session identity mismatch')
    # This is the original authorization. The isolated process only closes it.


class IncompleteAuthorization(RuntimeError):
    pass


async def recover(args):
    from core.account_lock import acquire_account_lock, release_account_lock
    from core.fingerprints import fingerprint_from_json
    from core.session_manager import session_dir, pack_session, cleanup_session
    from core.tdlib_client import make_client, wait_auth_state
    from db.database import get_db
    from proxy.rotator import ensure_ready_proxy

    db = await get_db()
    account = (await db.get_account_by_id(args.account_id)
               if args.account_id is not None else await db.get_account(args.phone))
    if not account:
        raise ValueError('Account not found')
    lock = await acquire_account_lock(account['id'])
    try:
        directory = session_dir(account['id'])
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('No retained RAM session exists')
        account = await db.get_account_by_id(account['id'])
        fp = fingerprint_from_json(account['fingerprint_json'])
        proxy = await ensure_ready_proxy(account['phone']) if account.get('proxy_id') else None
        client = make_client(fp, directory, proxy)
        try:
            await client.start()
            state = await wait_auth_state(client, {
                'AuthorizationStateReady', 'AuthorizationStateWaitPassword',
                'AuthorizationStateWaitPhoneNumber', 'AuthorizationStateWaitOtherDeviceConfirmation',
            })
            if type(state).__name__ == 'AuthorizationStateWaitPassword':
                print('Telegram is waiting for local 2FA.', flush=True)
                password = await asyncio.to_thread(local_password)
                try:
                    await client.api.check_authentication_password(password=password)
                finally:
                    del password
                state = await wait_auth_state(client, {'AuthorizationStateReady'})
            if type(state).__name__ != 'AuthorizationStateReady':
                raise IncompleteAuthorization('Retained QR flow did not complete')
            me = await client.api.get_me()
        finally:
            await client.stop()
        phone = '+' + str(me.phone_number).lstrip('+')
        if account.get('tg_user_id') not in (None, me.id):
            raise RuntimeError('Retained session identity mismatch')
        existing = await db.get_account(phone)
        if existing and existing['id'] != account['id']:
            raise RuntimeError('Account already has a separate saved session')
        from auth.phone_auth import _username
        await db.update_account(account['phone'], phone=phone, session_blob_base64=pack_session(directory),
                                status='active', tg_user_id=me.id, first_name=me.first_name,
                                last_name=me.last_name or '', username=_username(me))
        cleanup_session(account['id'])
        print('Retained native authorization saved.', flush=True)
    finally:
        release_account_lock(lock)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    login = sub.add_parser('login-qr')
    login.add_argument('--open-qr', action='store_true')
    login.add_argument('--device-type', choices=['android', 'ios', 'desktop'], default='desktop')
    sub.add_parser('probe-qr')
    check = sub.add_parser('verify')
    check.add_argument('--phone')
    check.add_argument('--transfer', action='store_true')
    recovery = sub.add_parser('recover')
    target = recovery.add_mutually_exclusive_group(required=True)
    target.add_argument('--phone')
    target.add_argument('--account-id', type=int)
    internal = sub.add_parser('_verify-import', help=argparse.SUPPRESS)
    internal.add_argument('--format', choices=['telethon', 'pyrogram'], required=True)
    internal.add_argument('--session-file', required=True)
    internal.add_argument('--source-lang-pack', required=True)
    internal.add_argument('--user-id', type=int, required=True)
    args = parser.parse_args()
    try:
        await {'login-qr': login_qr, 'probe-qr': probe_qr, 'verify': verify, 'recover': recover, '_verify-import': isolated_import}[args.command](args)
    finally:
        from auth.phone_auth import close_phone_sessions
        from auth.qr_auth import close_qr_sessions
        from db import database
        await asyncio.gather(close_phone_sessions(), close_qr_sessions(), return_exceptions=True)
        if database._db is not None:
            await database._db.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (Exception, KeyboardInterrupt) as error:
        print(f'Local verification failed ({type(error).__name__}). No credentials were printed.', file=sys.stderr)
        raise SystemExit(1)
