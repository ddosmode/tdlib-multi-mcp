"""
conftest.py — pytest configuration for tdlib-multi-mcp.

Sets TG_MCP_TESTING=1 so config.py uses dummy credentials in tests.
"""

import os
os.environ.setdefault("TG_MCP_TESTING", "1")

# Also redirect RAM-disk and DB paths to temp directories during tests
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="tg_mcp_test_"))

os.environ.setdefault("TG_MCP_TEST_TMPDIR", str(_tmp))
