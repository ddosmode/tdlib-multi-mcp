"""Incremental, transactionally replaced project documents in the local Ubuntu RAG."""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATEGORY = "project:tdlib-multi-mcp"
ALLOWED = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".sh"}
DENIED = re.compile(r"(^|/)(?:data|venv|\.venv|\.git|logs|__pycache__)(/|$)|(?:\.env|\.db|\.sqlite|\.session|\.pem|\.key)$|(?:^|/)[^/]*(?:credential|secret|password|token)[^/]*$", re.I)


def safe_content(root: Path, relative: str) -> str | None:
    path = root / relative
    if DENIED.search(relative) or path.suffix not in ALLOWED or path.is_symlink():
        return None
    if not path.is_file() or path.stat().st_size > 100_000:
        return None
    content = path.read_text(encoding="utf-8")
    # Never send API hashes, authorization keys or long serialized sessions to embeddings.
    content = re.sub(r"\b[0-9a-fA-F]{32,}\b", "[REDACTED_HEX]", content)
    content = re.sub(r"(?<![\w/])[A-Za-z0-9_+/=-]{100,}(?!\w)", "[REDACTED_BLOB]", content)
    content = re.sub(r"(?im)^(\s*(?:TG_API_HASH|api_hash|password|token|secret)\s*=).+$", r"\1 [REDACTED]", content)
    return content


def tracked_contents(root: Path) -> dict[str, str]:
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    result = {}
    for name in filter(None, names):
        content = safe_content(root, name)
        if content and content.strip():
            result[name] = content
    return result


def sync(root: Path, conn: sqlite3.Connection, embed, chunker, model_identity: str = "test") -> dict:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("CREATE TABLE IF NOT EXISTS project_rag_manifest (category TEXT, path TEXT, digest TEXT, PRIMARY KEY(category,path))")
    conn.commit()
    existing = dict(conn.execute("SELECT path,digest FROM project_rag_manifest WHERE category=?", (CATEGORY,)))
    contents = tracked_contents(root)
    digests = {name: hashlib.sha256(("sanitizer-v1:" + model_identity + "\0" + text).encode()).hexdigest() for name, text in contents.items()}
    changed = {name for name in contents if existing.get(name) != digests[name]}
    removed = set(existing) - set(contents)
    prepared = {}
    # Prepare every embedding before beginning the mutation transaction. Errors keep old knowledge intact.
    for name in sorted(changed):
        chunks = chunker(contents[name])
        prepared[name] = [(text, embed(text)) for text in chunks]
        if not prepared[name]:
            prepared[name] = [(contents[name], embed(contents[name]))]
    with conn:
        for name in sorted(changed | removed):
            absolute = str(root / name)
            conn.execute("DELETE FROM chunks WHERE doc_id IN (SELECT id FROM documents WHERE category=? AND path=?)", (CATEGORY, absolute))
            conn.execute("DELETE FROM documents WHERE category=? AND path=?", (CATEGORY, absolute))
            conn.execute("DELETE FROM project_rag_manifest WHERE category=? AND path=?", (CATEGORY, name))
        for name, chunks in prepared.items():
            doc_id = str(uuid.uuid4())
            absolute = str(root / name)
            conn.execute("INSERT INTO documents(id,title,category,source,path,created_at) VALUES(?,?,?,?,?,?)", (doc_id, f"tdlib-multi-mcp: {name}", CATEGORY, "local_project", absolute, datetime.datetime.now(datetime.UTC).isoformat()))
            for idx, (text, vector) in enumerate(chunks):
                conn.execute("INSERT INTO chunks(id,doc_id,chunk_index,text,vector,metadata) VALUES(?,?,?,?,?,?)", (f"{doc_id}_{idx}", doc_id, idx, text, vector.astype("float32").tobytes(), json.dumps({"file": absolute, "sha256": digests[name]})))
            conn.execute("INSERT INTO project_rag_manifest VALUES(?,?,?)", (CATEGORY, name, digests[name]))
    return {"category": CATEGORY, "tracked_documents": len(contents), "updated": len(changed), "removed": len(removed), "chunks_updated": sum(map(len, prepared.values()))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync")
    query = sub.add_parser("query")
    query.add_argument("question")
    query.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    sys.path.insert(0, str(Path.home() / ".local/share/ubuntu-rag"))
    from engine.config import KNOWLEDGE_DB, EMBED_MODEL, OLLAMA_URL
    from engine.db import init_db, vector_search
    from engine.embeddings import get_embedding
    from engine.indexer import chunk_text
    init_db()
    if args.command == "sync":
        lock_path = KNOWLEDGE_DB.parent / "tdlib-project-index.lock"
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with sqlite3.connect(KNOWLEDGE_DB, timeout=30) as conn:
                result = sync(ROOT, conn, get_embedding, chunk_text, EMBED_MODEL + "@" + OLLAMA_URL)
    else:
        result = vector_search(get_embedding(args.question), top_k=args.top_k, category=CATEGORY)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
