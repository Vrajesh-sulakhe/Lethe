import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_default_python_executable() -> str:
    """Returns the absolute path to the active Python interpreter."""
    return str(Path(sys.executable).resolve())


def get_default_db_path(db_path: Optional[str] = None) -> str:
    """Returns the absolute path to the Lethe database."""
    target = Path(db_path) if db_path else Path.cwd() / "lethe.db"
    return str(target.resolve())


def generate_mcp_config(
    python_path: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates the standard MCP server specification for Lethe."""
    py = python_path or get_default_python_executable()
    db = db_path or get_default_db_path()

    return {
        "command": py,
        "args": ["-m", "lethe.cli", "mcp", "--db", db],
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
            home / ".gemini" / "antigravity" / "mcp" / "lethe.json",
            ws / ".agent" / "mcp.json",
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


def detect_installed_tools(workspace_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """
    Inspects candidate locations and returns status for each tool:
    - installed: True if target file exists or parent directory exists
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
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        servers = data.get("mcpServers", {})
                        if "lethe" in servers:
                            is_configured = True
                            break
                except Exception:
                    pass

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
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Safely merges the Lethe MCP server configuration into target JSON file.
    Non-destructive: preserves any other existing configured servers.
    """
    mcp_entry = generate_mcp_config(python_path=python_path, db_path=db_path)
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
    dry_run: bool = False,
    workspace_dir: Optional[Path] = None,
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
            dry_run=dry_run,
        )
        outcomes.append(outcome)

    return outcomes

