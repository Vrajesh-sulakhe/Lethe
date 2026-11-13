import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_default_home_dir() -> Path:
    """Returns the shared Lethe home directory (~/.lethe)."""
    return Path.home() / ".lethe"


def get_default_python_executable() -> str:
    """Returns the absolute path to the active Python interpreter without breaking venv symlinks."""
    return os.path.abspath(sys.executable)


def get_default_lethe_executable() -> Optional[str]:
    """Returns the absolute path to the globally installed `lethe` CLI if available on PATH."""
    found = shutil.which("lethe")
    if found:
        return os.path.abspath(found)
    return None


def get_default_db_path(db_path: Optional[str] = None) -> str:
    """Returns the absolute path to the Lethe database (defaults to ~/.lethe/lethe.db or $LETHE_DB)."""
    raw = db_path or os.environ.get("LETHE_DB")
    target = Path(os.path.expanduser(raw)) if raw else get_default_home_dir() / "lethe.db"
    return os.path.abspath(str(target))


def get_default_procedures_dir(procedures_dir: Optional[str] = None) -> str:
    """Returns the absolute path to the Lethe procedures directory (defaults to ~/.lethe/procedures)."""
    raw = procedures_dir or os.environ.get("LETHE_PROCEDURES_DIR")
    target = Path(os.path.expanduser(raw)) if raw else get_default_home_dir() / "procedures"
    return os.path.abspath(str(target))


def get_wrapper_script_path(workspace_dir: Optional[Path] = None) -> Optional[str]:
    """Returns the absolute path to lethe.sh (macOS/Linux) or lethe.cmd (Windows) if present."""
    repo_root = (workspace_dir or Path(__file__).resolve().parents[2]).resolve()
    script_name = "lethe.cmd" if sys.platform == "win32" else "lethe.sh"
    candidate = repo_root / script_name
    if candidate.exists():
        return os.path.abspath(str(candidate))
    return None


def generate_mcp_config(
    python_path: Optional[str] = None,
    db_path: Optional[str] = None,
    procedures_dir: Optional[str] = None,
    use_wrapper: bool = False,
    lethe_cmd: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates the standard MCP server specification for Lethe."""
    db = get_default_db_path(db_path)
    wrapper = get_wrapper_script_path() if use_wrapper else None
    global_cli: Optional[str] = None
    if not python_path and not wrapper:
        global_cli = (lethe_cmd or None) if lethe_cmd is not None else get_default_lethe_executable()

    if python_path:
        cmd = python_path
        args = ["-m", "lethe.cli", "mcp", "--db", db]
    elif wrapper:
        cmd = wrapper
        args = ["mcp", "--db", db]
    elif global_cli:
        cmd = global_cli
        args = ["mcp", "--db", db]
    else:
        cmd = get_default_python_executable()
        args = ["-m", "lethe.cli", "mcp", "--db", db]

    if procedures_dir:
        args.extend(["--procedures-dir", get_default_procedures_dir(procedures_dir)])

    return {
        "command": cmd,
        "args": args,
        "env": {
            "PYTHONUNBUFFERED": "1",
        },
    }


def get_known_tool_targets(workspace_dir: Optional[Path] = None) -> Dict[str, List[Path]]:
    """
    Returns candidate configuration file paths for known AI coding tools.
    Supports both global user configurations and project-local workspace configs.
    """
    home = Path.home()
    ws = (workspace_dir or Path.cwd()).resolve()

    targets: Dict[str, List[Path]] = {
        "cursor": [
            ws / ".cursor" / "mcp.json",
            home / ".cursor" / "mcp.json",
        ],
        "claude": [
            home / ".claude.json",
            home / ".claude" / "mcp.json",
            ws / ".mcp.json",
        ],
        "antigravity": [
            home / ".gemini" / "antigravity" / "mcp_config.json",
            ws / ".agent" / "mcp.json",
        ],
        "codex": [
            home / ".codex" / "config.toml",
            ws / ".codex" / "config.toml",
        ],
        "vscode": [
            ws / ".vscode" / "mcp.json",
        ],
    }

    # Add OS-specific VS Code Cline / Roo Code settings path
    if sys.platform == "darwin":
        cline_path = (
            home
            / "Library"
            / "Application Support"
            / "Code"
            / "User"
            / "globalStorage"
            / "saoudrizwan.claude-dev"
            / "settings"
            / "cline_mcp_settings.json"
        )
        targets["vscode"].append(cline_path)
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            cline_path = (
                Path(appdata)
                / "Code"
                / "User"
                / "globalStorage"
                / "saoudrizwan.claude-dev"
                / "settings"
                / "cline_mcp_settings.json"
            )
            targets["vscode"].append(cline_path)

    return targets


def _is_lethe_configured(path: Path) -> bool:
    """Checks whether an existing JSON or TOML MCP config already defines 'lethe'."""
    try:
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".toml":
            data = tomllib.loads(raw)
            return "lethe" in data.get("mcp_servers", {})
        data = json.loads(raw)
        return "lethe" in data.get("mcpServers", {})
    except Exception:
        return False


def _merge_toml_mcp_server(
    existing_text: str,
    server_name: str,
    mcp_entry: Dict[str, Any],
) -> str:
    """
    Non-destructively inserts or replaces [mcp_servers.<server_name>] in a TOML file,
    preserving comments and unrelated sections, and validates via tomllib before returning.
    """
    if existing_text.strip():
        tomllib.loads(existing_text)

    table_header_re = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
    target_prefix = f"mcp_servers.{server_name}"

    kept_lines: List[str] = []
    skipping_target = False
    for line in existing_text.splitlines(keepends=True):
        m = table_header_re.match(line)
        if m:
            section = m.group(1).strip()
            skipping_target = section == target_prefix or section.startswith(f"{target_prefix}.")
        if not skipping_target:
            kept_lines.append(line)

    base = "".join(kept_lines).rstrip("\n")
    env_lines = "\n".join(
        f"{k} = {json.dumps(str(v))}" for k, v in (mcp_entry.get("env") or {}).items()
    )
    block = (
        f"[mcp_servers.{server_name}]\n"
        f"command = {json.dumps(mcp_entry['command'])}\n"
        f"args = {json.dumps(mcp_entry['args'])}\n"
    )
    if env_lines:
        block += f"\n[mcp_servers.{server_name}.env]\n{env_lines}\n"

    updated = f"{base}\n\n{block}" if base else block
    tomllib.loads(updated)
    return updated


def detect_installed_tools(workspace_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """
    Inspects candidate locations and returns status for each tool:
    - exists: True if target config file exists on disk
    - path: Best active path to write configuration
    - is_configured: True if 'lethe' server entry is already present
    """
    ws = (workspace_dir or Path.cwd()).resolve()
    home = Path.home().resolve()
    targets = get_known_tool_targets(workspace_dir)
    results: Dict[str, Dict[str, Any]] = {}

    for tool_name, paths in targets.items():
        found_path: Optional[Path] = None
        is_configured = False

        # Prefer an explicit workspace tool directory (e.g. <ws>/.cursor/) if present
        for p in paths:
            resolved_parent = p.parent.resolve()
            if (
                resolved_parent != ws
                and resolved_parent != home
                and ws in p.resolve().parents
                and p.parent.exists()
            ):
                found_path = p
                if p.exists():
                    is_configured = _is_lethe_configured(p)
                break

        # Next check for existing config files in priority order
        if not found_path:
            for p in paths:
                if p.exists():
                    found_path = p
                    if _is_lethe_configured(p):
                        is_configured = True
                        break

        # If file doesn't exist yet, pick the first valid parent directory or default path
        if not found_path:
            for p in paths:
                if p.parent.exists():
                    found_path = p
                    break
            if not found_path and paths:
                found_path = paths[0]

        results[tool_name] = {
            "name": tool_name,
            "path": found_path,
            "exists": found_path.exists() if found_path else False,
            "is_configured": is_configured,
        }

    return results


def configure_tool(
    tool_name: str,
    target_path: Path,
    python_path: Optional[str] = None,
    db_path: Optional[str] = None,
    procedures_dir: Optional[str] = None,
    dry_run: bool = False,
    use_wrapper: bool = False,
) -> Dict[str, Any]:
    """
    Safely merges the Lethe MCP server configuration into target JSON or TOML file.
    Non-destructive: preserves any other existing configured servers.
    """
    mcp_entry = generate_mcp_config(
        python_path=python_path,
        db_path=db_path,
        procedures_dir=procedures_dir,
        use_wrapper=use_wrapper,
    )

    if target_path.suffix.lower() == ".toml":
        existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
        updated_text = _merge_toml_mcp_server(existing_text, "lethe", mcp_entry)
        if not dry_run:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(updated_text)
    else:
        existing_data: Dict[str, Any] = {}

        if target_path.exists():
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except Exception:
                existing_data = {}

        if not isinstance(existing_data, dict):
            existing_data = {}

        if "mcpServers" not in existing_data:
            existing_data["mcpServers"] = {}

        existing_data["mcpServers"]["lethe"] = mcp_entry

        if not dry_run:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, indent=2)
                f.write("\n")

    return {
        "tool": tool_name,
        "path": str(target_path),
        "status": "dry_run" if dry_run else "configured",
        "mcp_config": mcp_entry,
    }


def setup_all_tools(
    tools: Optional[List[str]] = None,
    python_path: Optional[str] = None,
    db_path: Optional[str] = None,
    procedures_dir: Optional[str] = None,
    dry_run: bool = False,
    workspace_dir: Optional[Path] = None,
    use_wrapper: bool = False,
) -> List[Dict[str, Any]]:
    """
    Discovers and configures all requested or detected tools.
    """
    detected = detect_installed_tools(workspace_dir)
    target_tools = tools or list(detected.keys())
    outcomes: List[Dict[str, Any]] = []

    for tool in target_tools:
        if tool not in detected:
            continue

        info = detected[tool]
        path = info["path"]
        if not path:
            continue

        outcome = configure_tool(
            tool_name=tool,
            target_path=path,
            python_path=python_path,
            db_path=db_path,
            procedures_dir=procedures_dir,
            dry_run=dry_run,
            use_wrapper=use_wrapper,
        )
        outcomes.append(outcome)

    return outcomes


def _shell_join(parts: List[str]) -> str:
    """Quotes command parts cleanly for terminal copy-paste."""
    if hasattr(shlex, "join"):
        return shlex.join(parts)
    return " ".join(shlex.quote(p) for p in parts)


def build_mcp_credentials(
    python_path: Optional[str] = None,
    db_path: Optional[str] = None,
    procedures_dir: Optional[str] = None,
    use_wrapper: bool = False,
    lethe_cmd: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generates ready-to-paste MCP connection credentials across JSON (Cursor/Antigravity/Claude),
    TOML (Codex), and one-line CLI commands for the shared Lethe memory bus.
    """
    resolved_db = get_default_db_path(db_path)
    resolved_procs = get_default_procedures_dir(procedures_dir)
    entry = generate_mcp_config(
        python_path=python_path,
        db_path=resolved_db,
        procedures_dir=resolved_procs,
        use_wrapper=use_wrapper,
        lethe_cmd=lethe_cmd,
    )

    json_snippet = json.dumps({"mcpServers": {"lethe": entry}}, indent=2)
    toml_snippet = _merge_toml_mcp_server("", "lethe", entry).strip()
    launch_cmd = _shell_join([entry["command"], *entry["args"]])

    return {
        "db_path": resolved_db,
        "procedures_dir": resolved_procs,
        "mcp_entry": entry,
        "json_snippet": json_snippet,
        "toml_snippet": toml_snippet,
        "claude_cli": f"claude mcp add -s user lethe -- {launch_cmd}",
        "codex_cli": f"codex mcp add lethe -- {launch_cmd}",
    }


def format_mcp_credentials(
    python_path: Optional[str] = None,
    db_path: Optional[str] = None,
    procedures_dir: Optional[str] = None,
    use_wrapper: bool = False,
    lethe_cmd: Optional[str] = None,
) -> str:
    """Formats MCP credentials in human-readable copy-pasteable sections."""
    creds = build_mcp_credentials(
        python_path=python_path,
        db_path=db_path,
        procedures_dir=procedures_dir,
        use_wrapper=use_wrapper,
        lethe_cmd=lethe_cmd,
    )
    lines = [
        "=" * 80,
        " LETHE MCP CONNECTION CREDENTIALS (UNIVERSAL MEMORY BUS)",
        "=" * 80,
        f" Shared Database (--db):        {creds['db_path']}",
        f" Shared Runbooks (--procedures): {creds['procedures_dir']}",
        f" Command:                       {creds['mcp_entry']['command']}",
        "",
        " [1] Cursor / Antigravity / Claude Code JSON",
        "     Cursor:      Settings -> Tools & MCP -> Add new global MCP server",
        "     Antigravity: Manage MCP Servers -> View raw config",
        "-" * 80,
        creds["json_snippet"],
        "",
        " [2] Codex CLI / Desktop TOML (~/.codex/config.toml)",
        "-" * 80,
        creds["toml_snippet"],
        "",
        " [3] One-Line Terminal Commands",
        "-" * 80,
        f" Claude Code: {creds['claude_cli']}",
        f" Codex:       {creds['codex_cli']}",
        "",
        " Or auto-wire all detected tools at once:",
        "   lethe setup",
        "=" * 80,
    ]
    return "\n".join(lines)


