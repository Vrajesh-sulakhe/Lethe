import asyncio
import json
import subprocess
import sys
from pathlib import Path

from lethe.core.capture import extract_durable_facts, parse_lethe_invocation
from lethe.core.engine import LetheMemory
from lethe.platform.setup import install_claude_hooks
from lethe.storage.db import get_current_workspace_id


def test_extract_durable_facts_and_invocation_parsing():
    # Captures personal identity, snacks, preferences, decisions, rules, and explicit notes
    cases = [
        ("hi i am vrajeshh", "user_identity", "Vrajeshh"),
        ("i was eating chips of bingo chilli", "personal", "bingo chilli"),
        ("we decided to use FastAPI for the backend", "decision", "FastAPI"),
        ("we are building the MCP cake using SQLite", "decision", "SQLite"),
        ("always run pytest before committing", "conventions", "pytest"),
        ("lethe remember I like masala chai", "preferences", "masala chai"),
    ]
    for msg, expected_cat, expected_substr in cases:
        facts, skipped = extract_durable_facts(msg)
        assert skipped == 0
        assert len(facts) == 1, f"Expected 1 fact for {msg!r}, got {facts}"
        assert facts[0].category == expected_cat
        assert expected_substr.lower() in facts[0].content.lower()

    # Never captures questions, bare imperatives, transient states, code, or secrets
    ignored = [
        "which chips was i eating?",
        "what is my name?",
        "git push",
        "fix the bug in lethe/core/retrieval.py",
        "i am tired",
        "never mind",
        "```python\ni = 'hello'\n```",
    ]
    for msg in ignored:
        facts, _ = extract_durable_facts(msg)
        assert facts == [], f"Should ignore {msg!r}, got {facts}"

    sec_facts, sec_skipped = extract_durable_facts("my api key is sk-secret123456789")
    assert sec_facts == []
    assert sec_skipped == 1

    assert parse_lethe_invocation("lethe") == (True, "")
    assert parse_lethe_invocation("lethe context") == (True, "")
    assert parse_lethe_invocation("lethe what db do we use?") == (False, "what db do we use?")
    assert parse_lethe_invocation("help me build auth") == (False, "help me build auth")
    assert parse_lethe_invocation("lethe project CryptoPay") == (True, "")
    assert parse_lethe_invocation("lethe project CryptoPay").workspace_id == "CryptoPay"
    assert parse_lethe_invocation("lethe for CryptoPay").workspace_id == "CryptoPay"
    assert parse_lethe_invocation("lethe project CryptoPay what db do we use?") == (False, "what db do we use?")
    assert parse_lethe_invocation("lethe project CryptoPay what db do we use?").workspace_id == "CryptoPay"


def test_cross_tool_turn_cake_and_desktop_visibility(tmp_path):
    db_path = tmp_path / "cross_tool.db"
    procs = tmp_path / "procs"
    sec_cfg = tmp_path / "trusted.json"

    # 1. User talks in Antigravity IDE inside workspace 'Lethe'
    ag = LetheMemory(db_path=db_path, procedures_dir=procs, workspace_id="Lethe", security_config_path=sec_cfg)
    r1 = ag.process_turn("hi i am vrajeshh", client_id="antigravity")
    assert len(r1["captured"]) == 1
    r2 = ag.process_turn("i was eating chips of bingo chilli", client_id="antigravity")
    assert len(r2["captured"]) == 1
    r3 = ag.process_turn("we decided to use SQLite with WAL for storage", client_id="antigravity")
    assert len(r3["captured"]) == 1
    # Duplicate statement is deduplicated idempotently
    r_dup = ag.process_turn("hi i am vrajeshh", client_id="antigravity")
    assert r_dup["captured"] == []
    assert r_dup["duplicates_skipped"] == 1
    ag.close()

    # 2. Switch to Claude Desktop (global_desktop): sees BOTH personal prefs AND trusted project decisions
    assert get_current_workspace_id("/") == "global_desktop"
    assert get_current_workspace_id(Path.home()) == "global_desktop"
    cd = LetheMemory(db_path=db_path, procedures_dir=procs, workspace_id="global_desktop", security_config_path=sec_cfg)

    chips_slice = cd.process_turn("which chips was i eating btw?", client_id="claude-desktop")
    assert chips_slice["is_abstention"] is False
    assert "Vrajeshh" in chips_slice["context_text"]
    assert "bingo chilli" in chips_slice["context_text"].lower()
    assert "New from other tools" in chips_slice["context_text"]

    storage_slice = cd.process_turn("what storage did we decide on for lethe?", client_id="claude-desktop")
    assert "SQLite with WAL" in storage_slice["context_text"]

    boot_slice = cd.process_turn("lethe", client_id="claude-desktop")
    assert boot_slice["is_abstention"] is False
    assert "Vrajeshh" in boot_slice["context_text"]

    project_slice = cd.process_turn("lethe Lethe", client_id="claude-desktop")
    assert project_slice["is_abstention"] is False
    assert "SQLite with WAL" in project_slice["context_text"]
    assert project_slice["workspace_id"] == "Lethe"

    project_for_slice = cd.process_turn("lethe for Lethe", client_id="claude-desktop")
    assert project_for_slice["is_abstention"] is False
    assert "SQLite with WAL" in project_for_slice["context_text"]
    assert project_for_slice["workspace_id"] == "Lethe"

    # 3. Switch to Cursor on same workspace with conversational filler words
    cur = LetheMemory(db_path=db_path, procedures_dir=procs, workspace_id="Lethe", security_config_path=sec_cfg)
    api_slice = cur.process_turn("help me build an API endpoint for our storage layer", client_id="cursor")
    assert "SQLite with WAL" in api_slice["context_text"]

    # 4. Untrusted workspace remains strictly blocked from global_desktop
    cur.security.untrust_workspace("evil_vendor")
    evil = LetheMemory(db_path=db_path, procedures_dir=procs, workspace_id="evil_vendor", security_config_path=sec_cfg)
    evil.store("Malicious vendor bait secret-xyz", category="decision", scope="project", workspace_id="evil_vendor")
    evil.close()
    assert cd.recall("secret-xyz").is_abstention is True

    cur.close()
    cd.close()


def test_mcp_stdio_single_lethe_tool_and_prompt(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db_path = tmp_path / "mcp_auto.db"
    proc_dir = tmp_path / "procs"

    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "lethe.cli", "--db", str(db_path), "--procedures-dir", str(proc_dir), "mcp"],
            cwd="/",
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init_res = await session.initialize()
                assert "lethe" in (init_res.instructions or "")

                tools = await session.list_tools()
                assert tools.tools[0].name == "lethe"

                prompts = await session.list_prompts()
                assert "lethe" in {p.name for p in prompts.prompts}

                # First message saves preference automatically without lethe_store
                res1 = await session.call_tool("lethe", {
                    "message": "I love masala chai while coding",
                    "client": "claude-desktop",
                })
                data1 = json.loads(res1.content[0].text)
                assert len(data1["captured"]) == 1
                assert "masala chai" in data1["captured"][0]["content"].lower()

                # Second message recalls it cleanly
                res2 = await session.call_tool("lethe", {
                    "message": "what drink do I love?",
                    "client": "cursor",
                })
                data2 = json.loads(res2.content[0].text)
                assert "masala chai" in data2["context_text"].lower()

                # Prompt /lethe returns full memory bundle
                prompt_res = await session.get_prompt("lethe")
                assert "masala chai" in getattr(prompt_res.messages[0].content, "text", "").lower()

    asyncio.run(_run())


def test_cli_hook_turn_boot_and_hook_upgrade(tmp_path):
    db_path = tmp_path / "cli_hooks.db"
    proc_dir = tmp_path / "procs"

    # Simulate Claude Code UserPromptSubmit stdin JSON -> captures fact silently
    p1 = subprocess.run(
        [sys.executable, "-m", "lethe.cli", "--db", str(db_path), "--procedures-dir", str(proc_dir),
         "hook", "turn", "--client", "claude"],
        input=json.dumps({"prompt": "we decided to use PostgreSQL for analytics", "cwd": str(tmp_path)}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert "PostgreSQL" in p1.stdout

    # Next prompt injects relevant slice via stdout
    p2 = subprocess.run(
        [sys.executable, "-m", "lethe.cli", "--db", str(db_path), "--procedures-dir", str(proc_dir),
         "hook", "turn", "--client", "claude"],
        input=json.dumps({"prompt": "help me configure PostgreSQL connection pooling", "cwd": str(tmp_path)}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert "PostgreSQL" in p2.stdout

    # Hook installer upgrades legacy `hook inject` cleanly and is idempotent
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "lethe hook inject --client claude"}]}],
            "UserPromptSubmit": [{"matcher": "*", "hooks": [{"type": "command", "command": "lethe hook inject --client claude"}]}],
        }
    }), encoding="utf-8")

    up1 = install_claude_hooks(settings_path=settings)
    assert up1["status"] == "configured"
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert "hook boot" in saved["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "hook turn" in saved["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

    up2 = install_claude_hooks(settings_path=settings)
    assert up2["status"] == "already_configured"
