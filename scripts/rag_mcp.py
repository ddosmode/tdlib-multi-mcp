"""Project-scoped MCP access to the local TDLib knowledge index."""
import asyncio
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tdlib-project-rag")
COMMAND = Path(__file__).with_name("rag")


async def run(*args: str, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(str(COMMAND), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        # Error output can contain local paths and operational diagnostics, never input secrets.
        raise RuntimeError(f"Project RAG command failed ({proc.returncode}): {stderr.decode(errors='replace')[-2000:]}")
    return stdout.decode()


@mcp.tool()
async def project_knowledge_query(question: str, top_k: int = 5) -> str:
    """Search tdlib-multi-mcp project knowledge before planning or modifying the project."""
    if not question.strip() or len(question) > 4000 or not 1 <= top_k <= 20:
        raise ValueError("Use a nonempty question up to 4000 characters and top_k from 1 to 20")
    try:
        return await run("query", question, "--top-k", str(top_k), timeout=12)
    except (TimeoutError, RuntimeError):
        return await run("query", question, "--top-k", str(top_k), "--lexical", timeout=10)


@mcp.tool()
async def project_knowledge_sync() -> str:
    """Refresh knowledge after project changes; only tracked safe project files are indexed."""
    return await run("sync", timeout=900)


if __name__ == "__main__":
    mcp.run(transport="stdio")
