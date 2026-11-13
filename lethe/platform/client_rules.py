from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

RULE_START_MARKER = "<!-- LETHE_MEMORY_RULES:START -->"
RULE_END_MARKER = "<!-- LETHE_MEMORY_RULES:END -->"


def get_desktop_system_prompt() -> str:
    """Returns copy-pasteable custom instructions for Claude, Gemini, and ChatGPT Desktop."""
    return (
        "These instructions are best effort, not a native capture guarantee; they apply only when a supported Lethe MCP connection is available.\n"
        "You have persistent cross-tool memory via the `lethe` MCP server shared across my connected apps and IDEs.\n"
        "- ON EVERY MESSAGE: Silently call the `lethe` tool FIRST with my exact message as `message` (and your app name as `client`, e.g. `claude-desktop`).\n"
        "- In that single call, Lethe serves the relevant slice of memory (who I am, my snacks/preferences, active project decisions, cross-tool updates) AND automatically saves any durable facts I mentioned.\n"
        "- Use the returned memory silently as background truth. NEVER ask 'do you want me to save this?' or wait for commands.\n"
        "- If I just type `lethe`, pass it straight to `lethe(message='lethe')` (or `lethe_context`) to load my full memory. "
        "(`lethe_store`, `lethe_recall`, and `lethe_forget` remain available for explicit requests.)"
    )


def get_autonomous_memory_markdown() -> str:
    """Returns the bounded autonomous Lethe protocol block for agent instruction files."""
    return (
        "## Lethe Autonomous Cross-Tool Memory\n\n"
        "Instruction rules are best effort; native capture requires a separately installed, supported adapter.\n\n"
        "**THE ONE RULE — Call `lethe` FIRST on every user message**:\n"
        "Before replying to ANY user message, call `lethe(message=<user's message verbatim>, client=<your tool name>)`.\n\n"
        "1. **Automatic Slice Serving**: In one call, `lethe` returns only the relevant slice of memory (identity, preferences, snacks/foods, project decisions, active handovers, and cross-tool deltas). Use it silently as background truth; never say 'I don't know' without checking.\n"
        "2. **Automatic Zero-Permission Capture**: `lethe` deterministically extracts and saves durable facts from the user's message server-side. **CRITICAL**: NEVER ask the user 'would you like me to log/save this?' and never ask them to type `lethe_store` or `lethe_recall`.\n"
        "3. **Manual Trigger (`lethe`)**: If the user types `lethe` (or `lethe <question>`), pass it straight to `lethe(message=...)` (or `lethe_context` / `lethe_recall`) to load full cross-tool context.\n"
        "4. **Advanced Memory Git & Unlearning**: Use `lethe_branch`, `lethe_commit`, and `lethe_rollback` around speculative changes; use `lethe_forget` only when asked to delete a memory; keep `lethe_store` for long explicit notes (`category='architecture'` or `'preferences'`)."
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
        "Instruction rules are best effort and do not guarantee automatic capture.\n\n"
        "- **Call `lethe` FIRST on every message**: Pass the user's message verbatim as `message` and `client='cursor'` to get the relevant memory slice AND auto-save durable facts.\n"
        "- **Never wait for manual commands or ask permission**: Do not ask to save preferences or ask the user to type `lethe_store` or `lethe_recall`.\n"
        "- **Full Context / Sync**: When the user types `lethe` or starts a cross-tool task, pass it through (or call `lethe_context`) and check `lethe://sync/latest` and `lethe://handovers/active`.\n"
        "- **Safe Isolation**: Use `lethe_branch`, `lethe_commit`, and `lethe_rollback` around speculative changes; use `lethe_store` only for explicit long architectural notes.\n"
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
    if tools_set & {"claude", "claude-code"}:
        _record(install_claude_memory_rules(claude_dir=home / ".claude", dry_run=dry_run))
    if "cursor" in tools_set:
        cur_dir = (workspace_dir / ".cursor") if workspace_dir else (home / ".cursor")
        _record(install_cursor_sync_rule(cursor_dir=cur_dir, dry_run=dry_run))
    if tools_set & {"antigravity", "gemini", "gemini-cli"}:
        _record(install_antigravity_rules(gemini_dir=home / ".gemini", dry_run=dry_run))
    if tools_set & {"codex", "codex-desktop"}:
        _record(install_codex_rules(codex_dir=home / ".codex", dry_run=dry_run))

    return outcomes
