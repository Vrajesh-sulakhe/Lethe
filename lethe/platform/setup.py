import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

from lethe.platform.client_rules import (
    get_desktop_system_prompt,
    install_antigravity_rules,
    install_claude_memory_rules,
    install_client_rules_for_tools,
    install_codex_rules,
    install_cursor_sync_rule,
)


def get_default_home_dir() -> Path:
    """Returns the shared Lethe home directory (~/.lethe)."""
    return Path.home() / ".lethe"


def get_default_python_executable() -> str:
    """Returns the absolute path to the active Python interpreter without breaking venv symlinks."""
    return os.path.abspath(sys.executable)


def get_default_lethe_executable() -> Optional[str]:
    """
    Returns the absolute path to an installed `lethe` executable.
    On Windows, restricts PATH lookup to `.exe` binaries so `.cmd`/`.bat` or
    extensionless Git Bash shims never trigger Node/Electron spawn EINVAL errors.
    """
    if sys.platform == "win32":
        for name in ("lethe.exe", "lethe"):
            found = shutil.which(name)
            if found and Path(found).suffix.lower() == ".exe":
                return os.path.abspath(found)
    else:
        found = shutil.which("lethe")
        if found and Path(found).suffix.lower() not in {".cmd", ".bat", ".sh"}:
            return os.path.abspath(found)

    sibling_name = "lethe.exe" if sys.platform == "win32" else "lethe"
    sibling = Path(sys.executable).parent / sibling_name
    if sibling.exists() and sibling.is_file():
        return os.path.abspath(str(sibling))
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
    if not python_path and not wrapper and sys.platform != "win32":
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
            "PYTHONIOENCODING": "utf-8",
        },
    }


DEFAULT_SETUP_TOOLS: tuple[str, ...] = (
    "cursor",
    "claude",
    "claude-desktop",
    "antigravity",
    "gemini-cli",
    "codex",
    "vscode",
)

TOOL_ALIASES: Dict[str, str] = {
    "claude-code": "claude",
    "gemini": "gemini-cli",
    "openai": "openai-desktop",
    "chatgpt": "openai-desktop",
    "chatgpt-desktop": "openai-desktop",
    "codex-desktop": "codex",
}


def get_known_tool_targets(
    workspace_dir: Optional[Path] = None,
    home_dir: Optional[Path] = None,
) -> Dict[str, List[Path]]:
    """
    Returns candidate configuration file paths for known AI coding and desktop tools.
    Supports both global user configurations and project-local workspace configs.
    """
    home = (home_dir or Path.home()).resolve()
    ws = (workspace_dir or Path.cwd()).resolve()

    targets: Dict[str, List[Path]] = {
        "cursor": [
            ws / ".cursor" / "mcp.json",
            home / ".cursor" / "mcp.json",
        ],
        "claude": [
            ws / ".mcp.json",
            home / ".claude.json",
        ],
        "claude-desktop": [],
        "antigravity": [
            home / ".gemini" / "antigravity" / "mcp_config.json",
            home / ".gemini" / "config" / "mcp_config.json",
            ws / ".agent" / "mcp.json",
        ],
        "gemini-desktop": [],
        "gemini-cli": [ws / ".gemini" / "settings.json", home / ".gemini" / "settings.json"],
        "codex": [
            home / ".codex" / "config.toml",
            ws / ".codex" / "config.toml",
        ],
        "openai-desktop": [],
        "vscode": [
            ws / ".vscode" / "mcp.json",
        ],
    }

    # Add OS-specific Desktop app paths (Claude Desktop, Gemini Desktop, OpenAI / ChatGPT Desktop, VS Code Cline)
    if sys.platform == "darwin":
        app_support = home / "Library" / "Application Support"

        claude_desktop_mac = app_support / "Claude" / "claude_desktop_config.json"
        targets["claude-desktop"].append(claude_desktop_mac)


        cline_path = (
            app_support
            / "Code"
            / "User"
            / "globalStorage"
            / "saoudrizwan.claude-dev"
            / "settings"
            / "cline_mcp_settings.json"
        )
        targets["vscode"].append(cline_path)
    elif sys.platform == "win32":
        use_env = home_dir is None
        appdata = os.environ.get("APPDATA") if use_env else None
        localappdata = os.environ.get("LOCALAPPDATA") if use_env else None
        roaming = Path(appdata) if appdata else (home / "AppData" / "Roaming")
        local = Path(localappdata) if localappdata else (home / "AppData" / "Local")

        claude_desktop_win = roaming / "Claude" / "claude_desktop_config.json"
        targets["claude-desktop"].append(claude_desktop_win)

        msix_desktop = (
            local
            / "Packages"
            / "Claude_pzs8sxrjxfjjc"
            / "LocalCache"
            / "Roaming"
            / "Claude"
            / "claude_desktop_config.json"
        )
        targets["claude-desktop"].append(msix_desktop)


        cline_path = (
            roaming
            / "Code"
            / "User"
            / "globalStorage"
            / "saoudrizwan.claude-dev"
            / "settings"
            / "cline_mcp_settings.json"
        )
        targets["vscode"].append(cline_path)
    else:
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME") if home_dir is None else None
        cfg_root = Path(xdg_cfg) if xdg_cfg else (home / ".config")

        claude_desktop_linux = cfg_root / "Claude" / "claude_desktop_config.json"
        targets["claude-desktop"].append(claude_desktop_linux)


    # Register convenient aliases pointing to the exact same candidate lists
    for alias, canonical in TOOL_ALIASES.items():
        targets[alias] = list(targets[canonical])

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


def _safe_exists(p: Optional[Path]) -> bool:
    if p is None:
        return False
    try:
        return p.exists()
    except (OSError, PermissionError):
        return False


def detect_installed_tools(
    workspace_dir: Optional[Path] = None,
    home_dir: Optional[Path] = None,
    include_aliases: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """
    Inspects candidate locations and returns status for each tool:
    - exists: True if target config file exists on disk
    - path: Best active path to write configuration
    - is_configured: True if 'lethe' server entry is already present
    """
    ws = (workspace_dir or Path.cwd()).resolve()
    home = (home_dir or Path.home()).resolve()
    targets = get_known_tool_targets(workspace_dir=ws, home_dir=home)
    results: Dict[str, Dict[str, Any]] = {}

    for tool_name, paths in targets.items():
        if not include_aliases and tool_name in TOOL_ALIASES:
            continue

        found_path: Optional[Path] = None
        is_configured = False

        # Prefer an explicit workspace tool directory (e.g. <ws>/.cursor/) if present
        for p in paths:
            try:
                resolved_parent = p.parent.resolve()
                if (
                    resolved_parent != ws
                    and resolved_parent != home
                    and ws in p.resolve().parents
                    and _safe_exists(p.parent)
                ):
                    found_path = p
                    if _safe_exists(p):
                        is_configured = _is_lethe_configured(p)
                    break
            except (OSError, PermissionError):
                continue

        # Next check for existing config files in priority order (preferring already-configured files)
        if not found_path:
            existing_paths = [p for p in paths if _safe_exists(p)]
            for p in existing_paths:
                if _is_lethe_configured(p):
                    found_path = p
                    is_configured = True
                    break
            if not found_path and existing_paths:
                found_path = existing_paths[0]

        # If file doesn't exist yet, pick the first valid parent directory or default path
        if not found_path:
            for p in paths:
                if _safe_exists(p.parent):
                    found_path = p
                    break
            if not found_path and paths:
                found_path = paths[0]

        results[tool_name] = {
            "name": tool_name,
            "path": found_path,
            "exists": _safe_exists(found_path),
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
    canonical = TOOL_ALIASES.get(tool_name, tool_name)
    if canonical in {"openai-desktop", "gemini-desktop"}:
        return {"tool": tool_name, "path": None, "status": "unsupported",
                "reason": "No verified native local MCP configuration; use the client's supported connector UI or manual instructions."}
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
            with open(target_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        if not isinstance(existing_data, dict):
            raise ValueError(f"Expected JSON object in {target_path}; refusing to overwrite.")

        server_key = "mcpServers"
        if not isinstance(existing_data.get(server_key, {}), dict):
            raise ValueError(f"Expected {server_key} object in {target_path}; refusing to overwrite.")
        existing_data.setdefault(server_key, {})["lethe"] = mcp_entry

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
    with_hooks: bool = False,
    with_rules: bool = False,
    home_dir: Optional[Path] = None,
    auto_capture: bool = False,
    capture_mode: str = "automatic",
) -> List[Dict[str, Any]]:
    """Configure supported clients; native capture is an explicit workspace opt-in."""
    from lethe.platform.capabilities import client_capability
    from lethe.platform.hooks import configure_capture, install_capture_hooks, workspace_root

    target_tools = tools if tools is not None else list(DEFAULT_SETUP_TOOLS)
    capture_root = None
    capture_plan = None
    if auto_capture:
        if workspace_dir is None:
            raise ValueError("--auto-capture requires an explicit workspace root")
        capture_root = workspace_root(workspace_dir)
        unsupported = [t for t in target_tools if not client_capability(t)["automatic"]]
        if capture_mode == "strict" and unsupported:
            raise ValueError("Strict native capture unsupported for: " + ", ".join(unsupported))
        if any(client_capability(t)["automatic"] for t in target_tools):
            try:
                capture_plan = install_capture_hooks(capture_root, capture_mode, dry_run=True, python_path=python_path)
            except ValueError:
                if capture_mode == "strict":
                    raise
        # Validate the config before changing any client settings.
        if capture_plan:
            configure_capture(capture_root, "enable", mode=capture_mode, dry_run=True)

    detected = detect_installed_tools(
        workspace_dir=workspace_dir,
        home_dir=home_dir,
        include_aliases=True,
    )
    target_tools = tools if tools is not None else list(DEFAULT_SETUP_TOOLS)
    outcomes: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()

    for tool in target_tools:
        lookup = tool if tool in detected else TOOL_ALIASES.get(tool, tool)
        if lookup not in detected:
            outcomes.append({"tool": tool, "path": None, "status": "unsupported", "reason": "Unknown client"})
            continue

        info = detected[lookup]
        path = info["path"]
        if not path:
            outcomes.append({"tool": tool, "path": None, "status": "unsupported",
                             "reason": "No verified native local configuration; manual connector setup only"})
            continue

        resolved_key = str(Path(path).resolve())
        if tools is None and resolved_key in seen_paths:
            continue
        seen_paths.add(resolved_key)

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

    if auto_capture:
        for tool in target_tools:
            capability = client_capability(tool)
            if not capability["automatic"] or not capture_plan:
                outcomes.append({"tool": tool + "_capture", "path": None, "status": "manual",
                                 "capability": capability,
                                 "reason": "No verified adapter/version available; use the generic capture ingest interface"})
        if capture_plan:
            outcomes.append(install_capture_hooks(capture_root, capture_mode, dry_run=dry_run,
                                                  python_path=python_path, client_version=capture_plan["client_version"]))
            outcome = configure_capture(capture_root, "enable", mode=capture_mode,
                                        db_path=get_default_db_path(db_path),
                                        procedures_dir=get_default_procedures_dir(procedures_dir),
                                        clients=["claude-code"], dry_run=dry_run)
            outcomes.append({"tool": "workspace_capture", **outcome})

    if with_hooks and any(TOOL_ALIASES.get(t, t) == "claude" for t in target_tools):
        settings_file = (home_dir or Path.home()) / ".claude" / "settings.json"
        outcomes.append(
            install_claude_hooks(
                settings_path=settings_file,
                db_path=db_path,
                procedures_dir=procedures_dir,
                dry_run=dry_run,
            )
        )

    if with_hooks or with_rules:
        outcomes.extend(
            install_client_rules_for_tools(
                target_tools=target_tools,
                workspace_dir=workspace_dir,
                home_dir=home_dir,
                dry_run=dry_run,
            )
        )

    return outcomes


def _shell_join(parts: List[str]) -> str:
    """Quotes command parts cleanly for the active OS shell."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(parts)
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
    Generates ready-to-paste MCP connection credentials across JSON (Claude Desktop,
    Gemini Desktop, OpenAI / ChatGPT Desktop, Cursor, Antigravity, Claude Code),
    TOML (Codex CLI & Desktop), and one-line CLI commands for the shared Lethe memory bus.
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
    targets = get_known_tool_targets()

    return {
        "db_path": resolved_db,
        "procedures_dir": resolved_procs,
        "mcp_entry": entry,
        "json_snippet": json_snippet,
        "toml_snippet": toml_snippet,
        "claude_cli": f"claude mcp add -s user lethe -- {launch_cmd}",
        "gemini_cli": f"gemini mcp add lethe {launch_cmd}",
        "codex_cli": f"codex mcp add lethe -- {launch_cmd}",
        "desktop_config_paths": {
            "claude_desktop": str(targets["claude-desktop"][0]),
            "gemini_desktop": None,
            "openai_desktop": None,
        },
        "manual_clients": ["gemini-desktop", "chatgpt"],
        "desktop_system_prompt": get_desktop_system_prompt(),
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
    dpaths = creds["desktop_config_paths"]
    lines = [
        "=" * 80,
        " LETHE MCP CONNECTION CREDENTIALS (UNIVERSAL MEMORY BUS)",
        "=" * 80,
        f" Shared Database (--db):        {creds['db_path']}",
        f" Shared Runbooks (--procedures): {creds['procedures_dir']}",
        f" Command:                       {creds['mcp_entry']['command']}",
        "",
        " [1] Supported MCP JSON clients (Claude Desktop / Gemini CLI / Cursor / Antigravity)",
        f"     Claude Desktop:  {dpaths['claude_desktop']}",
        "     Gemini CLI:      ~/.gemini/settings.json (not Gemini Desktop)",
        "     ChatGPT/Gemini Desktop: manual connector setup; no verified native local config",
        "     Cursor:          Settings -> Tools & MCP -> Add new global MCP server",
        "     Antigravity:     Manage MCP Servers -> View raw config",
        "-" * 80,
        creds["json_snippet"],
        "",
        " [2] OpenAI Codex CLI / Desktop TOML (~/.codex/config.toml)",
        "-" * 80,
        creds["toml_snippet"],
        "",
        " [3] One-Line Terminal Commands",
        "-" * 80,
        f" Claude Code: {creds['claude_cli']}",
        f" Gemini CLI:  {creds['gemini_cli']}",
        f" Codex:       {creds['codex_cli']}",
        "",
        " Or auto-wire all detected CLI & Desktop tools at once:",
        "   lethe setup",
        "   lethe setup --tool claude-desktop",
        "   lethe setup --tool gemini-cli",
        "   lethe setup --tool claude --auto-capture --workspace-root /path/to/project",
        "   lethe setup --with-hooks    # legacy Claude hooks + best-effort client rules",
        "",
        " [4] Autonomous Desktop Custom Instructions (Claude / Gemini / ChatGPT Projects)",
        "-" * 80,
        creds["desktop_system_prompt"],
        "=" * 80,
    ]
    return "\n".join(lines)


def get_default_bin_dir(bin_dir: Optional[Path | str] = None) -> Path:
    """Returns the user-level bin directory (~/.local/bin) for global CLI launchers."""
    target = Path(os.path.expanduser(str(bin_dir))) if bin_dir else Path.home() / ".local" / "bin"
    return Path(os.path.abspath(str(target)))


def _normalize_path_entry(entry: str) -> str:
    cleaned = os.path.expanduser(entry.strip().strip('"'))
    norm = os.path.normpath(cleaned)
    return norm.lower() if sys.platform == "win32" else norm


def _is_dir_on_path(target_dir: Path, path_env: Optional[str] = None) -> bool:
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    if not raw:
        return False
    target_norm = _normalize_path_entry(str(target_dir))
    return any(_normalize_path_entry(p) == target_norm for p in raw.split(os.pathsep) if p.strip())


def _merge_path_entries(existing_path: str, target_dir: Path, sep: str = ";") -> tuple[str, bool]:
    """Appends target_dir to a PATH string only if an equivalent entry is not present."""
    parts = [p for p in existing_path.split(sep) if p.strip()]
    target_norm = _normalize_path_entry(str(target_dir))
    if any(_normalize_path_entry(p) == target_norm for p in parts):
        return existing_path, False
    updated = sep.join([*parts, str(target_dir)]) if parts else str(target_dir)
    return updated, True


def _ensure_windows_user_path(target_dir: Path) -> bool:
    """Adds target_dir to Windows HKCU\\Environment\\Path and notifies the shell."""
    if sys.platform != "win32":
        return False

    import ctypes
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_READ | winreg.KEY_SET_VALUE,
    ) as key:
        try:
            current_val, reg_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_val, reg_type = "", winreg.REG_EXPAND_SZ

        updated_val, changed = _merge_path_entries(str(current_val or ""), target_dir, sep=";")
        if not changed:
            return False

        if reg_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            reg_type = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, "Path", 0, reg_type, updated_val)

    try:
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    except Exception:
        pass
    return True


def _build_windows_cmd_shim(target_exe: Optional[Path], python_exe: str, module_name: str) -> str:
    # Always invoke python.exe -m <module> to avoid Windows Device Guard / AppLocker blocking venv executables
    launch = f'"{python_exe}" -m {module_name}'
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "PYTHONUNBUFFERED=1"\r\n'
        'set "PYTHONIOENCODING=utf-8"\r\n'
        f"{launch} %*\r\n"
        "exit /b %ERRORLEVEL%\r\n"
    )


def _build_posix_sh_shim(target_bin: Optional[Path], python_exe: str, module_name: str) -> str:
    if target_bin and target_bin.exists():
        exec_line = f'exec "{target_bin.as_posix()}" "$@"'
    else:
        exec_line = f'exec "{Path(python_exe).as_posix()}" -m {module_name} "$@"'
    return (
        "#!/usr/bin/env sh\n"
        "export PYTHONUNBUFFERED=1\n"
        "export PYTHONIOENCODING=utf-8\n"
        f"{exec_line}\n"
    )


def install_global_cli(
    bin_dir: Optional[Path | str] = None,
    update_path: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Installs global `lethe` and `lethe-mcp` launchers into ~/.local/bin (or bin_dir)
    and ensures Windows User PATH includes that directory so bare `lethe` prints
    the full CLI menu from any terminal folder.
    """
    target_bin = get_default_bin_dir(bin_dir)
    python_exe = get_default_python_executable()
    scripts_dir = Path(python_exe).parent
    commands = [
        ("lethe", "lethe.cli"),
        ("lethe-mcp", "lethe.mcp.server"),
    ]

    installed: List[Dict[str, str]] = []
    if not dry_run:
        target_bin.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        for cmd_name, module_name in commands:
            exe_candidate = scripts_dir / f"{cmd_name}.exe"
            exe_target = exe_candidate if exe_candidate.exists() else None

            cmd_path = target_bin / f"{cmd_name}.cmd"
            sh_path = target_bin / cmd_name
            if not dry_run:
                cmd_path.write_text(
                    _build_windows_cmd_shim(exe_target, python_exe, module_name),
                    encoding="utf-8",
                    newline="",
                )
                # Extensionless POSIX shim so Git Bash on Windows also supports bare `lethe`
                sh_path.write_text(
                    _build_posix_sh_shim(exe_target, python_exe, module_name),
                    encoding="utf-8",
                    newline="\n",
                )
            installed.append({"command": cmd_name, "path": str(cmd_path), "mode": "win_cmd_and_sh"})

        path_updated = _ensure_windows_user_path(target_bin) if (update_path and not dry_run) else False
        on_path = path_updated or _is_dir_on_path(target_bin)
    else:
        for cmd_name, module_name in commands:
            source_bin = scripts_dir / cmd_name
            dest_path = target_bin / cmd_name
            same_location = (
                source_bin.exists()
                and source_bin.parent.resolve() == target_bin.resolve()
            )
            if same_location:
                installed.append({"command": cmd_name, "path": str(dest_path), "mode": "already_in_bin"})
                continue

            if not dry_run:
                if dest_path.exists() or dest_path.is_symlink():
                    dest_path.unlink()
                if source_bin.exists():
                    dest_path.symlink_to(source_bin)
                    mode = "symlink"
                else:
                    dest_path.write_text(
                        _build_posix_sh_shim(None, python_exe, module_name),
                        encoding="utf-8",
                    )
                    dest_path.chmod(0o755)
                    mode = "sh_wrapper"
            else:
                mode = "symlink" if source_bin.exists() else "sh_wrapper"

            installed.append({"command": cmd_name, "path": str(dest_path), "mode": mode})

        path_updated = False
        on_path = _is_dir_on_path(target_bin)

    return {
        "platform": sys.platform,
        "bin_dir": str(target_bin),
        "python_executable": python_exe,
        "installed": installed,
        "path_updated": path_updated,
        "on_path": on_path,
        "dry_run": dry_run,
    }


def _build_hook_command(
    action: str,
    client_id: str,
    db_path: Optional[str] = None,
    procedures_dir: Optional[str] = None,
    max_tokens: int = 600,
) -> str:
    global_cli = get_default_lethe_executable()
    parts: List[str] = (
        [global_cli] if global_cli else [get_default_python_executable(), "-m", "lethe.cli"]
    )
    if db_path:
        parts.extend(["--db", get_default_db_path(db_path)])
    if procedures_dir:
        parts.extend(["--procedures-dir", get_default_procedures_dir(procedures_dir)])
    parts.extend(["hook", action, "--client", client_id, "--max-tokens", str(max_tokens)])
    return _shell_join(parts)


def _is_lethe_hook_cmd(cmd: str) -> bool:
    return "lethe" in cmd and ("hook turn" in cmd or "hook boot" in cmd or "hook inject" in cmd)


def install_claude_hooks(
    settings_path: Optional[Path | str] = None,
    db_path: Optional[str] = None,
    procedures_dir: Optional[str] = None,
    max_tokens: int = 600,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Idempotently installs Claude Code SessionStart (`hook boot`) & UserPromptSubmit
    (`hook turn`) hooks for zero-prompt auto-capture + slice recall, with .bak backup safety.
    """
    target = Path(settings_path) if settings_path else (Path.home() / ".claude" / "settings.json")
    event_commands = {
        "SessionStart": _build_hook_command(
            "boot", client_id="claude", db_path=db_path, procedures_dir=procedures_dir, max_tokens=max(max_tokens, 600),
        ),
        "UserPromptSubmit": _build_hook_command(
            "turn", client_id="claude", db_path=db_path, procedures_dir=procedures_dir, max_tokens=max_tokens,
        ),
    }

    existing: Dict[str, Any] = {}
    if target.exists():
        raw_text = target.read_text(encoding="utf-8")
        if raw_text.strip():
            try:
                loaded = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed JSON in {target}; refusing to overwrite hooks.") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"Expected JSON object in {target}; refusing to overwrite hooks.")
            existing = loaded

    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        existing["hooks"] = hooks

    changed = False
    for event_name, desired_cmd in event_commands.items():
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            entries = []
            hooks[event_name] = entries

        found_current = False
        upgraded = False
        for item in entries:
            if not isinstance(item, dict):
                continue
            for h in (item.get("hooks") or []):
                if not isinstance(h, dict):
                    continue
                cmd = str(h.get("command", ""))
                if cmd == desired_cmd:
                    found_current = True
                elif _is_lethe_hook_cmd(cmd):
                    h["command"] = desired_cmd
                    found_current = True
                    upgraded = True
        if upgraded:
            changed = True
        elif not found_current:
            entries.append({
                "matcher": "*",
                "hooks": [{"type": "command", "command": desired_cmd}],
            })
            changed = True

    if changed and not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = Path(f"{target}.bak")
            backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        target.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    return {
        "tool": "claude_hooks",
        "path": str(target),
        "status": ("dry_run" if dry_run else ("configured" if changed else "already_configured")),
        "command": event_commands["UserPromptSubmit"],
    }




