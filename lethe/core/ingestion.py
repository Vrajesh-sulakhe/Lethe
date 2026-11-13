"""Client-independent automatic ingestion with content-free durable receipts.

Public API: ``ingest_event(memory, event)`` and ``capture_status(memory, workspace_id=None)``.
Events carry client/session_id/event_id/kind, optional workspace_id/branch, and
message or workspace_root/path. IDs returned are complete engine UUIDs.

Trusted host policy lives on ``memory.capture_config``: enabled (default True),
paused (default False), artifacts_enabled (default False), allowed_roots ([]).
Event fields cannot override policy. No configuration files are read or written.

Architecture: a DB-adjacent OS lock serializes ingestion across processes and is
released on process death. The existing DB RLock protects the shared connection.
Each receipt/item checkpoint commits *before* calling store; there is never an
outer transaction spanning the engine's independent commits. A scoped SQLite
trigger binds the first engine write to its prepared item atomically, including
crashes inside store. Retries repair missing indexes/ledger/audit without creating
a second memory. Errors are static codes, never exception messages or raw events.

Content/path hashes and UUIDs survive unlearning, not text. Scoped triggers copy
those hashes into permanent suppression on tombstone or physical deletion,
including branch-copy origin IDs. Suppression is deliberately branch-independent;
artifact deletion suppresses the whole path, even if later edited. These tables
are outside memory snapshots and are lazily installed without changing user_version.
Only memories ingested through this service have this suppression guarantee.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from lethe.core.artifacts import ArtifactRejected, artifact_location, capture_artifact, contains_sensitive
from lethe.core.capture import capture_hash, extract_durable_facts
from lethe.models import MemoryStatus, StorageTier

MAX_MESSAGE_CHARS = 128 * 1024
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingestion_receipts (
    receipt_id TEXT PRIMARY KEY, event_hash TEXT NOT NULL,
    workspace_id TEXT NOT NULL, branch TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_items (
    item_key TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, branch TEXT NOT NULL,
    kind TEXT NOT NULL, subject_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
    memory_id TEXT, state TEXT NOT NULL DEFAULT 'prepared'
);
CREATE INDEX IF NOT EXISTS idx_ingestion_memory ON ingestion_items(memory_id);
CREATE INDEX IF NOT EXISTS idx_ingestion_subject ON ingestion_items(subject_hash, workspace_id, branch);
CREATE TABLE IF NOT EXISTS ingestion_receipt_items (
    receipt_id TEXT NOT NULL, item_key TEXT NOT NULL,
    PRIMARY KEY(receipt_id, item_key)
);
CREATE TABLE IF NOT EXISTS ingestion_suppressions (
    suppression_key TEXT PRIMARY KEY, created_at REAL NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ingestion_bind_memory AFTER INSERT ON memories
WHEN json_extract(NEW.metadata_json, '$.ingestion_key') IS NOT NULL
BEGIN
    UPDATE ingestion_items SET memory_id = NEW.id
    WHERE item_key = json_extract(NEW.metadata_json, '$.ingestion_key') AND memory_id IS NULL;
END;
CREATE TRIGGER IF NOT EXISTS ingestion_guard_memory BEFORE INSERT ON memories
WHEN EXISTS (
    SELECT 1 FROM ingestion_items i
    WHERE i.item_key = json_extract(NEW.metadata_json, '$.ingestion_key')
    AND (i.state = 'suppressed' OR EXISTS (
        SELECT 1 FROM ingestion_suppressions s
        WHERE s.suppression_key IN (i.content_hash, i.subject_hash)
    ))
)
BEGIN SELECT RAISE(ABORT, 'ingestion_suppressed'); END;
CREATE TRIGGER IF NOT EXISTS ingestion_guard_embedding BEFORE INSERT ON embeddings
WHEN EXISTS (SELECT 1 FROM ingestion_items i JOIN memories m ON m.id = i.memory_id
             WHERE m.id = NEW.memory_id AND (i.state = 'suppressed' OR m.status = 'tombstoned'))
BEGIN SELECT RAISE(ABORT, 'ingestion_suppressed'); END;
CREATE TRIGGER IF NOT EXISTS ingestion_guard_salt BEFORE INSERT ON ledger_salts
WHEN EXISTS (SELECT 1 FROM ingestion_items i JOIN memories m ON m.id = i.memory_id
             WHERE m.id = NEW.node_id AND (i.state = 'suppressed' OR m.status = 'tombstoned'))
BEGIN SELECT RAISE(ABORT, 'ingestion_suppressed'); END;
"""


def _hash(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


class _Failure(Exception):
    pass


@contextmanager
def _serialized(memory):
    db = memory.db
    with db._lock:
        if db.conn.in_transaction:
            raise _Failure("active_transaction")
        if str(db.db_path) == ":memory:":
            try:
                yield
            finally:
                if db.conn.in_transaction:
                    db.conn.rollback()
            return
        path = Path(db.db_path).resolve()
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path) + ".ingestion.lock", flags, 0o600)
        locked = False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise _Failure("unsafe_lock_file")
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                deadline = time.monotonic() + 30
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise _Failure("ingestion_busy") from None
                        time.sleep(0.02)
            locked = True
            yield
        finally:
            if db.conn.in_transaction:
                db.conn.rollback()
            if locked:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _ensure_schema(memory):
    conn = memory.db.conn
    if conn.in_transaction:
        raise _Failure("active_transaction")
    conn.executescript(_SCHEMA)
    for name, timing, table, condition, identifier in (
        ("tombstone", "AFTER UPDATE OF status", "memories", "NEW.status = 'tombstoned'", "NEW.id"),
        ("delete", "BEFORE DELETE", "memories", "1", "OLD.id"),
        ("permanent", "AFTER INSERT", "unlearning_tombstones", "1", "NEW.memory_id"),
    ):
        origin = ""
        if table == "memories":
            prefix = "OLD" if name == "delete" else "NEW"
            origin = f" OR memory_id = json_extract({prefix}.metadata_json, '$.origin_memory_id')"
        conn.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS ingestion_suppress_{name} {timing} ON {table}
            WHEN {condition}
            BEGIN
                INSERT OR IGNORE INTO ingestion_suppressions
                    SELECT content_hash, strftime('%s','now') FROM ingestion_items
                    WHERE memory_id = {identifier}{origin};
                INSERT OR IGNORE INTO ingestion_suppressions
                    SELECT subject_hash, strftime('%s','now') FROM ingestion_items
                    WHERE kind = 'artifact' AND (memory_id = {identifier}{origin});
                UPDATE ingestion_items SET state = 'suppressed'
                    WHERE memory_id = {identifier}{origin};
            END;
        """)


def _policy(memory):
    config = getattr(memory, "capture_config", {})
    if not isinstance(config, dict):
        raise _Failure("invalid_config")
    for key in ("enabled", "paused", "artifacts_enabled"):
        if key in config and not isinstance(config[key], bool):
            raise _Failure("invalid_config")
    return config


def _text(value, code, limit=512):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise _Failure(code)
    return value


def _event(memory, event):
    if not isinstance(event, dict):
        raise _Failure("invalid_event")
    values = {key: _text(event.get(key), "invalid_" + key) for key in ("client", "session_id", "event_id", "kind")}
    if values["kind"] not in {"user_message", "file_changed", "file_deleted"}:
        raise _Failure("unsupported_kind")
    values["workspace_id"] = _text(event.get("workspace_id") or memory.workspace_id, "invalid_workspace_id")
    values["branch"] = _text(event.get("branch") or memory.current_branch, "invalid_branch")
    values["workspace_root"] = event.get("workspace_root")
    if values["kind"] == "user_message":
        message = event.get("message")
        if not isinstance(message, str) or len(message) > MAX_MESSAGE_CHARS:
            raise _Failure("invalid_message")
        values["message"] = message
        # A root is optional for message events and never grants file access.
        if values["workspace_root"] is not None:
            values["workspace_root"] = _text(values["workspace_root"], "invalid_root", 4096)
    else:
        values["workspace_root"] = _text(values["workspace_root"], "invalid_root", 4096)
        values["path"] = _text(event.get("path"), "invalid_path", 4096)
    return values


def _result(status, receipt_id=None, **extra):
    return {"ok": status not in {"failed", "processing", "quarantined", "rejected"},
            "status": status, "receipt_id": receipt_id, "memory_ids": [], "stored": 0,
            "duplicates": 0, "suppressed": 0, "skipped_sensitive": 0,
            "retryable": status in {"failed", "processing", "paused"}, "replayed": False, **extra}


def _checkpoint(memory, receipt_id, result):
    memory.db.conn.execute(
        "UPDATE ingestion_receipts SET status = ?, result_json = ?, updated_at = ? WHERE receipt_id = ?",
        (result["status"], json.dumps(result, sort_keys=True), time.time(), receipt_id),
    )
    memory.db.conn.commit()
    return result


def _is_suppressed(memory, item):
    if item["state"] == "suppressed":
        return True
    conn = memory.db.conn
    if conn.execute("SELECT 1 FROM ingestion_suppressions WHERE suppression_key IN (?, ?)",
                    (item["content_hash"], item["subject_hash"])).fetchone():
        return True
    return bool(item["memory_id"] and memory.db.is_tombstoned_permanent([item["memory_id"]]))


def _refresh_result(memory, result):
    rows = memory.db.conn.execute(
        "SELECT i.* FROM ingestion_items i JOIN ingestion_receipt_items r USING(item_key) WHERE r.receipt_id = ?",
        (result["receipt_id"],),
    ).fetchall()
    suppressed = [row for row in rows if _is_suppressed(memory, row)]
    ids = {row["memory_id"] for row in suppressed}
    result["memory_ids"] = [mid for mid in result["memory_ids"] if mid not in ids]
    if suppressed:
        result["suppressed"] = len(suppressed)
        result["stored"] = min(result["stored"], len(result["memory_ids"]))
        if not result["memory_ids"] and result["status"] in {"stored", "duplicate", "ignored", "suppressed"}:
            result["status"] = "suppressed"
    return result


def _finish_node(memory, node):
    """Repair the index and content-free audit after a partially committed store."""
    if node.status == MemoryStatus.QUARANTINED:
        return "quarantined"
    if node.status == MemoryStatus.TOMBSTONED or memory.db.is_tombstoned_permanent([node.id]):
        return "suppressed"
    if memory.db.get_embedding(node.id) is None:
        memory.semantic.store(node)
    conn = memory.db.conn
    if not conn.execute("SELECT 1 FROM ledger_blocks WHERE node_id = ? AND action = 'STORE'", (node.id,)).fetchone():
        memory.ledger.record_store(node)
    if not conn.execute("SELECT 1 FROM audit_events WHERE target_id = ? AND event_type IN ('MEMORY_STORED', 'MEMORY_CONFLICT')", (node.id,)).fetchone():
        memory.db.log_audit_event("MEMORY_STORED", node.id,
                                  {"workspace_id": node.workspace_id, "branch": node.branch,
                                   "category": node.category, "recovered_ingestion": True})
    return "stored"


def _capture_item(memory, event, receipt_id, candidate):
    conn = memory.db.conn
    ws, branch = event["workspace_id"], event["branch"]
    key = _hash("item-v1", ws, branch, candidate["kind"], candidate["subject_hash"], candidate["content_hash"])
    conn.execute(
        "INSERT OR IGNORE INTO ingestion_items (item_key, workspace_id, branch, kind, subject_hash, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (key, ws, branch, candidate["kind"], candidate["subject_hash"], candidate["content_hash"]),
    )
    conn.execute("INSERT OR IGNORE INTO ingestion_receipt_items VALUES (?, ?)", (receipt_id, key))
    conn.commit()
    item = conn.execute("SELECT * FROM ingestion_items WHERE item_key = ?", (key,)).fetchone()
    if _is_suppressed(memory, item):
        return "suppressed", None
    node = memory.db.get_memory(item["memory_id"]) if item["memory_id"] else None
    if item["memory_id"] and node is None:
        # Missing engine rows are never recreated by a rescan or retry.
        conn.execute("INSERT OR IGNORE INTO ingestion_suppressions VALUES (?, ?)", (candidate["content_hash"], time.time()))
        conn.commit()
        return "suppressed", None
    duplicate = item["state"] == "stored"
    if node is None:
        metadata = {"ingestion_key": key, "ingestion_receipt_id": receipt_id,
                    "capture_client": event["client"], "capture_event_id": event["event_id"],
                    "capture_kind": event["kind"], "capture_session_id": event["session_id"],
                    **candidate["metadata"]}
        if event.get("workspace_root"):
            metadata["workspace_root"] = event["workspace_root"]
        node = memory.store(
            candidate["content"], category=candidate["category"], tier=StorageTier.SEMANTIC,
            source="artifact_auto_capture" if candidate["kind"] == "artifact" else "user_auto_capture",
            trust_level=0.3 if candidate["kind"] == "artifact" else candidate["trust"],
            session_id=event["session_id"], workspace_id=ws, branch=branch,
            scope="project", metadata=metadata,
        )
    state = _finish_node(memory, node)
    conn.execute("UPDATE ingestion_items SET state = ? WHERE item_key = ?", (state, key))
    conn.commit()
    # Recheck permanent suppression after engine commits, including concurrent forget.
    item = conn.execute("SELECT * FROM ingestion_items WHERE item_key = ?", (key,)).fetchone()
    if _is_suppressed(memory, item):
        return "suppressed", None
    if state == "stored":
        return "duplicate" if duplicate else "stored", node.id
    return state, None


def _delete_artifact(memory, event, receipt_id, config):
    _, _, subject = artifact_location(event["workspace_root"], event["path"], config)
    conn = memory.db.conn
    rows = conn.execute("SELECT * FROM ingestion_items WHERE subject_hash = ? AND workspace_id = ? AND branch = ?",
                        (subject, event["workspace_id"], event["branch"])).fetchall()
    deleted = 0
    for row in rows:
        conn.execute("INSERT OR IGNORE INTO ingestion_receipt_items VALUES (?, ?)", (receipt_id, row["item_key"]))
        conn.commit()
        if row["memory_id"]:
            node = memory.db.get_memory(row["memory_id"])
            if node and node.status != MemoryStatus.TOMBSTONED:
                memory.forget(node.id, reason="artifact_file_deleted")
                deleted += 1
    conn.execute("INSERT OR IGNORE INTO ingestion_suppressions VALUES (?, ?)", (subject, time.time()))
    conn.commit()
    return _result("deleted", receipt_id, deleted=deleted, suppressed=len(rows))


def _process(memory, event, receipt_id, config):
    if config.get("paused", False) or not config.get("enabled", True):
        return _result("paused", receipt_id)
    if event["kind"] == "file_deleted":
        return _delete_artifact(memory, event, receipt_id, config)
    candidates = []
    skipped = 0
    if event["kind"] == "user_message":
        facts, skipped = extract_durable_facts(event["message"])
        for fact in facts:
            if contains_sensitive(fact.content):
                skipped += 1
                continue
            digest = _hash("fact-v1", fact.category, capture_hash(fact.content))
            candidates.append({"content": fact.content, "category": fact.category, "kind": "fact",
                               "subject_hash": digest, "content_hash": digest,
                               "trust": 0.95 if fact.explicit else 0.9,
                               "metadata": {"capture_hash": fact.capture_hash, "capture_rule": fact.rule}})
    else:
        saved = memory.db.conn.execute(
            "SELECT i.* FROM ingestion_items i JOIN ingestion_receipt_items r USING(item_key) "
            "WHERE r.receipt_id = ? AND i.kind = 'artifact' AND i.memory_id IS NOT NULL LIMIT 1",
            (receipt_id,),
        ).fetchone()
        if saved:
            # A retry repairs the original committed snapshot, not newly changed bytes.
            artifact_location(event["workspace_root"], event["path"], config)
            candidates.append({"content": "", "category": "artifact", "kind": "artifact",
                               "subject_hash": saved["subject_hash"], "content_hash": saved["content_hash"],
                               "metadata": {}})
        else:
            artifact = capture_artifact(event["workspace_root"], event["path"], config)
            candidates.append({"content": artifact.content, "category": "artifact", "kind": "artifact",
                               "subject_hash": artifact.subject_hash,
                               "content_hash": _hash("artifact-v1", artifact.content_hash),
                               "metadata": {"artifact_path": artifact.relative_path, "artifact_hash": artifact.content_hash,
                                            "artifact_media_type": artifact.media_type}})
    result = _result("ignored", receipt_id, skipped_sensitive=skipped)
    quarantined = 0
    for candidate in candidates:
        state, memory_id = _capture_item(memory, event, receipt_id, candidate)
        if memory_id:
            result["memory_ids"].append(memory_id)
        if state == "quarantined":
            quarantined += 1
        else:
            result[{"duplicate": "duplicates"}.get(state, state)] += 1
        # Progress receipts contain IDs and counts only, even when the next store fails.
        _checkpoint(memory, receipt_id, {**result, "status": "processing", "ok": False, "retryable": True})
    status = ("quarantined" if quarantined else "stored" if result["stored"] else
              "duplicate" if result["duplicates"] else "suppressed" if result["suppressed"] else "ignored")
    result.update(status=status, ok=status != "quarantined", quarantined=quarantined)
    return result


def ingest_event(memory, event: dict) -> dict:
    """Ingest one event; failed/processing receipts are retryable with the same event.

    ``ok=False`` is suitable for strict callers. Rejections are deliberate policy
    decisions, not successful capture. A receipt_id of None means validation or
    receipt persistence failed. Exception strings and payloads are never returned.
    """
    receipt_id = None
    try:
        values = _event(memory, event)
        receipt_id = _hash("event-v1", values["workspace_id"], values["branch"], values["client"],
                           values["session_id"], values["event_id"])
        event_hash = _hash(values)
        config = _policy(memory)
        with _serialized(memory):
            _ensure_schema(memory)
            conn = memory.db.conn
            previous = conn.execute("SELECT * FROM ingestion_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
            if previous and previous["event_hash"] != event_hash:
                return _result("rejected", receipt_id, error_code="event_id_conflict")
            if previous and previous["status"] not in {"failed", "processing", "paused"}:
                result = _refresh_result(memory, json.loads(previous["result_json"]))
                result["replayed"] = True
                return _checkpoint(memory, receipt_id, result)
            now = time.time()
            conn.execute(
                "INSERT OR IGNORE INTO ingestion_receipts (receipt_id, event_hash, workspace_id, branch, kind, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'processing', ?, ?)",
                (receipt_id, event_hash, values["workspace_id"], values["branch"], values["kind"], now, now),
            )
            conn.execute("UPDATE ingestion_receipts SET attempts = attempts + 1, status = 'processing', updated_at = ? WHERE receipt_id = ?", (now, receipt_id))
            conn.commit()
            try:
                result = _process(memory, values, receipt_id, config)
            except ArtifactRejected as exc:
                result = _result("rejected", receipt_id, error_code=str(exc))
            except Exception:
                # Only service-owned transactions can be open inside this lock.
                conn.rollback()
                progress = conn.execute("SELECT result_json FROM ingestion_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
                result = json.loads(progress[0]) if progress and progress[0] != "{}" else _result("failed", receipt_id)
                result.update(status="failed", ok=False, retryable=True, error_code="processing_failed")
            return _checkpoint(memory, receipt_id, result)
    except _Failure as exc:
        return _result("failed", None, error_code=str(exc))
    except Exception:
        return _result("failed", receipt_id, error_code="receipt_unavailable")


def capture_status(memory, workspace_id=None) -> dict:
    """Return policy flags and receipt/item counts, never content, paths, or events."""
    try:
        config = _policy(memory)
        with _serialized(memory):
            _ensure_schema(memory)
            clause, params = (" WHERE workspace_id = ?", (workspace_id,)) if workspace_id is not None else ("", ())
            counts = {row[0]: row[1] for row in memory.db.conn.execute(
                "SELECT status, COUNT(*) FROM ingestion_receipts" + clause + " GROUP BY status", params)}
            items = memory.db.conn.execute("SELECT COUNT(*) FROM ingestion_items" + clause, params).fetchone()[0]
            return {"ok": True, "enabled": config.get("enabled", True), "paused": config.get("paused", False),
                    "artifacts_enabled": config.get("artifacts_enabled", False), "receipt_counts": counts,
                    "item_count": items, "failed": counts.get("failed", 0), "processing": counts.get("processing", 0)}
    except Exception:
        return {"ok": False, "status": "failed", "error_code": "status_unavailable"}
