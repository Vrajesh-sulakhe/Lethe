"""Workspace capture configuration and the bounded Claude Code hook adapter."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

from lethe.platform.capabilities import CLAUDE_MIN_VERSION

CONFIG_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
INPUT_TIMEOUT = 5
WORKER_TIMEOUT = 10
HOOK_TIMEOUT = 20
HOOK_MODULE = "lethe.platform.hooks"


def workspace_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace_root must be an existing directory")
    return root


def config_path(root: Path) -> Path:
    return root / ".lethe" / "capture.json"


def read_config(root: Path) -> dict:
    path = config_path(root)
    if not path.exists():
        return {"version": CONFIG_VERSION, "state": "disabled", "mode": "automatic", "workspace_root": str(root)}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != CONFIG_VERSION:
        raise ValueError("Unsupported capture configuration version")
    if data.get("state") not in {"enabled", "paused", "disabled"} or data.get("mode") not in {"automatic", "strict"}:
        raise ValueError("Invalid capture state or mode")
    if Path(data.get("workspace_root", "")).resolve() != root:
        raise ValueError("Capture configuration belongs to a different workspace")
    return data


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".lethe-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def configure_capture(root: Path, action: str, *, mode: str = "automatic", db_path: str | None = None,
                      procedures_dir: str | None = None, workspace_id: str | None = None,
                      clients: list[str] | None = None, dry_run: bool = False) -> dict:
    from lethe.storage.db import get_current_workspace_id

    data = read_config(root)
    if action == "enable":
        if mode not in {"automatic", "strict"}:
            raise ValueError("Invalid capture mode")
        data.update(version=CONFIG_VERSION, state="enabled", mode=mode, workspace_root=str(root),
                    workspace_id=workspace_id or data.get("workspace_id") or get_current_workspace_id(root),
                    clients=clients or data.get("clients") or ["generic"],
                    artifacts_enabled=data.get("artifacts_enabled", True))
        for key, value in (("db_path", db_path), ("procedures_dir", procedures_dir)):
            if value:
                data[key] = str(Path(value).expanduser().resolve())
    elif action == "pause":
        if data["state"] == "disabled":
            raise ValueError("Enable capture before pausing it")
        data["state"] = "paused"
    elif action == "resume":
        if data["state"] == "disabled":
            raise ValueError("Enable capture before resuming it")
        data["state"] = "enabled"
    elif action == "disable":
        data["state"] = "disabled"
    else:
        raise ValueError("Unknown capture configuration action")
    if not dry_run:
        write_json(config_path(root), data)
    return {"status": "dry_run" if dry_run else data["state"], "path": str(config_path(root)), "config": data}


def read_event(stream, timeout: float = INPUT_TIMEOUT) -> dict:
    """Bound stdin size and wait time, including streams without a file descriptor."""
    result = queue.Queue(maxsize=1)

    def read():
        try:
            source = getattr(stream, "buffer", stream)
            try:
                fd = source.fileno()
            except (AttributeError, OSError):
                fd = None
            if fd is None:
                raw = source.read(MAX_INPUT_BYTES + 1)
            else:
                # os.read avoids a buffered-stdin lock at interpreter shutdown on timeout.
                raw = bytearray()
                while len(raw) <= MAX_INPUT_BYTES:
                    chunk = os.read(fd, min(65536, MAX_INPUT_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                raw = bytes(raw)
            result.put(raw)
        except Exception as exc:
            result.put(exc)

    threading.Thread(target=read, daemon=True).start()
    try:
        raw = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise ValueError("Capture stdin timed out") from exc
    if isinstance(raw, Exception):
        raise ValueError("Cannot read capture stdin") from raw
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, bytes) or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Capture input exceeds 1 MiB")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Capture input must be a JSON object")
    if "version" in data and (type(data["version"]) is not int or data["version"] != CONFIG_VERSION):
        raise ValueError("Unsupported capture event version")
    return data


def normalize_event(event: dict, root: Path, config: dict) -> dict:
    data = dict(event)
    if data.get("workspace_root") and Path(data["workspace_root"]).expanduser().resolve() != root:
        raise ValueError("Event workspace_root does not match configured root")
    for key in ("client", "session_id", "event_id"):
        if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 512:
            raise ValueError(f"Event requires a bounded non-empty {key}")
    if data.get("workspace_id") and data["workspace_id"] != config.get("workspace_id"):
        raise ValueError("Event workspace_id does not match configuration")
    data.update(workspace_root=str(root), workspace_id=config.get("workspace_id"))
    if data.get("kind") == "user_message":
        if not isinstance(data.get("message"), str) or not data["message"].strip():
            raise ValueError("user_message requires message")
    elif data.get("kind") in {"file_changed", "file_deleted"}:
        if not isinstance(data.get("path"), str) or not data["path"]:
            raise ValueError("File event requires path")
        path = Path(data["path"])
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("File event escapes workspace_root")
        data["path"] = str(path)
    else:
        raise ValueError("Unsupported capture event kind")
    return data


def claude_version() -> str | None:
    command = shutil.which("claude")
    if not command:
        return None
    try:
        result = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=5, check=True)
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def verify_claude_version(version: str | None = None) -> str:
    found = version or claude_version()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", found or "")
    if not match or tuple(map(int, match.groups())) < CLAUDE_MIN_VERSION:
        raise ValueError("Claude Code >= 2.1.314 is required for this verified exec-form capture adapter; version unavailable or too old")
    return found


def hook_handler(root: Path, mode: str, python_path: str | None = None) -> dict:
    # Exec form avoids shell interpolation on macOS, Git Bash and PowerShell.
    result = {"type": "command", "command": python_path or os.path.abspath(sys.executable),
              "args": ["-m", HOOK_MODULE, "--workspace-root", str(root), "--mode", mode],
              "timeout": HOOK_TIMEOUT}
    if mode == "strict":
        result["onFailure"] = "block"
    return result


def _owned_hook(handler: object, root: Path) -> bool:
    if not isinstance(handler, dict):
        return False
    args = handler.get("args")
    return (isinstance(args, list) and len(args) >= 4 and args[:3] == ["-m", HOOK_MODULE, "--workspace-root"]
            and args[3] == str(root))


def install_capture_hooks(root: Path, mode: str = "automatic", *, remove: bool = False,
                          dry_run: bool = False, python_path: str | None = None,
                          client_version: str | None = None) -> dict:
    version = None if remove else verify_claude_version(client_version)
    target = root / ".claude" / "settings.local.json"
    existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    if not isinstance(existing, dict) or not isinstance(existing.get("hooks", {}), dict):
        raise ValueError("Malformed Claude settings; refusing to overwrite")
    before = json.dumps(existing, sort_keys=True)
    hooks = existing.setdefault("hooks", {})
    for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError("Malformed Claude hook groups; refusing to overwrite")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("Malformed Claude hook handlers; refusing to overwrite")
            handlers = [h for h in group["hooks"] if not _owned_hook(h, root)]
            if handlers or not group["hooks"]:
                kept.append({**group, "hooks": handlers})
        if not remove:
            group = {"hooks": [hook_handler(root, mode, python_path)]}
            if event == "PostToolUse":
                group["matcher"] = "Write|Edit"
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        existing.pop("hooks", None)
    changed = before != json.dumps(existing, sort_keys=True)
    if changed and not dry_run:
        write_json(target, existing)
    return {"tool": "claude_capture_hooks", "path": str(target), "status": "dry_run" if dry_run else
            ("removed" if remove else "configured") if changed else "unchanged", "client_version": version,
            "runtime_verified": False}


def capture_hook_status(root: Path) -> dict:
    target = root / ".claude" / "settings.local.json"
    data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    installed = []
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Malformed Claude hooks configuration")
    for event in ("UserPromptSubmit", "PostToolUse", "Stop"):
        if any(_owned_hook(handler, root) for group in hooks.get(event, [])
               for handler in group.get("hooks", [])):
            installed.append(event)
    failures = list((root / ".lethe" / "capture-failures").glob("*/*.json"))
    return {"path": str(target), "installed_events": installed, "runtime_verified": False,
            "unresolved_failures": len(failures)}


def claude_event(payload: dict, root: Path) -> dict | None:
    kind = payload.get("hook_event_name")
    if kind not in {"UserPromptSubmit", "PostToolUse", "Stop"}:
        raise ValueError("Unsupported Claude hook event")
    if kind == "Stop":
        return None
    cwd = workspace_root(payload.get("cwd") or root)
    if not cwd.is_relative_to(root):
        raise ValueError("Claude cwd is outside configured workspace")
    event = {"version": CONFIG_VERSION, "client": "claude-code", "session_id": payload.get("session_id"),
             "event_id": payload.get("event_id") or payload.get("tool_use_id") or str(uuid.uuid4()),
             "workspace_root": str(root)}
    if kind == "UserPromptSubmit":
        event.update(kind="user_message", message=payload.get("prompt"))
    else:
        if payload.get("tool_name") not in {"Write", "Edit"}:
            return None
        response = payload.get("tool_response")
        if isinstance(response, dict) and (response.get("is_error") or response.get("error")):
            return None
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get("file_path"), str):
            raise ValueError("Write/Edit hook requires tool_input.file_path")
        path = Path(tool_input["file_path"])
        event.update(kind="file_changed", path=str(path if path.is_absolute() else cwd / path))
    return event


def _failure_path(root: Path, payload: dict) -> Path:
    session = hashlib.sha256(str(payload.get("session_id", "unknown")).encode()).hexdigest()
    identity = payload.get("tool_use_id") or payload.get("hook_event_name", "unknown")
    key = hashlib.sha256(str(identity).encode()).hexdigest()
    return root / ".lethe" / "capture-failures" / session / (key + ".json")


def capture_policy(root: Path, config: dict) -> dict:
    return {"enabled": config["state"] != "disabled", "paused": config["state"] == "paused",
            "artifacts_enabled": config.get("artifacts_enabled", False), "allowed_roots": [str(root)]}


def clear_capture_failure(root: Path, event: dict) -> None:
    payload = {"session_id": event["session_id"], "hook_event_name": "UserPromptSubmit"}
    if event["kind"] != "user_message":
        payload["tool_use_id"] = event["event_id"]
    _failure_path(root, payload).unlink(missing_ok=True)


def run_hook(root: Path, mode: str, payload: dict) -> int:
    strict = mode == "strict"
    failure = _failure_path(root, payload)
    try:
        if not config_path(root).exists():
            raise ValueError("Capture configuration is missing")
        config = read_config(root)
        strict = config["mode"] == "strict"
        if config["state"] != "enabled":
            return 0
        if payload.get("hook_event_name") == "Stop":
            if strict and any(failure.parent.glob("*.json")):
                print("Lethe capture has unresolved failures; retry ingestion or explicitly pause capture.", file=sys.stderr)
                return 0 if payload.get("stop_hook_active") is True else 2
            return 0
        event = claude_event(payload, root)
        if event is None:
            return 0
        event = normalize_event(event, root, config)
        result = subprocess.run(
            [sys.executable, "-m", "lethe.cli", "capture", "ingest", "--workspace-root", str(root)],
            input=json.dumps(event), capture_output=True, text=True, timeout=WORKER_TIMEOUT,
        )
        if result.returncode:
            raise ValueError("Capture worker failed; run lethe capture status for diagnostics")
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("status") in {"error", "failed", "disabled", "paused"} or response.get("ok") is not True:
            raise ValueError("Capture worker did not acknowledge ingestion")
        failure.unlink(missing_ok=True)
        return 0
    except Exception as exc:
        try:
            write_json(failure, {"status": "failed", "event": payload.get("hook_event_name"), "error_type": type(exc).__name__})
        except OSError:
            pass
        print(f"Lethe capture failed ({type(exc).__name__}); inspect configuration and retry ingestion.", file=sys.stderr)
        if payload.get("hook_event_name") == "Stop" and payload.get("stop_hook_active") is True:
            return 0
        return 2 if strict else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lethe Claude Code capture adapter v1")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--mode", choices=["automatic", "strict"], default="automatic")
    args = parser.parse_args(argv)
    strict = args.mode == "strict"
    try:
        root = workspace_root(args.workspace_root)
        if config_path(root).exists():
            strict = read_config(root)["mode"] == "strict"
        payload = read_event(sys.stdin)
    except Exception as exc:
        print(f"Lethe capture input failed ({type(exc).__name__}).", file=sys.stderr)
        return 2 if strict else 0
    return run_hook(root, args.mode, payload)


if __name__ == "__main__":
    sys.exit(main())
