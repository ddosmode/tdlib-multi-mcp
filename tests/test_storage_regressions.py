import base64
import asyncio
import io
import sqlite3
import stat
import tarfile

import pytest

from core.session_manager import pack_session, unpack_session
from db.database import Database
from db.models import CREATE_ACCOUNTS_TABLE, CREATE_AUTH_LOG_TABLE


@pytest.mark.asyncio
async def test_account_upsert_preserves_identity_and_session(tmp_path):
    db = Database(tmp_path / "accounts.db", tmp_path / "proxies.db")
    await db.connect()
    try:
        first = await db.create_account("+100", {"device_model": "original"})
        await db.update_account("+100", status="active", session_blob_base64="blob")
        await db.create_account("+200", {"device_model": "other"})
        again = await db.create_account("+100", {"device_model": "replacement"})
        row = await db.get_account("+100")
        assert again == first
        assert row["fingerprint_json"] == '{"device_model": "original"}'
        assert row["status"] == "active"
        assert row["session_blob_base64"] == "blob"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_proxy_selection_excludes_assigned_proxy_across_databases(tmp_path):
    db = Database(tmp_path / "accounts.db", tmp_path / "proxies.db")
    await db.connect()
    try:
        fast = await db.upsert_proxy("192.0.2.1", 1080)
        slow = await db.upsert_proxy("192.0.2.2", 1080)
        await db.update_proxy_status(fast, "alive", 1)
        await db.update_proxy_status(slow, "alive", 2)
        await db.create_account("+100", {}, fast)
        assert (await db.get_best_available_proxy())["id"] == slow
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_database_files_private_and_timestamp_is_value(tmp_path):
    db = Database(tmp_path / "accounts.db", tmp_path / "proxies.db")
    await db.connect()
    try:
        await db.create_account("+100", {})
        await db.save_session_blob("+100", "blob")
        assert (await db.get_account("+100"))["last_active"].endswith("Z")
        assert stat.S_IMODE((tmp_path / "accounts.db").stat().st_mode) == 0o600
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_cross_database_fk_migrates_without_data_loss(tmp_path):
    accounts = tmp_path / "accounts.db"
    with sqlite3.connect(accounts) as connection:
        connection.execute(CREATE_ACCOUNTS_TABLE.replace(
            "proxy_id            INTEGER,", "proxy_id INTEGER REFERENCES proxies(id),"))
        connection.execute(CREATE_AUTH_LOG_TABLE)
        connection.execute("INSERT INTO accounts(phone, fingerprint_json) VALUES('+100', '{}')")
        connection.execute("INSERT INTO auth_log(account_id, event) VALUES(1, 'before')")
    db = Database(accounts, tmp_path / "proxies.db")
    await db.connect()
    try:
        assert (await db.get_account("+100"))["id"] == 1
        await db.create_account("+200", {})
        await db.log_auth_event(1, "after")
        async with db._accounts_db.execute("SELECT event FROM auth_log ORDER BY id") as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["before", "after"]
        async with db._accounts_db.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_creates_return_one_identity(tmp_path):
    db = Database(tmp_path / "accounts.db", tmp_path / "proxies.db")
    await db.connect()
    try:
        ids = await asyncio.gather(*(db.create_account("+100", {"value": n}) for n in range(20)))
        assert len(set(ids)) == 1
        assert len(await db.list_accounts()) == 1
        with pytest.raises(ValueError):
            await db.update_account("+100", fingerprint_json="replacement")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_wal_sidecars_become_private(tmp_path):
    accounts = tmp_path / "accounts.db"
    with sqlite3.connect(accounts) as existing:
        existing.execute("PRAGMA journal_mode=WAL")
        existing.execute("CREATE TABLE existing_data(value TEXT)")
        existing.commit()
        sidecars = [accounts.with_name(accounts.name + suffix) for suffix in ("-wal", "-shm")]
        for path in sidecars:
            path.chmod(0o644)
        db = Database(accounts, tmp_path / "proxies.db")
        await db.connect()
        try:
            assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in sidecars)
        finally:
            await db.close()


@pytest.mark.asyncio
async def test_sidecar_symlink_is_rejected_without_changing_target(tmp_path):
    accounts = tmp_path / "accounts.db"
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    target.chmod(0o644)
    accounts.with_name(accounts.name + "-wal").symlink_to(target)
    db = Database(accounts, tmp_path / "proxies.db")
    with pytest.raises(OSError):
        await db.connect()
    assert target.read_text() == "unchanged"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def _archive(member):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.addfile(member)
    return base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.parametrize("name", ["session/../escaped", "/absolute"])
def test_unpack_rejects_unsafe_paths(tmp_path, name):
    with pytest.raises(ValueError):
        unpack_session(_archive(tarfile.TarInfo(name)), tmp_path / "restored")


def test_unpack_rejects_symlinks(tmp_path):
    member = tarfile.TarInfo("session/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"
    with pytest.raises(ValueError):
        unpack_session(_archive(member), tmp_path / "restored")


def test_pack_rejects_symlinks_and_unpack_is_private(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"session data")
    restored = tmp_path / "restored"
    unpack_session(pack_session(source), restored)
    assert stat.S_IMODE(restored.stat().st_mode) == 0o700
    assert stat.S_IMODE((restored / "data").stat().st_mode) == 0o600
    (source / "link").symlink_to(source / "data")
    with pytest.raises(ValueError):
        pack_session(source)
