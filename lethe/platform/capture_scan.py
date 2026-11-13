"""Bounded, opt-in observation of project HTML, Markdown and text files.

Any IDE or GUI that writes files can use this adapter without model/tool hooks.
File observation cannot read proprietary chats or unsaved editor buffers. The
trusted host supplies ``memory.capture_config``; scanning never enables capture,
changes an allowlist, installs a daemon, or reads project-local configuration.

Tracking stores source paths and hashes only. Ingestion owns extraction, durable
receipts and permanent unlearning suppression. Time/stop budgets are cooperative:
an in-flight artifact check or ingestion finishes under the core's own timeouts.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from threading import Event
import time

from lethe.core.artifacts import ArtifactRejected, MAX_ARTIFACT_BYTES, capture_artifact
from lethe.core.ingestion import ingest_event
from lethe.core.projects import register_project

MAX_FILES = 1000
MAX_ENTRIES = 10000
MAX_SECONDS = 30.0
MAX_DEPTH = 64
_SUFFIXES = {".html", ".htm", ".md", ".markdown", ".txt", ".rst"}
_EXCLUDED = {"node_modules", "vendor", "dist", "build", "target", "__pycache__", "venv", "coverage"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS capture_scan_sources (
    workspace_root TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (workspace_root, workspace_id, branch, path)
)
"""


class _Stopped(Exception):
    pass


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _directory(path):
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ArtifactRejected("safe_open_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _missing(root, relative):
    """Links and replacement directories are not evidence of deletion."""
    try:
        with _directory(root / relative.parent) as fd:
            os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
        return False
    except FileNotFoundError:
        return True
    except NotADirectoryError:
        return False


def _root_and_policy(memory, workspace_root):
    raw = Path(workspace_root)
    if not raw.is_absolute():
        raise ValueError("workspace_root must be an existing absolute directory")
    try:
        root = raw.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError):
        raise ValueError("workspace_root must be an existing absolute directory") from None
    if not root.is_dir():
        raise ValueError("workspace_root must be an existing absolute directory")
    if root == root.parent or root == Path.home().resolve():
        raise ValueError("Choose a project directory, not a home or filesystem root")
    if root != Path(os.path.abspath(raw)):
        raise ValueError("workspace_root must not contain symlinks")
    config = getattr(memory, "capture_config", {})
    if not isinstance(config, dict) or any(
        key in config and not isinstance(config[key], bool)
        for key in ("enabled", "paused", "artifacts_enabled")
    ):
        raise ArtifactRejected("invalid_config")
    if not config.get("enabled", True) or config.get("paused", False):
        raise ArtifactRejected("paused")
    if config.get("artifacts_enabled") is not True:
        raise ArtifactRejected("artifacts_disabled")
    allowed = config.get("allowed_roots", [])
    if not isinstance(allowed, (list, tuple)) or root not in {
        Path(os.path.abspath(value)) for value in allowed
        if isinstance(value, (str, Path)) and Path(value).is_absolute()
    }:
        raise ArtifactRejected("root_not_allowed")
    return root, config


def scan_once(memory, workspace_root, client="workspace-scan", *, max_files=MAX_FILES,
              max_file_bytes=MAX_ARTIFACT_BYTES, max_entries=MAX_ENTRIES,
              max_seconds=MAX_SECONDS, stop_event=None) -> dict:
    """Scan an authorized absolute project root and return counts plus core receipts.

    ``completed`` means traversal and deletion reconciliation finished. Incomplete
    traversals never reconcile deletes. Ignored, rejected, oversized or linked
    files are not considered removed. Limits include directory entries, candidate
    files, per-file bytes, depth and elapsed time; unchanged files still consume
    traversal budget. Retries keep deterministic event/session IDs across runs.
    Invalid roots/limits raise ValueError; policy and runtime failures are returned
    as content-free codes. Platforms without safe descriptor opens fail closed.
    """
    for value, ceiling in ((max_files, MAX_FILES), (max_entries, MAX_ENTRIES),
                           (max_file_bytes, MAX_ARTIFACT_BYTES)):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= ceiling:
            raise ValueError("Scan limits must be positive integers within the hard limits")
    if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)) or not math.isfinite(max_seconds) or not 0 < max_seconds <= MAX_SECONDS:
        raise ValueError("max_seconds must be positive and within the hard limit")
    if not isinstance(client, str) or not client.strip() or len(client) > 512 or any(ord(c) < 32 for c in client):
        raise ValueError("client must be a nonempty identifier")
    summary = {"ok": False, "completed": False, "workspace_id": None, "scanned": 0,
               "entries": 0, "unchanged": 0, "changed": 0, "deleted": 0,
               "skipped": 0, "receipts": [], "errors": [], "reason": None}
    deadline = time.monotonic() + max_seconds

    def check_budget():
        if stop_event is not None and stop_event.is_set():
            raise _Stopped("stopped")
        if time.monotonic() >= deadline:
            raise _Stopped("time_limit")

    def entry_budget():
        check_budget()
        if summary["entries"] >= max_entries:
            raise _Stopped("entry_limit")
        summary["entries"] += 1

    try:
        root, config = _root_and_policy(memory, workspace_root)
        check_budget()
        with memory.db._lock:
            if memory.db.conn.in_transaction:
                raise _Stopped("active_transaction")
            workspace_id = register_project(memory.db, str(root))
        summary["workspace_id"] = workspace_id
        branch = memory.current_branch
        scope = (str(root), workspace_id, branch)
        session_id = "workspace-scan-" + _digest(str(root))
        db = memory.db
        with db._lock:
            db.conn.execute(_SCHEMA)
            db.conn.commit()

        def send(kind, relative, digest):
            check_budget()
            result = ingest_event(memory, {
                "kind": kind, "workspace_root": str(root), "workspace_id": workspace_id,
                "branch": branch, "client": client, "session_id": session_id,
                "event_id": _digest("workspace-scan-v1", kind, str(root), relative, digest),
                "path": relative,
            })
            summary["receipts"].append(result)
            if result.get("retryable") or result.get("status") not in {
                "stored", "duplicate", "suppressed", "ignored", "quarantined", "deleted"
            }:
                summary["errors"].append(result.get("error_code", "ingestion_incomplete"))
                return False
            if result.get("ok") is False:
                summary["errors"].append("ingestion_quarantined")
            return True

        pending = [Path(".")]
        while pending:
            check_budget()
            directory = pending.pop()
            with _directory(root / directory) as fd, os.scandir(fd) as entries:
                for entry in entries:
                    entry_budget()
                    if entry.name.startswith(".") or entry.name.lower() in _EXCLUDED:
                        continue
                    relative = directory / entry.name
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if len(relative.parts) >= MAX_DEPTH:
                            raise _Stopped("depth_limit")
                        pending.append(relative)
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or relative.suffix.lower() not in _SUFFIXES:
                        continue
                    if summary["scanned"] >= max_files:
                        raise _Stopped("file_limit")
                    summary["scanned"] += 1
                    if info.st_size > max_file_bytes:
                        summary["skipped"] += 1
                        continue
                    try:
                        artifact = capture_artifact(str(root), relative.as_posix(), config)
                    except ArtifactRejected as exc:
                        summary["skipped"] += 1
                        if str(exc) in {"ignore_check_failed", "safe_open_unavailable", "file_changed_during_read"}:
                            raise _Stopped(str(exc)) from None
                        continue
                    check_budget()
                    path = artifact.relative_path
                    with db._lock:
                        previous = db.conn.execute(
                            "SELECT content_hash FROM capture_scan_sources WHERE workspace_root = ? "
                            "AND workspace_id = ? AND branch = ? AND path = ?", (*scope, path),
                        ).fetchone()
                    if previous and previous[0] == artifact.content_hash:
                        summary["unchanged"] += 1
                        continue
                    if send("file_changed", path, artifact.content_hash):
                        with db._lock:
                            db.conn.execute(
                                "INSERT OR REPLACE INTO capture_scan_sources VALUES (?, ?, ?, ?, ?)",
                                (*scope, path, artifact.content_hash),
                            )
                            db.conn.commit()
                        summary["changed"] += 1

        # Confirm absence separately: exclusions and changed ignore rules are not deletes.
        deletions = []
        after = ""
        while True:
            check_budget()
            with db._lock:
                rows = db.conn.execute(
                    "SELECT path, content_hash FROM capture_scan_sources WHERE workspace_root = ? "
                    "AND workspace_id = ? AND branch = ? AND path > ? ORDER BY path LIMIT 100",
                    (*scope, after),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                entry_budget()
                after = row[0]
                if _missing(root, Path(row[0])):
                    if len(deletions) >= max_files:
                        raise _Stopped("file_limit")
                    deletions.append((row[0], row[1]))
        check_budget()
        for path, digest in deletions:
            check_budget()
            if not _missing(root, Path(path)):
                continue
            if send("file_deleted", path, digest):
                with db._lock:
                    db.conn.execute(
                        "DELETE FROM capture_scan_sources WHERE workspace_root = ? AND workspace_id = ? "
                        "AND branch = ? AND path = ? AND content_hash = ?", (*scope, path, digest),
                    )
                    db.conn.commit()
                summary["deleted"] += 1
        summary["completed"] = True
        summary["ok"] = not summary["errors"]
        summary["reason"] = "completed" if summary["ok"] else "ingestion_incomplete"
    except (ArtifactRejected, _Stopped) as exc:
        summary["reason"] = str(exc)
    except OSError:
        summary["reason"] = "filesystem_error"
    except sqlite3.Error:
        summary["reason"] = "tracking_unavailable"
    return summary


def watch(memory, workspace_root, client="workspace-scan", *, interval=5.0,
          stop_event=None, **scan_limits):
    """Yield scan summaries until stopped; waiting is interruptible, with no daemon.

    The caller owns iteration and memory lifetime. A stopped watcher does not
    perform an initial scan. Each iteration rechecks trusted host capture policy.
    """
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be a positive finite number")
    stop_event = stop_event if stop_event is not None else Event()
    while not stop_event.is_set():
        yield scan_once(memory, workspace_root, client, stop_event=stop_event, **scan_limits)
        if stop_event.wait(interval):
            break
