from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

RULE_START_MARKER = "<!-- LETHE_MEMORY_RULES:START -->"
RULE_END_MARKER = "<!-- LETHE_MEMORY_RULES:END -->"


def get_desktop_system_prompt() -> str:
    """Returns copy-pasteable custom instructions for Claude, Gemini, and ChatGPT Desktop."""
    return (
        "You have persistent cross-tool cognitive memory via the `lethe` MCP server. "
        "Never ask me to type `lethe_store`, `lethe_recall`, `lethe_branch`, `lethe_commit`, "
        "`lethe_rollback`, `lethe_inspect`, `lethe_forget`, or `lethe_procedures_list` manually.\n"
        "- At the start of every new chat—or before answering questions about who I am, "
        "'recent memories', preferences, what I was eating/doing, or past decisions—silently call `lethe_context` "
        "(or `lethe_recall`). Never say you don't know without querying Lethe first.\n"
        "- Whenever I introduce myself (name, handle, role), state preferences (favorite snacks, foods, drinks, setup), "
        "or share lasting technical/personal facts, SILENTLY CALL `lethe_store` immediately "
        "(category='user_identity' or 'preferences', scope='global'). "
        "DO NOT ASK PERMISSION or say 'let me know if you want me to log this'—store it silently in the background!"
    )


def get_autonomous_memory_markdown() -> str:
    """Returns the bounded autonomous Lethe protocol block for agent instruction files."""
    return (
        "## Lethe Autonomous Cross-Tool Memory\n\n"
        "Never wait for the user to manually type `lethe_store`, `lethe_recall`, `lethe_branch`, "
        "`lethe_commit`, `lethe_rollback`, `lethe_inspect`, `lethe_forget`, or `lethe_procedures_list`.\n\n"
        "1. **Silent Session Boot & Recall**: At the start of conversations—or before answering about "
        "user identity (`who am i`, `what is my name`), `recent memories`, preferences, what the user was eating or doing, "
        "or prior work—silently call `lethe_context` (single boot bundle) or `lethe_recall`. Never guess or say you don't know without checking.\n"
        "2. **Silent Proactive Capture (Zero Permission Gate)**: When the user shares their name/handle/bio "
        "(`category='user_identity'`, `scope='global'`), preferences, snacks, foods, setup (`category='preferences'`, `scope='global'`), "
        "or lasting architectural decisions (`category='architecture'`), silently call `lethe_store`. "
        "**CRITICAL**: NEVER ask the user 'would you like me to log/save this?'—silently execute `lethe_store` in the same turn alongside your normal conversational reply.\n"
        "3. **Safe Memory Git & Sync**: Check `lethe://sync/latest` and `lethe://handovers/active` when "
        "resuming cross-tool work; use `lethe_branch`, `lethe_commit`, and `lethe_rollback` to isolate or revert "
        "speculative memory changes."
    )


def _replace_or_append_marked_block(existing: str, marked_block: str) -> str:
    start_idx = existing.find(RULE_START_MARKER)
    end_idx = existing.find(RULE_END_MARKER)
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        after_end = end_idx + len(RULE_END_MARKER)
        return existing[:start_idx] + marked_block + existing[after_end:]

    stripped = existing.rstrip()
    if not stripped:
        return marked_block + "\n"
    return stripped + "\n\n" + marked_block + "\n"


def upsert_marked_rule_section(
    target_path: Path,
    body: str,
    tool_name: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Idempotently inserts or updates a marker-bounded Lethe rules section with .bak backup."""
    marked_block = f"{RULE_START_MARKER}\n{body.strip()}\n{RULE_END_MARKER}"
    existing = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    desired = _replace_or_append_marked_block(existing, marked_block) if existing else (marked_block + "\n")

    if target_path.exists() and existing == desired:
        return {"tool": tool_name, "path": str(target_path), "status": "already_configured"}

    if dry_run:
        return {"tool": tool_name, "path": str(target_path), "status": "dry_run"}

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        backup = Path(f"{target_path}.bak")
        backup.write_text(existing, encoding="utf-8")
    target_path.write_text(desired, encoding="utf-8")
    return {"tool": tool_name, "path": str(target_path), "status": "configured"}


def install_cursor_sync_rule(
    cursor_dir: Optional[Path | str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Writes an always-apply Cursor rule instructing Cursor to boot with Lethe context,
    silently store durable identity/architecture facts, and track reactive sync resources.
    """
    base_dir = Path(cursor_dir) if cursor_dir else (Path.home() / ".cursor")
    rule_path = base_dir / "rules" / "lethe-sync.mdc"
    desired = (
        "---\n"
        "description: Autonomous cross-tool memory boot, capture, and reactive sync with Lethe MCP\n"
        "alwaysApply: true\n"
        "---\n\n"
        "# Lethe Autonomous Cross-Tool Memory\n\n"
        "- **Never wait for manual commands**: Do not ask the user to type `lethe_store`, `lethe_recall`, "
        "`lethe_branch`, `lethe_commit`, `lethe_rollback`, `lethe_inspect`, or `lethe_forget`.\n"
        "- **Boot & Sync**: When starting a task or after cross-tool switches (Claude / Antigravity / Codex), "
        "call `lethe_context` (or `lethe_recall`) and read `lethe://sync/latest` and `lethe://handovers/active`.\n"
        "- **Silent Capture**: Silently call `lethe_store` for user identity (`category='user_identity'`, "
        "`scope='global'`), preferences (`category='preferences'`, `scope='global'`), and architectural decisions.\n"
        "- **Safe Isolation**: Use `lethe_branch`, `lethe_commit`, and `lethe_rollback` around speculative changes.\n"
    )

    if rule_path.exists() and rule_path.read_text(encoding="utf-8") == desired:
        status = "already_configured"
    elif dry_run:
        status = "dry_run"
    else:
        rule_path.parent.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(desired, encoding="utf-8")
        status = "configured"

    return {
        "tool": "cursor_sync_rule",
        "path": str(rule_path),
        "status": status,
    }


def install_antigravity_rules(
    gemini_dir: Optional[Path | str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Installs autonomous Lethe memory rules into Antigravity / Gemini (~/.gemini/GEMINI.md)."""
    base_dir = Path(gemini_dir) if gemini_dir else (Path.home() / ".gemini")
    return upsert_marked_rule_section(
        target_path=base_dir / "GEMINI.md",
        body=get_autonomous_memory_markdown(),
        tool_name="antigravity_rules",
        dry_run=dry_run,
    )


def install_claude_memory_rules(
    claude_dir: Optional[Path | str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Installs autonomous Lethe memory rules into Claude Code (~/.claude/CLAUDE.md)."""
    base_dir = Path(claude_dir) if claude_dir else (Path.home() / ".claude")
    return upsert_marked_rule_section(
        target_path=base_dir / "CLAUDE.md",
        body=get_autonomous_memory_markdown(),
        tool_name="claude_rules",
        dry_run=dry_run,
    )


def install_codex_rules(
    codex_dir: Optional[Path | str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Installs autonomous Lethe memory rules into Codex CLI (~/.codex/AGENTS.md)."""
    base_dir = Path(codex_dir) if codex_dir else (Path.home() / ".codex")
    return upsert_marked_rule_section(
        target_path=base_dir / "AGENTS.md",
        body=get_autonomous_memory_markdown(),
        tool_name="codex_rules",
        dry_run=dry_run,
    )


def install_client_rules_for_tools(
    target_tools: Iterable[str],
    workspace_dir: Optional[Path] = None,
    home_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Installs autonomous Lethe memory instruction rules for all requested clients."""
    home = home_dir or Path.home()
    outcomes: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()

    def _record(outcome: Dict[str, Any]) -> None:
        key = str(Path(outcome["path"]).resolve())
        if key not in seen_paths:
            seen_paths.add(key)
            outcomes.append(outcome)

    tools_set = set(target_tools)
    if "claude" in tools_set:
        _record(install_claude_memory_rules(claude_dir=home / ".claude", dry_run=dry_run))
    if "cursor" in tools_set:
        cur_dir = (workspace_dir / ".cursor") if workspace_dir else (home / ".cursor")
        _record(install_cursor_sync_rule(cursor_dir=cur_dir, dry_run=dry_run))
    if tools_set & {"antigravity", "gemini"}:
        _record(install_antigravity_rules(gemini_dir=home / ".gemini", dry_run=dry_run))
    if tools_set & {"codex", "openai", "chatgpt"}:
        _record(install_codex_rules(codex_dir=home / ".codex", dry_run=dry_run))

    return outcomes
