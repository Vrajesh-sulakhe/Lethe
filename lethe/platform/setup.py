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
            "PYTHONIOENCODING": "utf-8",
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
    if target_exe and target_exe.exists():
        launch = f'"{target_exe}"'
    else:
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



