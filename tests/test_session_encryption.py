import asyncio
import os
import sqlite3
import stat

import pytest

from db.database import Database
from db.models import CREATE_ACCOUNTS_TABLE
from core.session_manager import pack_session, unpack_session


def stored(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT session_blob_base64 FROM accounts ORDER BY id").fetchall()


@pytest.mark.asyncio
async def test_sessions_encrypted_on_disk_and_decrypted_for_runtime(tmp_path):
    path = tmp_path / "accounts.db"
    db = Database(path, tmp_path / "proxies.db")
    await db.connect()
    try:
        aid = await db.create_account("+100", {})
        await db.update_account("+100", session_blob_base64="highly-private-session")
        assert stored(path)[0][0].startswith("tg-session:v1:")
        assert (await db.get_account("+100"))["session_blob_base64"] == "highly-private-session"
        assert (await db.get_account_by_id(aid))["session_blob_base64"] == "highly-private-session"
        await db.save_session_blob("+100", "updated-private-session")
        assert (await db.list_accounts())[0]["session_blob_base64"] == "updated-private-session"
        await db.create_account("+200", {}, session_blob_base64="initial-private-session")
        assert stored(path)[1][0].startswith("tg-session:v1:")
        assert (await db.get_account("+200"))["session_blob_base64"] == "initial-private-session"
    finally:
        await db.close()
    assert b"private-session" not in path.read_bytes()
    key = path.with_name(path.name + ".session.key")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_legacy_plaintext_session_migrated(tmp_path):
    path = tmp_path / "accounts.db"
    with sqlite3.connect(path) as db:
        db.execute(CREATE_ACCOUNTS_TABLE)
        db.execute("INSERT INTO accounts(phone,fingerprint_json,session_blob_base64) VALUES('+100','{}','legacy-private-session')")
    db = Database(path, tmp_path / "proxies.db")
    await db.connect()
    try:
        assert stored(path)[0][0].startswith("tg-session:v1:")
        assert (await db.get_account("+100"))["session_blob_base64"] == "legacy-private-session"
    finally:
        await db.close()
    assert b"legacy-private-session" not in path.read_bytes()


@pytest.mark.asyncio
async def test_legacy_migration_retries_checkpoint_after_reader_releases_wal(tmp_path):
    path = tmp_path / "accounts.db"
    plaintext = "legacy-session-pinned-by-reader"
    reader = sqlite3.connect(path)
    try:
        reader.execute("PRAGMA journal_mode=WAL")
        reader.execute(CREATE_ACCOUNTS_TABLE)
        reader.execute(
            "INSERT INTO accounts(phone,fingerprint_json,session_blob_base64) VALUES('+100','{}',?)",
            (plaintext,),
        )
        reader.commit()
        assert reader.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        reader.execute("BEGIN")
        assert reader.execute("SELECT session_blob_base64 FROM accounts").fetchone() == (plaintext,)

        for _ in range(2):
            db = Database(path, tmp_path / "proxies.db")
            try:
                with pytest.raises(RuntimeError, match="checkpoint"):
                    await db.connect()
            finally:
                await db.close()
        assert plaintext.encode() in path.read_bytes()
    finally:
        reader.close()

    db = Database(path, tmp_path / "proxies.db")
    await db.connect()
    try:
        assert (await db.get_account("+100"))["session_blob_base64"] == plaintext
        for file in (path, path.with_name(path.name + "-wal")):
            assert not file.exists() or plaintext.encode() not in file.read_bytes()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovered_snapshot_survives_encryption_and_database_reopen(tmp_path):
    source = tmp_path / "recovery"
    source.mkdir()
    (source / "td.binlog").write_bytes(b"latest-recovered-session-state")
    snapshot = pack_session(source)
    path = tmp_path / "accounts.db"
    db = Database(path, tmp_path / "proxies.db")
    await db.connect()
    await db.create_account("+100", {})
    await db.save_session_blob("+100", snapshot)
    await db.close()
    reopened = Database(path, tmp_path / "proxies.db")
    await reopened.connect()
    try:
        restored = tmp_path / "restored"
        unpack_session((await reopened.get_account("+100"))["session_blob_base64"], restored)
        assert (restored / "td.binlog").read_bytes() == b"latest-recovered-session-state"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_failed_legacy_migration_preserves_all_rows(tmp_path):
    path = tmp_path / "accounts.db"
    with sqlite3.connect(path) as raw:
        raw.execute(CREATE_ACCOUNTS_TABLE)
        raw.execute("INSERT INTO accounts(phone,fingerprint_json,session_blob_base64) VALUES('+100','{}','legacy-private-session')")
        raw.execute("INSERT INTO accounts(phone,fingerprint_json,session_blob_base64) VALUES('+200','{}','tg-session:v1:invalid')")
    before = stored(path)
    db = Database(path, tmp_path / "proxies.db")
    with pytest.raises(RuntimeError):
        await db.connect()
    assert stored(path) == before
    assert not path.with_name(path.name + ".session.key").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["symlink", "public", "invalid"])
async def test_unsafe_key_rejected(tmp_path, unsafe):
    path = tmp_path / "accounts.db"
    key = path.with_name(path.name + ".session.key")
    if unsafe == "symlink":
        target = tmp_path / "unrelated"
        target.write_bytes(os.urandom(32))
        target.chmod(0o600)
        key.symlink_to(target)
    else:
        key.write_bytes(os.urandom(32) if unsafe == "public" else b"invalid")
        key.chmod(0o644 if unsafe == "public" else 0o600)
    db = Database(path, tmp_path / "proxies.db")
    with pytest.raises(RuntimeError):
        await db.connect()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "wrong", "tampered", "swapped", "downgrade"])
async def test_ciphertext_key_loss_or_tampering_fails_closed(tmp_path, failure):
    path = tmp_path / "accounts.db"
    db = Database(path, tmp_path / "proxies.db")
    await db.connect()
    try:
        await db.create_account("+100", {})
        await db.create_account("+200", {})
        await db.save_session_blob("+100", "private-one")
        await db.save_session_blob("+200", "private-two")
    finally:
        await db.close()
    key = path.with_name(path.name + ".session.key")
    if failure == "missing":
        key.unlink()
    elif failure == "wrong":
        key.write_bytes(os.urandom(32))
    else:
        with sqlite3.connect(path) as raw:
            value = stored(path)[0][0]
            if failure == "tampered":
                value = value[:-8] + "AAAAAAAA"
            elif failure == "downgrade":
                value = "plaintext-replacement"
            else:
                value = stored(path)[1][0]
            raw.execute("UPDATE accounts SET session_blob_base64=? WHERE phone='+100'", (value,))
    broken = Database(path, tmp_path / "proxies.db")
    with pytest.raises(RuntimeError):
        await broken.connect()
    if failure == "missing":
        assert not key.exists()


@pytest.mark.asyncio
async def test_concurrent_proxy_reservations_are_distinct_and_failure_preserves_assignment(tmp_path):
    db = Database(tmp_path / "accounts.db", tmp_path / "proxies.db")
    await db.connect()
    try:
        for number in (100, 200):
            await db.create_account(f"+{number}", {})
            proxy = await db.upsert_proxy(f"192.0.2.{number // 100}", 1080)
            await db.update_proxy_status(proxy, "alive", number)
        first, second = await asyncio.gather(db.reserve_proxy_for_account("+100"), db.reserve_proxy_for_account("+200"))
        assert first["id"] != second["id"]
        await db.update_proxy_status(first["id"], "dead")
        assert await db.reserve_proxy_for_account("+100") is None
        assert (await db.get_account("+100"))["proxy_id"] == first["id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_proxy_reservation_excludes_other_database_connections(tmp_path, monkeypatch):
    paths = (tmp_path / "accounts.db", tmp_path / "proxies.db")
    first_db, second_db = Database(*paths), Database(*paths)
    await first_db.connect()
    await second_db.connect()
    try:
        for number in (100, 200):
            await first_db.create_account(f"+{number}", {})
            proxy_id = await first_db.upsert_proxy(f"192.0.2.{number // 100}", 1080)
            await first_db.update_proxy_status(proxy_id, "alive", number)

        selected = asyncio.Event()
        release = asyncio.Event()
        select_proxy = first_db._best_available_proxy

        async def pause_after_selecting():
            proxy = await select_proxy()
            selected.set()
            await release.wait()
            return proxy

        monkeypatch.setattr(first_db, "_best_available_proxy", pause_after_selecting)
        await second_db._accounts_db.execute("PRAGMA busy_timeout=0")
        first = asyncio.create_task(first_db.reserve_proxy_for_account("+100"))
        try:
            await asyncio.wait_for(selected.wait(), timeout=5)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                await second_db.reserve_proxy_for_account("+200")
        finally:
            release.set()
            first_proxy = await first

        second_proxy = await second_db.reserve_proxy_for_account("+200")
        assert first_proxy["id"] != second_proxy["id"]
        assert (await second_db.get_account("+100"))["proxy_id"] == first_proxy["id"]
        assert (await first_db.get_account("+200"))["proxy_id"] == second_proxy["id"]
    finally:
        await first_db.close()
        await second_db.close()


@pytest.mark.asyncio
async def test_qr_placeholder_can_be_renamed_when_saving_session(tmp_path):
    db = Database(tmp_path / "accounts.db", tmp_path / "proxies.db")
    await db.connect()
    try:
        account_id = await db.create_account("qr_placeholder", {"lang_pack": "tdesktop"})
        await db.update_account("qr_placeholder", phone="+10000000001",
                                status="active", session_blob_base64="native-qr-session")
        assert await db.get_account("qr_placeholder") is None
        saved = await db.get_account("+10000000001")
        assert saved["id"] == account_id
        assert saved["status"] == "active"
        assert saved["session_blob_base64"] == "native-qr-session"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_strict_account_creation_preserves_winner_across_connections(tmp_path):
    paths = (tmp_path / "accounts.db", tmp_path / "proxies.db")
    first_db, second_db = Database(*paths), Database(*paths)
    await first_db.connect()
    await second_db.connect()
    try:
        results = await asyncio.gather(
            first_db.create_account("+100", {"owner": "first"}, session_blob_base64="first", reject_existing=True),
            second_db.create_account("+100", {"owner": "second"}, session_blob_base64="second", reject_existing=True),
            return_exceptions=True,
        )
        assert sum(isinstance(result, int) for result in results) == 1
        assert sum(isinstance(result, ValueError) and "already exists" in str(result) for result in results) == 1
        winner = "first" if isinstance(results[0], int) else "second"
        winning_id = next(result for result in results if isinstance(result, int))
        assert await second_db.create_account("+100", {}, session_blob_base64="replacement") == winning_id
        rows = await first_db.list_accounts()
        assert len(rows) == 1
        assert rows[0]["fingerprint_json"] == '{"owner": "' + winner + '"}'
        assert rows[0]["session_blob_base64"] == winner
    finally:
        await first_db.close()
        await second_db.close()
