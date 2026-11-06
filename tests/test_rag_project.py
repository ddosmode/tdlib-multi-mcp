"""Real SQLite checks for atomic incremental RAG updates without model/network access."""
import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("rag_project", Path(__file__).parents[1] / "scripts/rag_project.py")
rag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rag)


class Vector:
    def astype(self, _):
        return self

    def tobytes(self):
        return b"1234"


@pytest.fixture
def setup_index(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("current architecture and deployment instructions")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE documents(id TEXT,title TEXT,category TEXT,source TEXT,path TEXT,created_at TEXT); CREATE TABLE chunks(id TEXT,doc_id TEXT,chunk_index INTEGER,text TEXT,vector BLOB,metadata TEXT);")
    return tmp_path, conn


def test_incremental_update_delete_and_ignore_untracked(setup_index):
    root, conn = setup_index
    embed = lambda _: Vector()
    chunk = lambda text: [text]
    assert rag.sync(root, conn, embed, chunk)["updated"] == 1
    assert rag.sync(root, conn, embed, chunk)["updated"] == 0
    (root / "untracked.md").write_text("must not be indexed")
    (root / "README.md").write_text("new architecture after the change")
    assert rag.sync(root, conn, embed, chunk)["updated"] == 1
    assert conn.execute("SELECT text FROM chunks").fetchall() == [("new architecture after the change",)]
    (root / "README.md").unlink()
    assert rag.sync(root, conn, embed, chunk)["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_embedding_failure_keeps_previous_index(setup_index):
    root, conn = setup_index
    rag.sync(root, conn, lambda _: Vector(), lambda text: [text])
    before = conn.execute("SELECT * FROM chunks").fetchall()
    (root / "README.md").write_text("modified document")
    def fail(_):
        raise RuntimeError("model unavailable")
    with pytest.raises(RuntimeError, match="model unavailable"):
        rag.sync(root, conn, fail, lambda text: [text])
    assert conn.execute("SELECT * FROM chunks").fetchall() == before
    assert rag.sync(root, conn, lambda _: Vector(), lambda text: [text])["updated"] == 1


def test_secrets_and_symlinks_excluded(tmp_path):
    (tmp_path / "credentials.env").write_text("secret")
    assert rag.safe_content(tmp_path, "credentials.env") is None
    (tmp_path / "safe.md").write_text("API hash " + "a" * 32 + "\nTG_API_HASH=abcdef\n")
    text = rag.safe_content(tmp_path, "safe.md")
    assert "a" * 32 not in text
    assert "abcdef" not in text
    (tmp_path / "link.md").symlink_to(tmp_path / "safe.md")
    assert rag.safe_content(tmp_path, "link.md") is None


def test_embedding_model_change_invalidates_cache(setup_index):
    root, conn = setup_index
    rag.sync(root, conn, lambda _: Vector(), lambda text: [text], "model-a")
    assert rag.sync(root, conn, lambda _: Vector(), lambda text: [text], "model-a")["updated"] == 0
    assert rag.sync(root, conn, lambda _: Vector(), lambda text: [text], "model-b")["updated"] == 1


def test_extensionless_shebang_scripts_indexed_and_binary_skipped(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/run-server").write_text("#!/usr/bin/env bash\nexec python server.py\n")
    (tmp_path / "scripts/binary").write_bytes(b"\x00\xffexecutable")
    (tmp_path / "scripts/binary-shebang").write_bytes(b"#!/bin/sh\n\x00binary")
    (tmp_path / "scripts/invalid-utf8").write_bytes(b"#!\xff")
    (tmp_path / "scripts/no-shebang").write_text("ordinary unsupported extensionless content")
    (tmp_path / "scripts/secret-run").write_text("#!/bin/sh\necho credential\n")
    assert rag.safe_content(tmp_path, "scripts/run-server").startswith("#!/")
    assert rag.safe_content(tmp_path, "scripts/binary") is None
    assert rag.safe_content(tmp_path, "scripts/binary-shebang") is None
    assert rag.safe_content(tmp_path, "scripts/invalid-utf8") is None
    assert rag.safe_content(tmp_path, "scripts/no-shebang") is None
    assert rag.safe_content(tmp_path, "scripts/secret-run") is None


def test_lexical_fallback_reads_fresh_tracked_text_and_redacts(setup_index):
    root, _ = setup_index
    (root / 'README.md').write_text('fresh live_telegram results\nTG_API_HASH=private-value\n')
    (root / 'untracked.md').write_text('live_telegram hidden text')
    hits = rag.lexical_search(root, 'live_telegram results', 5)
    assert len(hits) == 1
    assert hits[0]['search_mode'] == 'lexical'
    assert 'fresh live_telegram results' in hits[0]['text']
    assert 'private-value' not in hits[0]['text']
