import asyncio
import json
from pathlib import Path

import pytest

from lethe.core.engine import LetheMemory
from lethe.core.projects import register_project, project_root
from lethe.core.drift import check_code_drift
from lethe.mcp.server import create_mcp_server
from lethe.models import CodeAnchor


@pytest.fixture
def memory(tmp_path):
    mem = LetheMemory(tmp_path / "vault.db", tmp_path / "procedures", workspace_id="global_desktop")
    yield mem
    mem.close()


def test_project_name_and_html_anchor_recall(memory, tmp_path, monkeypatch):
    root = tmp_path / "testlethe"
    root.mkdir()
    (root / "index.html").write_text("<title>MacBook Air M4</title><h1>Choose your finish</h1>")
    node = memory.store(
        "A standalone landing page for MacBook Air M4 using React 18 and Tailwind CSS with a live configurator.",
        category="architecture", workspace_root=str(root), code_anchor=CodeAnchor(file_path="index.html"),
    )
    assert node.workspace_id == "testlethe"
    assert node.code_anchor.workspace_root == str(root)
    other = tmp_path / "unrelated"
    other.mkdir()
    monkeypatch.chdir(other)
    for query in ("testlethe", "lethe recall testlethe", "what is the HTML page about?", "testlethe HTML page"):
        result = memory.recall(query)
        assert node.id in [m.id for m in result.memories], (query, result.context_text)
        assert result.drift_warnings == []
    inspected = memory.build_session_context()
    assert not inspected["drift_warnings"]


def test_legacy_relative_anchor_is_unverified_not_missing(memory):
    node = memory.store("MacBook Air landing page architecture", workspace_id="testlethe", code_anchor=CodeAnchor(file_path="index.html"))
    result = memory.recall("HTML page")
    assert node.id in [m.id for m in result.memories]
    assert any("unverified" in warning for warning in result.drift_warnings)
    assert not any("no longer exists" in warning for warning in result.drift_warnings)


def test_same_name_roots_are_distinct_and_ambiguous(memory, tmp_path):
    roots = [tmp_path / "one" / "demo", tmp_path / "two" / "demo"]
    names = []
    for i, root in enumerate(roots):
        root.mkdir(parents=True)
        node = memory.store(f"Architecture number {i}", category="architecture", workspace_root=str(root))
        names.append(node.workspace_id)
    assert names[0] == "demo" and names[1].startswith("demo-")
    assert names[0] != names[1]
    result = memory.recall("demo")
    assert result.is_abstention and "ambiguous" in result.context_text
    assert project_root(memory.db, names[1]) == str(roots[1])
    with pytest.raises(ValueError, match="different root"):
        third = tmp_path / "three"
        third.mkdir()
        register_project(memory.db, str(third), names[0])


def test_query_project_selection_does_not_bypass_trust(memory, tmp_path):
    root = tmp_path / "vendor"
    root.mkdir()
    node = memory.store("A restricted HTML page", workspace_root=str(root))
    memory.security.untrust_workspace(node.workspace_id)
    assert memory.recall("vendor").is_abstention
    assert memory.recall("HTML page").is_abstention


def test_cross_platform_anchor_does_not_report_deletion():
    drifted, warning = check_code_drift(CodeAnchor(file_path="index.html"), workspace_dir="testlethe")
    assert not drifted and "unverified" in warning
    if __import__("os").name != "nt":
        drifted, warning = check_code_drift(CodeAnchor(file_path=r"C:\projects\testlethe\index.html"))
        assert not drifted and "another platform" in warning


def test_desktop_mcp_inspection_and_project_identity(tmp_path, monkeypatch):
    # Server launch directory must not become the client's project identity.
    launch = tmp_path / "Lethe"
    launch.mkdir()
    monkeypatch.chdir(launch)
    root = tmp_path / "testlethe"
    root.mkdir()
    (root / "index.html").write_text("<h1>MacBook Air</h1>")
    server = create_mcp_server(tmp_path / "mcp.db", tmp_path / "procs")
    try:
        tools = server._tool_manager._tools
        stored = json.loads(tools["lethe_store"].fn(content="MacBook Air landing page", category="architecture", code_file="index.html", workspace_root=str(root)))
        assert stored["workspace_id"] == "testlethe"
        recalled = json.loads(tools["lethe_recall"].fn(query="testlethe"))
        assert recalled["matched_workspaces"] == ["testlethe"]
        assert recalled["drift_warnings"] == []
        inspected = json.loads(tools["lethe_inspect"].fn())
        assert inspected["items"][0]["workspace_id"] == "testlethe"
        assert inspected["items"][0]["workspace_root"] == str(root)
        projects = json.loads(tools["lethe_projects"].fn())
        assert projects["projects"][0]["workspace_id"] == "testlethe"
    finally:
        server._lethe_memory.close()


def test_mcp_root_discovery(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import ListRootsResult, Root
    import sys
    root = tmp_path / "testlethe"
    root.mkdir()
    (root / "index.html").write_text("<h1>MacBook Air</h1>")

    async def roots(_):
        return ListRootsResult(roots=[Root(uri=root.as_uri(), name="testlethe")])

    async def run():
        async with stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "lethe.cli", "mcp", "--db", str(tmp_path / "roots.db"), "--procedures-dir", str(tmp_path / "procs")])) as (read, write):
            async with ClientSession(read, write, list_roots_callback=roots) as session:
                await session.initialize()
                res = await session.call_tool("lethe_store", {"content": "MacBook Air landing page", "category": "architecture", "code_file": "index.html"})
                assert not res.is_error, res
                stored = json.loads(res.content[0].text)
                assert stored["workspace_id"] == "testlethe"
                assert stored["code_anchor"]["workspace_root"] == str(root)
    asyncio.run(run())


def test_mcp_root_discovery_home_falls_back_to_global_desktop(memory):
    from types import SimpleNamespace
    import anyio
    from lethe.mcp.workspaces import resolve_client_workspace

    class _Session:
        async def list_roots(self):
            return SimpleNamespace(roots=[SimpleNamespace(uri=Path.home().resolve().as_uri())])

    ctx = SimpleNamespace(client_capabilities=SimpleNamespace(roots=SimpleNamespace()), session=_Session())
    ws_id, ws_root = anyio.run(
        lambda: anyio.to_thread.run_sync(lambda: resolve_client_workspace(memory, None, None, ctx=ctx))
    )
    assert ws_id is None and ws_root is None
