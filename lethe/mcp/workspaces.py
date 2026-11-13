"""Resolve MCP project identity from explicit paths or client-advertised roots."""
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import anyio
from mcp.server.mcpserver import Context
from lethe.core.projects import register_project


def file_uri_path(uri: str) -> Optional[str]:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    return url2pathname(unquote(parsed.path))


def resolve_client_workspace(memory, workspace_id: Optional[str], workspace_root: Optional[str], ctx: Optional[Context] = None) -> tuple[Optional[str], Optional[str]]:
    if workspace_root:
        return register_project(memory.db, workspace_root, workspace_id), str(Path(workspace_root).expanduser().resolve())
    if workspace_id:
        return workspace_id, None
    if ctx is None:
        return None, None
    try:
        capabilities = ctx.client_capabilities
    except (ValueError, RuntimeError):
        return None, None
    if capabilities is None or getattr(capabilities, "roots", None) is None:
        return None, None

    async def read_roots():
        with anyio.fail_after(2):
            return await ctx.session.list_roots()

    try:
        result = anyio.from_thread.run(read_roots)
    except Exception as exc:
        raise ValueError("Client workspace roots are unavailable; pass workspace_id and workspace_root explicitly") from exc
    paths = [file_uri_path(str(root.uri)) for root in result.roots]
    paths = list(dict.fromkeys(p for p in paths if p))
    if not paths:
        return None, None
    if len(paths) > 1:
        raise ValueError("Client has multiple workspace roots; pass workspace_root explicitly")
    candidate = Path(paths[0]).expanduser().resolve()
    if not candidate.is_dir() or candidate == Path.home().resolve() or candidate == candidate.parent:
        return None, None
    return register_project(memory.db, str(candidate)), str(candidate)
