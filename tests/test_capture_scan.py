import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from threading import Event

import pytest

from lethe.core.engine import LetheMemory
from lethe.models import MemoryStatus
from lethe.platform import capture_scan


@pytest.fixture
def memory(tmp_path):
    instance = LetheMemory(db_path=tmp_path / "vault.db", procedures_dir=tmp_path / "procedures",
                           workspace_id="global_desktop", security_config_path=tmp_path / "trusted.json")
    yield instance
    instance.close()


@pytest.fixture
def root(tmp_path, memory):
    folder = tmp_path / "project"
    folder.mkdir()
    memory.capture_config = {"artifacts_enabled": True, "allowed_roots": [str(folder)]}
    return folder


def sources(memory):
    return [dict(row) for row in memory.db.conn.execute("SELECT * FROM capture_scan_sources")]


def test_capture_html_markdown_text_and_no_unchanged_flood(memory, root):
    files = {"index.html": "<title>Orchid landing page</title><h1>Garden design</h1>",
             "README.md": "# Project architecture\nA local garden planner.",
             "notes.txt": "The interface uses a green palette."}
    for path, text in files.items():
        (root / path).write_text(text)
    first = capture_scan.scan_once(memory, root, client="antigravity")
    assert first["completed"] and first["ok"], first
    assert first["changed"] == 3
    assert len(first["receipts"]) == 3
    assert first["workspace_id"] == "project"
    assert all(receipt["status"] == "stored" for receipt in first["receipts"])
    for receipt in first["receipts"]:
        node = memory.db.get_memory(receipt["memory_ids"][0])
        assert node.workspace_id == "project"
        assert node.metadata["capture_client"] == "antigravity"
    second = capture_scan.scan_once(memory, root, client="cursor")
    assert second["completed"] and second["unchanged"] == 3
    assert second["receipts"] == []
    assert memory.db.conn.execute("SELECT COUNT(*) FROM ingestion_receipts").fetchone()[0] == 3
    rows = sources(memory)
    assert set(rows[0]) == {"workspace_root", "workspace_id", "branch", "path", "content_hash"}
    assert "green palette" not in json.dumps(rows)
    assert {row["content_hash"] for row in rows} == {hashlib.sha256(value.encode()).hexdigest() for value in files.values()}


def test_content_edit_and_deterministic_replay_after_tracking_loss(memory, root, monkeypatch):
    path = root / "notes.md"
    path.write_text("An orchid garden planner.")
    events = []
    original = capture_scan.ingest_event

    def record(instance, event):
        events.append(event)
        return original(instance, event)

    monkeypatch.setattr(capture_scan, "ingest_event", record)
    capture_scan.scan_once(memory, root)
    memory.db.conn.execute("DELETE FROM capture_scan_sources")
    memory.db.conn.commit()
    replay = capture_scan.scan_once(memory, root)
    assert events[0] == events[1]
    assert replay["receipts"][0]["replayed"]
    path.write_text("An orchid garden planner with a seasonal calendar.")
    changed = capture_scan.scan_once(memory, root)
    assert changed["changed"] == 1
    assert events[2]["event_id"] != events[1]["event_id"]
    assert events[2]["session_id"] == events[1]["session_id"]


def test_deleted_file_and_removed_directory_reconciliation(memory, root):
    folder = root / "docs"
    folder.mkdir()
    (folder / "plan.md").write_text("Orchid garden architecture.")
    (root / "notes.txt").write_text("A second garden document.")
    first = capture_scan.scan_once(memory, root)
    shutil.rmtree(folder)
    (root / "notes.txt").unlink()
    deleted = capture_scan.scan_once(memory, root)
    assert deleted["completed"] and deleted["deleted"] == 2, deleted
    assert all(result["status"] == "deleted" for result in deleted["receipts"])
    assert not sources(memory)
    assert capture_scan.scan_once(memory, root)["receipts"] == []
    for result in first["receipts"]:
        assert memory.db.get_memory(result["memory_ids"][0]).status == MemoryStatus.TOMBSTONED


@pytest.mark.parametrize("physical", [False, True])
def test_forgotten_material_not_recreated_after_edit_or_tracking_loss(memory, root, physical):
    path = root / "notes.md"
    path.write_text("A garden plan with orchids.")
    first = capture_scan.scan_once(memory, root)
    memory_id = first["receipts"][0]["memory_ids"][0]
    if physical:
        memory.db.delete_memory_physical(memory_id)
    else:
        memory.forget(memory_id)
    assert capture_scan.scan_once(memory, root)["receipts"] == []
    memory.db.conn.execute("DELETE FROM capture_scan_sources")
    memory.db.conn.commit()
    path.write_text("A revised garden plan with orchids and roses.")
    replay = capture_scan.scan_once(memory, root, client="codex")
    assert replay["receipts"][0]["status"] == "suppressed", replay
    assert replay["receipts"][0]["memory_ids"] == []


def test_recreated_deleted_path_is_suppressed(memory, root):
    path = root / "notes.md"
    path.write_text("Original project overview.")
    capture_scan.scan_once(memory, root)
    path.unlink()
    assert capture_scan.scan_once(memory, root)["deleted"] == 1
    path.write_text("A new overview at a previously deleted source.")
    result = capture_scan.scan_once(memory, root)
    assert result["receipts"][0]["status"] == "suppressed"


def test_exclusions_size_secrets_binary_and_links(memory, root, tmp_path):
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for name in (".git", ".lethe", ".venv", "node_modules", "venv", "dist", "build", "vendor"):
        folder = root / name
        folder.mkdir(exist_ok=True)
        (folder / "notes.md").write_text("Must not capture this generated document.")
    (root / "secret.md").write_text("Sensitive filename.")
    (root / "credentials.txt").write_text("Sensitive credentials filename.")
    (root / "binary.txt").write_bytes(b"binary\0data")
    (root / "large.md").write_bytes(b"a" * 257)
    (root / "code.py").write_text("Not a supported artifact.")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text("Outside project scope.")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "linked.md").symlink_to(outside / "notes.md")
    (root / "valid.md").write_text("Valid project architecture.")
    result = capture_scan.scan_once(memory, root, max_file_bytes=256)
    assert result["completed"] and result["changed"] == 1, result
    assert [row["path"] for row in sources(memory)] == ["valid.md"]


def test_gitignore_nested_negation_and_tracked_ignored_files(memory, root):
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text("ignored.md\nblocked/\n")
    (root / "ignored.md").write_text("An ignored document.")
    subprocess.run(["git", "-C", str(root), "add", "-f", "ignored.md"], check=True, capture_output=True)
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "inside.md").write_text("An ignored directory document.")
    docs = root / "docs"
    docs.mkdir()
    (docs / ".gitignore").write_text("*.md\n!keep.md\n")
    (docs / "skip.md").write_text("An ignored nested document.")
    (docs / "keep.md").write_text("A public nested document.")
    result = capture_scan.scan_once(memory, root)
    assert result["completed"] and result["changed"] == 1, result
    assert [row["path"] for row in sources(memory)] == ["docs/keep.md"]


def test_ignore_check_failure_fails_closed(memory, root, monkeypatch):
    (root / "notes.md").write_text("A public project plan.")

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr("lethe.core.artifacts.subprocess.run", unavailable)
    result = capture_scan.scan_once(memory, root)
    assert result["reason"] == "ignore_check_failed"
    assert not result["completed"] and not result["receipts"]


@pytest.mark.parametrize("limits, reason", [({"max_files": 1}, "file_limit"),
                                           ({"max_entries": 1}, "entry_limit")])
def test_incomplete_scan_never_reconciles_deletes(memory, root, limits, reason):
    (root / "removed.md").write_text("A removable project document.")
    first = capture_scan.scan_once(memory, root)
    memory_id = first["receipts"][0]["memory_ids"][0]
    (root / "removed.md").unlink()
    (root / "one.md").write_text("First new project document.")
    (root / "two.md").write_text("Second new project document.")
    result = capture_scan.scan_once(memory, root, **limits)
    assert not result["completed"] and result["reason"] == reason
    assert result["deleted"] == 0
    assert memory.db.get_memory(memory_id).status != MemoryStatus.TOMBSTONED
    assert capture_scan.scan_once(memory, root)["deleted"] == 1


def test_time_budget_and_stop_during_scan_prevent_deletes(memory, root, monkeypatch):
    (root / "removed.md").write_text("A removable project document.")
    capture_scan.scan_once(memory, root)
    (root / "removed.md").unlink()
    (root / "new.md").write_text("A new project document.")
    now = [0.0]
    original = capture_scan.capture_artifact

    def elapsed(*args):
        artifact = original(*args)
        now[0] = 2.0
        return artifact

    monkeypatch.setattr(capture_scan.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(capture_scan, "capture_artifact", elapsed)
    result = capture_scan.scan_once(memory, root, max_seconds=1)
    assert result["reason"] == "time_limit" and result["deleted"] == 0
    stop = Event()
    stop.set()
    result = capture_scan.scan_once(memory, root, stop_event=stop)
    assert result["reason"] == "stopped" and result["receipts"] == []


def test_changed_ignore_rules_oversize_and_symlink_are_not_deletion(memory, root, tmp_path):
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    for name in ("ignored.md", "large.md", "link.md"):
        (root / name).write_text("A captured project document: " + name)
    capture_scan.scan_once(memory, root)
    (root / ".gitignore").write_text("ignored.md\n")
    (root / "large.md").write_text("x" * 1000)
    (root / "link.md").unlink()
    (root / "link.md").symlink_to(tmp_path / "missing.md")
    result = capture_scan.scan_once(memory, root, max_file_bytes=100)
    assert result["completed"] and result["deleted"] == 0, result
    assert len(sources(memory)) == 3


@pytest.mark.parametrize("config, reason", [({}, "artifacts_disabled"),
                                            ({"paused": True}, "paused"),
                                            ({"enabled": False}, "paused"),
                                            ({"enabled": "yes"}, "invalid_config"),
                                            ({"artifacts_enabled": True, "allowed_roots": []}, "root_not_allowed")])
def test_trusted_policy_required_before_reading(memory, root, monkeypatch, config, reason):
    memory.capture_config = config

    def forbidden(*args):
        pytest.fail("Unauthorized scan attempted a file read")

    monkeypatch.setattr(capture_scan, "capture_artifact", forbidden)
    result = capture_scan.scan_once(memory, root)
    assert result["reason"] == reason and not result["completed"]


@pytest.mark.parametrize("kind", ["relative", "home", "filesystem", "file", "symlink"])
def test_unsafe_roots_rejected(memory, root, tmp_path, kind):
    if kind == "relative":
        path = Path("relative")
    elif kind == "home":
        path = Path.home()
    elif kind == "filesystem":
        path = Path(root.anchor)
    elif kind == "file":
        path = root / "file.md"
        path.write_text("Not a directory.")
    else:
        path = tmp_path / "linked-root"
        path.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        capture_scan.scan_once(memory, path)


def test_same_name_roots_and_branch_tracking_are_isolated(memory, root, tmp_path):
    other = tmp_path / "other" / root.name
    other.mkdir(parents=True)
    memory.capture_config["allowed_roots"].append(str(other))
    (root / "notes.md").write_text("First project architecture.")
    (other / "notes.md").write_text("Second project architecture.")
    first = capture_scan.scan_once(memory, root)
    second = capture_scan.scan_once(memory, other)
    assert first["workspace_id"] == "project"
    assert second["workspace_id"].startswith("project-")
    memory.create_branch("feature", switch=True)
    feature = capture_scan.scan_once(memory, root)
    assert feature["changed"] == 1 and feature["receipts"]
    assert len(sources(memory)) == 3


def test_failed_ingestion_retries_same_id_without_advancing_hash(memory, root, monkeypatch):
    (root / "notes.md").write_text("A project overview.")
    events = []
    original = capture_scan.ingest_event

    def failed(instance, event):
        events.append(event)
        return {"ok": False, "status": "failed", "retryable": True, "error_code": "processing_failed"}

    monkeypatch.setattr(capture_scan, "ingest_event", failed)
    result = capture_scan.scan_once(memory, root)
    assert not result["ok"] and result["changed"] == 0
    assert not sources(memory)

    def recovered(instance, event):
        events.append(event)
        return original(instance, event)

    monkeypatch.setattr(capture_scan, "ingest_event", recovered)
    assert capture_scan.scan_once(memory, root)["changed"] == 1
    assert events[0] == events[1]


def test_failed_delete_keeps_source_for_retry(memory, root, monkeypatch):
    (root / "notes.md").write_text("A project overview.")
    capture_scan.scan_once(memory, root)
    (root / "notes.md").unlink()
    original = capture_scan.ingest_event
    monkeypatch.setattr(capture_scan, "ingest_event", lambda *_: {"ok": False, "status": "failed", "retryable": True})
    assert capture_scan.scan_once(memory, root)["deleted"] == 0
    assert len(sources(memory)) == 1
    monkeypatch.setattr(capture_scan, "ingest_event", original)
    assert capture_scan.scan_once(memory, root)["deleted"] == 1


def test_watch_interruptible_wait_and_pre_stopped(memory, root):
    class StopAfterWait:
        def __init__(self):
            self.intervals = []

        def is_set(self):
            return False

        def wait(self, interval):
            self.intervals.append(interval)
            return True

    stop = StopAfterWait()
    results = list(capture_scan.watch(memory, root, interval=0.25, stop_event=stop))
    assert len(results) == 1 and results[0]["completed"]
    assert stop.intervals == [0.25]
    stopped = Event()
    stopped.set()
    assert list(capture_scan.watch(memory, root, stop_event=stopped)) == []


@pytest.mark.parametrize("limits", [{"max_files": 0}, {"max_entries": 0}, {"max_file_bytes": -1},
                                     {"max_seconds": float("nan")}, {"max_seconds": float("inf")},
                                     {"max_files": True}, {"max_entries": capture_scan.MAX_ENTRIES + 1}])
def test_invalid_limits(memory, root, limits):
    with pytest.raises(ValueError):
        capture_scan.scan_once(memory, root, **limits)
