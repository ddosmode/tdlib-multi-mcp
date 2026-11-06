# Project knowledge index

Run `scripts/rag sync` after changing tracked project files, and before handing work over.
Run `scripts/rag query 'your question'` before planning changes. Results cite current source files.

The user service `tdlib-rag.timer` checks tracked working-tree files every 60 seconds.
It reads the working tree, including uncommitted changes to tracked files. New files enter the
index after `git add`. Deleted documents are removed on the next successful sync.

The index uses the existing Ubuntu RAG database and local Ollama embedding model, with category
`project:tdlib-multi-mcp`. Hashes of sanitized contents avoid re-embedding unchanged files.
All replacement chunks are prepared first, then documents and the manifest change in one SQLite
transaction. If embedding fails, the old index stays intact and the service reports failure.

Only tracked text/source extensions and scripts with a shebang are accepted; data, logs, environments, symlinks and oversized
files are excluded. Long hex and serialized blobs are redacted before embedding. Secrets must never
be added to Git. Sanitization is defense in depth, not a substitute for this rule.

Check operation with `systemctl --user status tdlib-rag.timer tdlib-rag.service` and
`journalctl --user -u tdlib-rag.service -n 20`. Query scope is restricted to this project.
The timer works while the user systemd manager is running. No system-wide service is installed.

## Agent tools

Register `venv/bin/python scripts/rag_mcp.py` with the MCP hub as `tdlib-project-rag`.
It exposes `project_knowledge_query(question, top_k=5)` and `project_knowledge_sync()`.
The server accepts no file paths or shell commands. Both tools are limited to this checkout.
Install or refresh the automatic service with `scripts/rag-install.sh`.
