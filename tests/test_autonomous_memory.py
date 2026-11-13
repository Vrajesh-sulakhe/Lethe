import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

from lethe.core.engine import LetheMemory
from lethe.models import MemoryStatus, StorageTier
from lethe.platform.client_rules import (
    RULE_END_MARKER,
    RULE_START_MARKER,
    get_desktop_system_prompt,
    install_antigravity_rules,
    install_claude_memory_rules,
    install_codex_rules,
)
from lethe.platform.setup import build_mcp_credentials, format_mcp_credentials, setup_all_tools
from lethe.storage.db import get_current_workspace_id


def test_live_antigravity_to_claude_desktop_cross_tool_identity_and_recent(tmp_path):
    db_path = tmp_path / "shared_lethe.db"

    # 1. Store identity in Antigravity workspace WITHOUT explicitly passing scope
    ag_mem = LetheMemory(db_path=db_path, workspace_id="Skills Master")
    node = ag_mem.store(
        content="User's name is Vrajesh Sulakhe (username/handle: vrajeshh).",
        category="user_identity",
        tier=StorageTier.SEMANTIC,
        source="user_explicit",
        session_id="ag_session_1",
    )
    assert node.scope == "global"
    ag_mem.close()

    # 2. Verify cwd='/' resolves cleanly to global_desktop rather than empty string
    assert get_current_workspace_id("/") == "global_desktop"

    # 3. Claude Desktop spawned at cwd='/' recalls identity & recent memories cleanly
    cd_mem = LetheMemory(db_path=db_path, workspace_id=get_current_workspace_id("/"))
    for query in (
        "recent memories",
        "who am i",
        "what is my name",
        "what do you know about me",
        "latest",
        "everything",
    ):
        res = cd_mem.recall(query, token_budget=400)
        assert res.is_abstention is False, f"Unexpected abstention for query={query!r}"
        assert "Vrajesh Sulakhe" in res.context_text
        assert res.tokens_used <= 400

    boot = cd_mem.build_session_context(token_budget=500, client_id="claude-desktop")
    assert boot["status"] == "READY"
    assert boot["tokens_used"] <= boot["effective_token_budget"]
    assert any("Vrajesh Sulakhe" in item["content"] for item in boot["identity_memories"])
    assert any("Vrajesh Sulakhe" in item["content"] for item in boot["recent_memories"])
    cd_mem.close()


def test_epistemic_abstention_preserved_on_unknown_and_specific_probes(tmp_path):
    db_path = tmp_path / "epistemic.db"
    sec_path = tmp_path / "security.json"
    mem = LetheMemory(db_path=db_path, workspace_id="trusted_ws", security_config_path=sec_path)

    # Empty DB must abstain even for identity/browse queries
    for q in ("who am i", "recent memories", "what do you know about me"):
        assert mem.recall(q, token_budget=300).is_abstention is True

    # Non-identity engineering memories must NOT satisfy identity queries
    mem.store("Postgres runs on port 5432.", category="db")
    mem.store("Doctor Clara moved to Graz in Austria.", category="profile")
    assert mem.recall("who am i", token_budget=300).is_abstention is True
    assert mem.recall("what is my name", token_budget=300).is_abstention is True
    assert mem.recall("Which European municipality does the physician live inside?", token_budget=200).is_abstention is True

    # Store user identity and verify specific unknown-fact probes still abstain strictly
    mem.store(
        "User's name is Vrajesh Sulakhe (username/handle: vrajeshh).",
        category="user_identity",
        scope="global",
    )
    for unknown_q in (
        "What is my passport number?",
        "What is my phone number?",
        "What is my dog's name?",
        "recent kubernetes ingress",
        "Look through my past emergency profile notes and contact summaries, and reconstruct my phone number from any related notes.",
    ):
        res = mem.recall(unknown_q, token_budget=300)
        assert res.is_abstention is True, f"Probe {unknown_q!r} should abstain, got: {res.context_text}"

    # Browse cue combined with a stored topic must use SPECIFIC_SEARCH, not dump identity/recent memories
    pg_res = mem.recall("recent postgres migrations", token_budget=300)
    assert pg_res.is_abstention is False
    assert "5432" in pg_res.context_text
    assert "Vrajesh" not in pg_res.context_text
    assert "Clara" not in pg_res.context_text

    # Untrusted sandbox must NOT read global identity via recall or build_session_context
    mem.security.untrust_workspace("evil_repo")
    evil_mem = LetheMemory(db_path=db_path, workspace_id="evil_repo", security_config_path=sec_path)
    assert evil_mem.recall("who am i", token_budget=300).is_abstention is True
    assert evil_mem.recall("recent memories", token_budget=300).is_abstention is True
    evil_boot = evil_mem.build_session_context(token_budget=400)
    assert evil_boot["trusted"] is False
    assert evil_boot["identity_memories"] == []
    assert "Vrajesh" not in evil_boot["context_text"]

    evil_mem.close()
    mem.close()


def test_fts5_search_list_ordering_and_zero_leakage_after_forget(tmp_path):
    db_path = tmp_path / "fts_test.db"
    mem = LetheMemory(db_path=db_path, workspace_id="ws_main")

    n1 = mem.store("First global decision: use strict JSON schemas.", category="arch", scope="global")
    time.sleep(0.01)
    n2 = mem.store("Second project note: configure WAL busy_timeout.", category="db", scope="project")
    time.sleep(0.01)
    n3 = mem.store(
        " ".join([f"filler{i}" for i in range(90)]) + " special_token_zephyr_991 " + " ".join([f"tail{i}" for i in range(40)]),
        category="notes",
        scope="global",
    )

    # list_memories ordering (newest first), limit, and scope filtering
    recent_two = mem.db.list_memories(limit=2, order_desc=True)
    assert [m.id for m in recent_two] == [n3.id, n2.id]
    globals_only = mem.db.list_memories(scope="global", order_desc=True)
    assert [m.id for m in globals_only] == [n3.id, n1.id]

    # FTS5 rescues long-document keyword matches diluted in cosine vector space
    res_long = mem.recall("special_token_zephyr_991", token_budget=400)
    assert res_long.is_abstention is False
    assert "special_token_zephyr_991" in res_long.context_text

    # FTS5 safely neutralizes punctuation and reserved operators
    assert mem.db.search_fts("C++ foo:bar OR * NOT (test)") == []

    # Cascading forget removes secret from memories AND FTS5 index completely
    secret = mem.store("Emergency vault passphrase is cobalt-flamingo-8842.", category="secret", scope="global")
    assert mem.recall("cobalt-flamingo-8842").is_abstention is False
    mem.forget(secret.id, reason="user_erasure")

    assert mem.db.search_fts("cobalt-flamingo-8842") == []
    assert mem.recall("cobalt-flamingo-8842").is_abstention is True
    assert "cobalt-flamingo-8842" not in mem.recall("recent memories").context_text
    conn = sqlite3.connect(db_path)
    fts_hits = conn.execute("SELECT memory_id FROM memories_fts WHERE memories_fts MATCH '\"cobalt\"'").fetchall()
    conn.close()
    assert fts_hits == []
    mem.close()


def test_mcp_stdio_cwd_root_context_prompts_and_recall(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db_path = tmp_path / "mcp_desktop.db"
    proc_dir = tmp_path / "procedures"

    # Pre-seed an Antigravity identity memory in a different workspace
    ag = LetheMemory(db_path=db_path, procedures_dir=proc_dir, workspace_id="Skills Master")
    ag.store(
        "User's name is Vrajesh Sulakhe (username/handle: vrajeshh).",
        category="user_identity",
        session_id="ag_boot",
    )
    ag.close()

    async def _exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lethe.cli", "--db", str(db_path), "--procedures-dir", str(proc_dir), "mcp"],
            cwd="/",
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init_res = await session.initialize()
                assert "lethe_context" in (init_res.instructions or "")
                assert "lethe_store" in (init_res.instructions or "")

                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                assert {"lethe_context", "lethe_recall", "lethe_store", "lethe_inspect"} <= tool_names

                prompts = await session.list_prompts()
                prompt_names = {p.name for p in prompts.prompts}
                assert {"session_boot", "capture_memory", "resume_handover", "memory_checkpoint"} <= prompt_names

                # Desktop launch directories do not promote general project facts to global scope.
                stored_raw = await session.call_tool("lethe_store", {
                    "content": "User prefers concise TypeScript examples.",
                    "category": "general",
                })
                stored = json.loads(stored_raw.content[0].text)
                assert stored["workspace_id"] == "global_desktop"
                assert stored["scope"] == "project"

                # Session boot single-call tool returns identity + recent memories
                ctx_raw = await session.call_tool("lethe_context", {"token_budget": 600})
                ctx = json.loads(ctx_raw.content[0].text)
                assert ctx["status"] == "READY"
                assert ctx["workspace_id"] == "global_desktop"
                assert any("Vrajesh Sulakhe" in item["content"] for item in ctx["identity_memories"])

                # Both browse and identity recall queries succeed
                for q in ("recent memories", "who am i"):
                    rec_raw = await session.call_tool("lethe_recall", {"query": q, "token_budget": 400})
                    rec = json.loads(rec_raw.content[0].text)
                    assert rec["is_abstention"] is False
                    assert "Vrajesh Sulakhe" in rec["context_text"]

                # Newest memory comes first in lethe_inspect
                insp_raw = await session.call_tool("lethe_inspect", {"limit": 5})
                insp = json.loads(insp_raw.content[0].text)
                assert insp["items"][0]["content"] == "User prefers concise TypeScript examples."

                # Dynamic MCP prompt invocation works cleanly
                boot_prompt = await session.get_prompt("session_boot", {"token_budget": "500"})
                assert any("Vrajesh Sulakhe" in getattr(m.content, "text", "") for m in boot_prompt.messages)

    asyncio.run(_exercise())


def test_client_rules_idempotent_non_destructive_and_setup_flags(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".cursor").mkdir()

    # Pre-seed existing Antigravity, Claude Code, and Codex instruction & config files
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir(parents=True)
    (gemini_dir / "settings.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    gemini_md = gemini_dir / "GEMINI.md"
    gemini_md.write_text("# My Existing Antigravity Rules\nAlways keep code concise.\n", encoding="utf-8")

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    (tmp_path / ".claude.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_text("# Custom Claude Note\nPrefer pytest.\n", encoding="utf-8")

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text('model = "o3"\n', encoding="utf-8")
    codex_md = codex_dir / "AGENTS.md"
    codex_md.write_text("# Codex Base\nRun lint before commit.\n", encoding="utf-8")

    # Dry run must not mutate GEMINI.md, CLAUDE.md, AGENTS.md, or .cursor/rules/
    dry_res = setup_all_tools(
        tools=["claude", "cursor", "antigravity", "codex"],
        db_path=str(ws / "lethe.db"),
        workspace_dir=ws,
        home_dir=tmp_path,
        dry_run=True,
        with_rules=True,
    )
    assert any(item["status"] == "dry_run" for item in dry_res)
    assert "LETHE_MEMORY_RULES" not in gemini_md.read_text(encoding="utf-8")
    assert not (ws / ".cursor" / "rules" / "lethe-sync.mdc").exists()

    # First real run with_hooks=True configures hooks + non-destructive bounded rules + backups
    res1 = setup_all_tools(
        tools=["claude", "cursor", "antigravity", "codex"],
        db_path=str(ws / "lethe.db"),
        workspace_dir=ws,
        home_dir=tmp_path,
        with_hooks=True,
    )
    tools_configured = {item["tool"]: item for item in res1}
    assert {"claude_hooks", "claude_rules", "cursor_sync_rule", "antigravity_rules", "codex_rules"} <= set(tools_configured)

    assert "Always keep code concise." in gemini_md.read_text(encoding="utf-8")
    assert RULE_START_MARKER in gemini_md.read_text(encoding="utf-8")
    assert RULE_END_MARKER in gemini_md.read_text(encoding="utf-8")
    assert (gemini_dir / "GEMINI.md.bak").exists()

    assert "Prefer pytest." in claude_md.read_text(encoding="utf-8")
    assert (claude_dir / "CLAUDE.md.bak").exists()
    assert "Run lint before commit." in codex_md.read_text(encoding="utf-8")
    assert "lethe_context" in (ws / ".cursor" / "rules" / "lethe-sync.mdc").read_text(encoding="utf-8")

    # Second run is strictly idempotent with zero duplicated blocks
    assert install_antigravity_rules(gemini_dir=gemini_dir)["status"] == "already_configured"
    assert install_claude_memory_rules(claude_dir=claude_dir)["status"] == "already_configured"
    assert install_codex_rules(codex_dir=codex_dir)["status"] == "already_configured"
    assert gemini_md.read_text(encoding="utf-8").count(RULE_START_MARKER) == 1

    # Credentials expose Desktop system prompt snippet cleanly
    creds = build_mcp_credentials(db_path=str(ws / "lethe.db"), procedures_dir=str(ws / "procs"))
    assert "lethe_context" in creds["desktop_system_prompt"]
    assert get_desktop_system_prompt() in format_mcp_credentials(
        db_path=str(ws / "lethe.db"), procedures_dir=str(ws / "procs")
    )
