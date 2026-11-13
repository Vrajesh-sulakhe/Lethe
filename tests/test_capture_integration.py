import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lethe import cli
from lethe.platform import hooks, setup
from lethe.platform.capabilities import client_capability
from lethe.platform.client_rules import install_client_rules_for_tools


@pytest.fixture
def root(tmp_path):
    workspace = tmp_path / "project with 'quotes' $and spaces"
    workspace.mkdir()
    return workspace.resolve()


def enable(root, mode="automatic"):
    return hooks.configure_capture(root, "enable", mode=mode, db_path=str(root / "vault.db"),
                                   procedures_dir=str(root / "procedures"), workspace_id="project")


def prompt(root, **extra):
    return {"hook_event_name": "UserPromptSubmit", "session_id": "session-1", "cwd": str(root),
            "prompt": "We decided to use SQLite.", **extra}


def event(root, **extra):
    return {"version": 1, "client": "custom-desktop", "session_id": "session-1", "event_id": "event-1",
            "kind": "user_message", "workspace_root": str(root), "message": "We decided to use SQLite.", **extra}


def test_explicit_config_lifecycle(root):
    assert hooks.read_config(root)["state"] == "disabled"
    enable(root, "strict")
    data = hooks.read_config(root)
    assert data["version"] == 1 and data["mode"] == "strict"
    assert data["artifacts_enabled"] is True
    assert hooks.capture_policy(root, data)["allowed_roots"] == [str(root)]
    for action, state in (("pause", "paused"), ("resume", "enabled"), ("disable", "disabled")):
        hooks.configure_capture(root, action)
        assert hooks.read_config(root)["state"] == state
    with pytest.raises(ValueError, match="Enable"):
        hooks.configure_capture(root, "resume")


@pytest.mark.parametrize("data", [[], {"version": 2}, {"version": True}, {"version": 1, "state": "invalid"}])
def test_invalid_config_is_preserved(root, data):
    hooks.write_json(hooks.config_path(root), data)
    before = hooks.config_path(root).read_bytes()
    with pytest.raises(ValueError):
        enable(root)
    assert hooks.config_path(root).read_bytes() == before


@pytest.mark.parametrize("raw", ["[]", "not json", '{"version":2}', '{"version":true}', "x" * (hooks.MAX_INPUT_BYTES + 1)])
def test_input_is_versioned_and_bounded(raw):
    with pytest.raises(ValueError):
        hooks.read_event(io.StringIO(raw))


def test_input_timeout():
    import threading
    release = threading.Event()

    class Slow:
        def read(self, limit):
            release.wait(2)
            return "{}"

    try:
        with pytest.raises(ValueError, match="timed out"):
            hooks.read_event(Slow(), timeout=0.01)
    finally:
        release.set()


def test_utf8_input_limit_counts_bytes():
    raw = json.dumps({"message": "é" * hooks.MAX_INPUT_BYTES}, ensure_ascii=False)
    with pytest.raises(ValueError, match="1 MiB"):
        hooks.read_event(io.StringIO(raw))


@pytest.mark.parametrize("changes", [{"client": ""}, {"kind": "unknown"}, {"workspace_id": "other"},
                                     {"message": 42}, {"kind": "file_changed", "path": "../outside.py"}])
def test_event_validation(root, changes):
    enable(root)
    with pytest.raises(ValueError):
        hooks.normalize_event(event(root, **changes), root, hooks.read_config(root))


def test_symlink_escape_rejected(root):
    (root / "outside").symlink_to(root.parent, target_is_directory=True)
    enable(root)
    with pytest.raises(ValueError, match="escapes"):
        hooks.normalize_event(event(root, kind="file_changed", path="outside/secret"), root, hooks.read_config(root))


def test_mocked_claude_payloads(root):
    msg = hooks.claude_event(prompt(root), root)
    assert msg["kind"] == "user_message" and msg["client"] == "claude-code"
    payload = {"hook_event_name": "PostToolUse", "session_id": "session-1", "cwd": str(root),
               "tool_name": "Edit", "tool_use_id": "tool-123", "tool_input": {"file_path": "app.py"},
               "tool_response": {"success": True}}
    result = hooks.claude_event(payload, root)
    assert result["path"] == str(root / "app.py") and result["event_id"] == "tool-123"
    assert result["kind"] == "file_changed"
    assert hooks.claude_event({**payload, "tool_name": "Bash"}, root) is None
    assert hooks.claude_event({**payload, "tool_response": {"is_error": True}}, root) is None
    with pytest.raises(ValueError):
        hooks.claude_event({**payload, "tool_input": {}}, root)
    with pytest.raises(ValueError):
        hooks.claude_event(prompt(root, cwd=str(root.parent)), root)


@pytest.mark.parametrize("python_path,workspace", [
    ("/Applications/My Python's/bin/python", "/tmp/project 'quoted' $HOME"),
    (r"C:\Program Files\Python\python.exe", r"C:\Projects\name & spaces %HOME%"),
])
def test_exec_form_mac_windows_paths_are_literal(python_path, workspace):
    handler = hooks.hook_handler(Path(workspace), "strict", python_path)
    assert handler["command"] == python_path
    assert handler["args"] == ["-m", "lethe.platform.hooks", "--workspace-root", workspace, "--mode", "strict"]
    assert handler["onFailure"] == "block"
    assert handler["timeout"] > hooks.WORKER_TIMEOUT + hooks.INPUT_TIMEOUT
    assert "shell" not in handler


def test_install_remove_only_owned_hooks(root):
    target = root / ".claude" / "settings.local.json"
    user_hook = {"type": "command", "command": "echo lethe is not an ownership marker"}
    initial = {"permissions": {"allow": ["Read"]}, "hooks": {
        "UserPromptSubmit": [{"hooks": [user_hook]}],
        "Stop": [{"hooks": [{"type": "command", "command": "my-stop"}]}],
    }}
    hooks.write_json(target, initial)
    result = hooks.install_capture_hooks(root, "strict", client_version="2.1.314")
    assert result["status"] == "configured"
    first = target.read_bytes()
    assert hooks.install_capture_hooks(root, "strict", client_version="2.1.314")["status"] == "unchanged"
    assert target.read_bytes() == first
    installed = json.loads(first)
    assert installed["permissions"] == initial["permissions"]
    assert installed["hooks"]["PostToolUse"][-1]["matcher"] == "Write|Edit"
    assert "matcher" not in installed["hooks"]["UserPromptSubmit"][-1]
    own = installed["hooks"]["UserPromptSubmit"].pop()["hooks"][0]
    installed["hooks"]["UserPromptSubmit"][0]["hooks"].append(own)
    hooks.write_json(target, installed)
    hooks.install_capture_hooks(root, remove=True)
    assert json.loads(target.read_text()) == initial
    assert hooks.install_capture_hooks(root, remove=True)["status"] == "unchanged"


def test_dry_run_no_files_and_old_client_rejected(root):
    hooks.install_capture_hooks(root, dry_run=True, client_version="2.1.314")
    assert list(root.iterdir()) == []
    with pytest.raises(ValueError, match="2.1.314"):
        hooks.install_capture_hooks(root, client_version="2.1.100")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("data", [[], {"hooks": []}, {"hooks": {"Stop": {}}}, {"hooks": {"Stop": [42]}}])
def test_bad_settings_not_overwritten(root, data):
    target = root / ".claude" / "settings.local.json"
    hooks.write_json(target, data)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        hooks.install_capture_hooks(root, client_version="2.1.314")
    assert before == target.read_bytes()


@pytest.mark.parametrize("mode,expected", [("automatic", 0), ("strict", 2)])
def test_hook_failures_and_stop_loop_guard(root, monkeypatch, mode, expected):
    enable(root, mode)
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""))
    assert hooks.run_hook(root, mode, prompt(root)) == expected
    assert hooks.capture_hook_status(root)["unresolved_failures"] == 1
    stop = {"hook_event_name": "Stop", "session_id": "session-1"}
    assert hooks.run_hook(root, mode, stop) == expected
    assert hooks.run_hook(root, mode, {**stop, "stop_hook_active": True}) == 0
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"ok":true,"status":"stored"}'))
    assert hooks.run_hook(root, mode, prompt(root)) == 0
    assert hooks.capture_hook_status(root)["unresolved_failures"] == 0


def test_worker_timeout_strict_and_pause(root, monkeypatch):
    enable(root, "strict")

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == hooks.WORKER_TIMEOUT
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(hooks.subprocess, "run", timeout)
    assert hooks.run_hook(root, "strict", prompt(root)) == 2
    hooks.configure_capture(root, "pause")
    assert hooks.run_hook(root, "strict", prompt(root)) == 0


def test_missing_config_strict_blocks(root):
    assert hooks.run_hook(root, "strict", prompt(root)) == 2


@pytest.mark.parametrize("response", ["{}", "[]", '{"ok":false,"status":"rejected"}', "not json"])
def test_invalid_worker_ack_strict(root, monkeypatch, response):
    enable(root, "strict")
    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=response))
    assert hooks.run_hook(root, "strict", prompt(root)) == 2


def test_capabilities_are_not_installation_claims():
    for client in ("cursor", "gemini-cli", "codex"):
        info = client_capability(client)
        assert info["native_hooks_documented"] is True
        assert info["automatic"] is False and info["strict"] is False
    assert client_capability("chatgpt")["client"] != "codex"
    assert client_capability("gemini")["client"] == "gemini-cli"


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_desktop_aliases_have_no_fictional_config(root, monkeypatch, platform):
    monkeypatch.setattr(setup.sys, "platform", platform)
    targets = setup.get_known_tool_targets(workspace_dir=root, home_dir=root / "home")
    assert targets["chatgpt"] == targets["gemini-desktop"] == []
    assert targets["codex"] and targets["gemini-cli"]
    assert targets["gemini"] == targets["gemini-cli"]
    assert targets["codex-desktop"] == targets["codex"]
    assert not any("desktop" in p.name for p in targets["claude"])


def test_unsupported_setup_and_rules_do_not_write(root):
    results = setup.setup_all_tools(tools=["chatgpt", "gemini-desktop"], workspace_dir=root, home_dir=root / "home")
    assert [r["status"] for r in results] == ["unsupported", "unsupported"]
    assert install_client_rules_for_tools(["chatgpt", "openai", "gemini-desktop"], home_dir=root / "home") == []
    assert list(root.iterdir()) == []
    result = setup.configure_tool("chatgpt", root / "fictitious.json")
    assert result["status"] == "unsupported" and list(root.iterdir()) == []


def test_strict_unsupported_setup_fails_before_writes(root):
    with pytest.raises(ValueError, match="unsupported"):
        setup.setup_all_tools(tools=["claude", "cursor"], workspace_dir=root, home_dir=root / "home",
                              auto_capture=True, capture_mode="strict")
    assert list(root.iterdir()) == []


def test_auto_setup_local_hooks_and_contract(root, monkeypatch):
    monkeypatch.setattr(hooks, "claude_version", lambda: "2.1.314")
    results = setup.setup_all_tools(tools=["claude"], workspace_dir=root, home_dir=root / "home",
                                   db_path=str(root / "vault.db"), procedures_dir=str(root / "procs"),
                                   auto_capture=True, capture_mode="strict")
    assert hooks.read_config(root)["clients"] == ["claude-code"]
    assert len(hooks.capture_hook_status(root)["installed_events"]) == 3
    assert any(r["tool"] == "workspace_capture" for r in results)
    assert not (root / "home").exists()


def test_malformed_mcp_config_preserved(root):
    target = root / "mcp.json"
    target.write_text("not JSON")
    with pytest.raises(ValueError):
        setup.configure_tool("cursor", target)
    assert target.read_text() == "not JSON"


def test_creds_report_manual_desktops():
    result = setup.build_mcp_credentials(python_path=sys.executable)
    assert result["desktop_config_paths"]["openai_desktop"] is None
    assert result["desktop_config_paths"]["gemini_desktop"] is None
    assert "manual connector" in setup.format_mcp_credentials(python_path=sys.executable)


def test_capture_cli_delegates_policy_and_status(root, monkeypatch, capsys):
    from contextlib import contextmanager
    import lethe.core.ingestion as ingestion

    memory = SimpleNamespace()
    seen = {}

    @contextmanager
    def opened(args, **kwargs):
        seen["db"] = args.db
        yield memory

    def ingest(mem, data):
        seen["event"] = data
        seen["policy"] = mem.capture_config
        return {"ok": True, "status": "stored", "stored": 1}

    monkeypatch.setattr(cli, "_open_memory", opened)
    monkeypatch.setattr(ingestion, "ingest_event", ingest)
    monkeypatch.setattr(ingestion, "capture_status", lambda m, workspace_id=None: {"ok": True, "workspace_id": workspace_id})
    assert cli.main(["capture", "enable", "--workspace-root", str(root), "--db", str(root / "v.db")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "enabled"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event(root))))
    assert cli.main(["capture", "ingest"]) == 0
    assert json.loads(capsys.readouterr().out)["stored"] == 1
    assert seen["db"] == str(root / "v.db")
    assert seen["policy"]["artifacts_enabled"] and seen["policy"]["allowed_roots"] == [str(root)]
    assert seen["event"]["client"] == "custom-desktop"
    assert cli.main(["capture", "status", "--workspace-root", str(root)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["hooks"]["runtime_verified"] is False
    assert status["ingestion"]["workspace_id"] == root.name


def test_cli_disabled_and_bad_input_do_not_open_memory(root, monkeypatch, capsys):
    def unexpected(*a, **kw):
        raise AssertionError("must not open memory")

    monkeypatch.setattr(cli, "_open_memory", unexpected)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event(root))))
    cli.main(["capture", "ingest"])
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"
    monkeypatch.setattr(sys, "stdin", io.StringIO("bad json"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["capture", "ingest"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_cli_strict_unsupported_is_structured(root, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "--tool", "chatgpt", "--auto-capture", "--capture-mode", "strict",
                  "--workspace-root", str(root)])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
    assert list(root.iterdir()) == []


def test_legacy_hook_turn_still_fails_open(monkeypatch):
    def broken(args):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(cli, "_hook_turn", broken)
    assert cli.main(["hook", "turn", "--client", "claude"]) == 0


def test_capture_scan_cli_respects_enabled_state(root, capsys):
    assert cli.main(["capture", "scan", "--workspace-root", str(root)]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["status"] == "disabled" and disabled["completed"] is False

    (root / "notes.md").write_text("# Design\nUsing SQLite WAL storage.", encoding="utf-8")
    assert cli.main(["capture", "enable", "--workspace-root", str(root), "--db", str(root / "vault.db")]) == 0
    capsys.readouterr()

    assert cli.main(["capture", "scan", "--workspace-root", str(root), "--client", "antigravity"]) == 0
    scanned = json.loads(capsys.readouterr().out)
    assert scanned["ok"] is True and scanned["completed"] is True and scanned["changed"] == 1
