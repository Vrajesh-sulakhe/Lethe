import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_default_python_executable() -> str:
    """Returns the absolute path to the active Python interpreter without breaking venv symlinks."""
    return os.path.abspath(sys.executable)


def get_default_db_path(db_path: Optional[str] = None) -> str:
    """Returns the absolute path to the Lethe database."""
    target = Path(os.path.expanduser(db_path)) if db_path else Path.cwd() / "lethe.db"
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
) -> Dict[str, Any]:
    """Generates the standard MCP server specification for Lethe."""
    db = get_default_db_path(db_path) if db_path else get_default_db_path()
    wrapper = get_wrapper_script_path() if use_wrapper else None

    if wrapper and not python_path:
        cmd = wrapper
        args = ["mcp", "--db", db]
    else:
        cmd = python_path or get_default_python_executable()
        args = ["-m", "lethe.cli", "mcp", "--db", db]

    if procedures_dir:
        args.extend(["--procedures-dir", os.path.abspath(os.path.expanduser(procedures_dir))])

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
    targets = get_known_tool_targets(workspace_dir)
    results: Dict[str, Dict[str, Any]] = {}

    for tool_name, paths in targets.items():
        found_path: Optional[Path] = None
        is_configured = False

        # First check for existing files
        for p in paths:
            if p.exists():
                found_path = p
                if _is_lethe_configured(p):
                    is_configured = True
                    break

        # If file doesn't exist, pick the first valid parent directory or default workspace path
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

