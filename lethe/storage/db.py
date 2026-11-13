import sqlite3
import json
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from lethe.models import MemoryNode, MemoryStatus, StorageTier, HandoverPacket, CodeAnchor
from lethe.storage.fts_index import (
    delete_fts_entries as _fts_delete_entries,
    delete_fts_for_branch as _fts_delete_for_branch,
    ensure_fts_table as _fts_ensure_table,
    query_fts_memories as _fts_query_memories,
    sync_fts_node as _fts_sync_node,
)

SCHEMA_VERSION = 4


def get_current_workspace_id(start_path: Path | str = ".") -> str:
    """
    Resolves the active workspace identifier.
    Traverses upwards to find the enclosing git repository root, or falls back to
    the directory name (or 'global_desktop' when rooted at '/', drive root, or $HOME).
    """
    p = Path(start_path).resolve()
    home = Path.home().resolve()
    for parent in [p] + list(p.parents):
        if (parent / ".git").exists() and parent.name:
            return parent.name
    if p == home:
        return "global_desktop"
    return p.name or "global_desktop"


class DatabaseManager:
    """
    Cross-platform SQLite database manager for Lethe.
    Uses WAL mode, busy_timeout=30000, and PRAGMA user_version schema versioning
    to ensure Windows/Mac concurrency safety and zero-downtime migrations.
    """
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: Path | str = "lethe.db"):
        self.db_path = Path(db_path)
        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        # timeout=30.0 for cross-platform file-locking resilience;
        # check_same_thread=False allows safe usage from MCP/async worker threads under lock
        self._lock = threading.RLock()
        self._fts_enabled = False
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        
        # Configure PRAGMAs and schema
        with self._lock:
            self._init_pragmas()
            self._create_tables()

    def _init_pragmas(self):
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA busy_timeout = 30000;")
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        
        # Only set WAL if not already in WAL mode to avoid unnecessary lock contention
        cursor.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        if row and row[0].lower() != "wal":
            try:
                cursor.execute("PRAGMA journal_mode = WAL;")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("PRAGMA user_version;")
        ver_row = cursor.fetchone()
        current_ver = int(ver_row[0]) if ver_row else 0
        if current_ver >= self.SCHEMA_VERSION:
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unlearning_tombstones'"
            )
            if cursor.fetchone() is not None:
                cursor.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
                )
                if cursor.fetchone() is not None:
                    self._fts_enabled = True
                    return
                try:
                    cursor.execute("BEGIN IMMEDIATE;")
                except sqlite3.OperationalError:
                    pass
                self._fts_enabled = _fts_ensure_table(cursor)
                self.conn.commit()
                return

        try:
            cursor.execute("BEGIN IMMEDIATE;")
        except sqlite3.OperationalError:
            pass

        # Memories table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL,
                tier TEXT NOT NULL,
                trust_level REAL NOT NULL,
                status TEXT NOT NULL,
                source_session TEXT NOT NULL,
                source_origin TEXT NOT NULL,
                created_at REAL NOT NULL,
                valid_from REAL NOT NULL,
                valid_to REAL,
                parent_ids_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                workspace_id TEXT,
                scope TEXT NOT NULL DEFAULT 'project',
                branch TEXT NOT NULL DEFAULT 'main'
            );
        """)

        # Migration: ensure workspace_id, scope, and branch columns exist
        cursor.execute("PRAGMA table_info(memories);")
        columns = [row["name"] for row in cursor.fetchall()]
        if "workspace_id" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN workspace_id TEXT;")
        if "scope" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'project';")
        if "branch" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN branch TEXT NOT NULL DEFAULT 'main';")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_branch_status ON memories(branch, status);")

        # Lineage DAG edges table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lineage_edges (
                parent_id TEXT NOT NULL,
                child_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            );
        """)

        # Vector embeddings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS embeddings (
                memory_id TEXT PRIMARY KEY,
                dim INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
            );
        """)

        # Handover packets table (branch-isolated composite PK)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS handover_packets (
                task_id TEXT NOT NULL,
                packet_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                branch TEXT NOT NULL DEFAULT 'main',
                PRIMARY KEY (task_id, branch)
            );
        """)
        cursor.execute("PRAGMA table_info(handover_packets);")
        hp_info = {row["name"]: dict(row) for row in cursor.fetchall()}
        if "branch" not in hp_info or int(hp_info["branch"].get("pk", 0)) == 0:
            if "branch" not in hp_info:
                cursor.execute("ALTER TABLE handover_packets ADD COLUMN branch TEXT NOT NULL DEFAULT 'main';")
            cursor.execute("""
                CREATE TABLE handover_packets_v4 (
                    task_id TEXT NOT NULL,
                    packet_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    branch TEXT NOT NULL DEFAULT 'main',
                    PRIMARY KEY (task_id, branch)
                );
            """)
            cursor.execute("""
                INSERT OR REPLACE INTO handover_packets_v4 (task_id, packet_json, created_at, branch)
                SELECT task_id, packet_json, created_at, COALESCE(branch, 'main') FROM handover_packets;
            """)
            cursor.execute("DROP TABLE handover_packets;")
            cursor.execute("ALTER TABLE handover_packets_v4 RENAME TO handover_packets;")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_handover_branch_created ON handover_packets(branch, created_at);")

        # Memory Git branches table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_branches (
                name TEXT PRIMARY KEY,
                parent_branch TEXT,
                parent_commit_id TEXT,
                head_commit_id TEXT,
                created_at REAL NOT NULL,
                description TEXT NOT NULL DEFAULT ''
            );
        """)
        cursor.execute("PRAGMA table_info(memory_branches);")
        mb_cols = [row["name"] for row in cursor.fetchall()]
        if "parent_commit_id" not in mb_cols:
            cursor.execute("ALTER TABLE memory_branches ADD COLUMN parent_commit_id TEXT;")
        cursor.execute("""
            INSERT OR IGNORE INTO memory_branches (name, parent_branch, parent_commit_id, head_commit_id, created_at, description)
            VALUES ('main', NULL, NULL, NULL, ?, 'Default memory branch')
        """, (time.time(),))

        # Memory Git commits (snapshots) table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_commits (
                id TEXT PRIMARY KEY,
                branch TEXT NOT NULL,
                parent_commit_id TEXT,
                message TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_commits_branch_created ON memory_commits(branch, created_at);")

        # Permanent branch-independent unlearning tombstone registry (never deleted on rollback or branch delete)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS unlearning_tombstones (
                memory_id TEXT PRIMARY KEY,
                logical_key TEXT,
                purged_at REAL NOT NULL
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tombstones_logical_key ON unlearning_tombstones(logical_key);")

        # Reactive sync global state & per-client delivery cursors
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        cursor.execute("INSERT OR IGNORE INTO sync_state (key, value) VALUES ('active_branch', 'main');")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_cursors (
                client_id TEXT PRIMARY KEY,
                last_audit_id INTEGER NOT NULL DEFAULT 0,
                branch TEXT NOT NULL DEFAULT 'main',
                updated_at REAL NOT NULL
            );
        """)

        # Audit log table for state transition traceability
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                target_id TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
        """)

        # Append-only cryptographic state-transition ledger
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ledger_blocks (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                prev_hash TEXT NOT NULL,
                block_hash TEXT NOT NULL,
                action TEXT NOT NULL,
                node_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                timestamp REAL NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ledger_node_id ON ledger_blocks(node_id);")

        # Cryptographic per-node salts table (shredded on cascading purge)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ledger_salts (
                node_id TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                created_at REAL NOT NULL
            );
        """)

        # Track explicit schema version via PRAGMA user_version
        cursor.execute("PRAGMA user_version;")
        ver_row = cursor.fetchone()
        current_ver = int(ver_row[0]) if ver_row else 0
        if current_ver < self.SCHEMA_VERSION:
            cursor.execute(f"PRAGMA user_version = {int(self.SCHEMA_VERSION)};")

        self._fts_enabled = _fts_ensure_table(cursor)
        self.conn.commit()

    def get_schema_version(self) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("PRAGMA user_version;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def log_audit_event(self, event_type: str, target_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO audit_events (event_type, target_id, details_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_type, target_id, json.dumps(details or {}), time.time()),
            )
            self.conn.commit()

    def _run_integrity_check(self, cursor: sqlite3.Cursor) -> str:
        """
        Runs PRAGMA quick_check on a fresh snapshot connection for file-backed WAL DBs
        to prevent SQLite FTS5 vtab cache skew during concurrent multi-process writes.
        """
        db_target = str(self.db_path)
        if db_target == ":memory:" or db_target.startswith("file::memory:"):
            cursor.execute("PRAGMA quick_check;")
            return str(cursor.fetchone()[0]).lower()

        check_conn = sqlite3.connect(db_target, timeout=30.0)
        try:
            check_conn.execute("PRAGMA busy_timeout = 30000;")
            try:
                check_conn.execute("BEGIN IMMEDIATE;")
            except sqlite3.OperationalError:
                pass
            row = check_conn.execute("PRAGMA quick_check;").fetchone()
            return str(row[0]).lower() if row else "unknown"
        finally:
            if check_conn.in_transaction:
                try:
                    check_conn.rollback()
                except sqlite3.Error:
                    pass
            check_conn.close()

    def check_health(self, procedures_dir: Optional[Path | str] = None) -> Dict[str, Any]:
        """
        Executes a comprehensive health and readiness check on the SQLite database
        and optional procedures directory.
        """
        start = time.perf_counter()
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 1;")
            ping_ok = cursor.fetchone()[0] == 1

            cursor.execute("PRAGMA journal_mode;")
            journal_mode = str(cursor.fetchone()[0]).lower()

            integrity = self._run_integrity_check(cursor)

            cursor.execute("PRAGMA user_version;")
            schema_version = int(cursor.fetchone()[0])

            cursor.execute("SELECT COUNT(*) FROM memories;")
            total_memories = int(cursor.fetchone()[0])

            cursor.execute("SELECT status, COUNT(*) as cnt FROM memories GROUP BY status;")
            status_counts = {row["status"]: int(row["cnt"]) for row in cursor.fetchall()}

            cursor.execute("SELECT tier, COUNT(*) as cnt FROM memories GROUP BY tier;")
            tier_counts = {row["tier"]: int(row["cnt"]) for row in cursor.fetchall()}

            cursor.execute("SELECT COUNT(*) FROM handover_packets;")
            handover_count = int(cursor.fetchone()[0])

            cursor.execute("SELECT COUNT(*) FROM lineage_edges;")
            lineage_edges_count = int(cursor.fetchone()[0])

        proc_info: Dict[str, Any] = {"configured": False, "writable": True, "count": 0}
        if procedures_dir is not None:
            p_dir = Path(procedures_dir)
            try:
                p_dir.mkdir(parents=True, exist_ok=True)
                proc_files = list(p_dir.glob("*.md"))
                proc_info = {
                    "configured": True,
                    "path": str(p_dir.resolve()),
                    "writable": True,
                    "count": len(proc_files),
                }
            except Exception as exc:
                proc_info = {
                    "configured": True,
                    "path": str(p_dir),
                    "writable": False,
                    "error": str(exc),
                    "count": 0,
                }

        latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
        healthy = bool(ping_ok and integrity == "ok" and proc_info.get("writable", True))

        return {
            "status": "ok" if healthy else "degraded",
            "healthy": healthy,
            "db_path": str(self.db_path.resolve()),
            "journal_mode": journal_mode,
            "integrity": integrity,
            "schema_version": schema_version,
            "total_memories": total_memories,
            "status_counts": status_counts,
            "tier_counts": tier_counts,
            "handover_packets": handover_count,
            "lineage_edges": lineage_edges_count,
            "procedures": proc_info,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        }

    def save_memory(self, node: MemoryNode) -> None:
        if node.code_anchor and "code_anchor" not in node.metadata:
            node.metadata["code_anchor"] = node.code_anchor.model_dump()

        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO memories (
                    id, content, category, tier, trust_level, status,
                    source_session, source_origin, created_at, valid_from, valid_to,
                    parent_ids_json, metadata_json, workspace_id, scope, branch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                node.id,
                node.content,
                node.category,
                node.tier.value if isinstance(node.tier, StorageTier) else str(node.tier),
                float(node.trust_level),
                node.status.value if isinstance(node.status, MemoryStatus) else str(node.status),
                node.source_session,
                node.source_origin,
                float(node.created_at),
                float(node.valid_from),
                float(node.valid_to) if node.valid_to is not None else None,
                json.dumps(node.parent_ids),
                json.dumps(node.metadata),
                node.workspace_id,
                node.scope or "project",
                node.branch or "main",
            ))

            # Register lineage edges for parent_ids
            for parent_id in node.parent_ids:
                cursor.execute("""
                    INSERT OR IGNORE INTO lineage_edges (parent_id, child_id, relation_type, created_at)
                    VALUES (?, ?, 'derived_from', ?)
                """, (parent_id, node.id, node.created_at))

            _fts_sync_node(cursor, node, self._fts_enabled)
            self.conn.commit()

    def get_memory(self, memory_id: str) -> Optional[MemoryNode]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
            row = cursor.fetchone()
            if not row and len(memory_id) >= 6:
                cursor.execute("SELECT * FROM memories WHERE id LIKE ? LIMIT 2", (f"{memory_id}%",))
                matches = cursor.fetchall()
                if len(matches) == 1:
                    row = matches[0]
            if not row:
                return None
            return self._row_to_node(row)

    def list_workspaces(self) -> List[str]:
        """Returns all distinct non-empty workspace identifiers present in the database."""
        with self._lock:
            cursor = self.conn.cursor()
            rows = cursor.execute(
                "SELECT DISTINCT workspace_id FROM memories WHERE workspace_id IS NOT NULL AND workspace_id != '' ORDER BY workspace_id"
            ).fetchall()
            return [r[0] for r in rows if r[0]]

    def list_memories(
        self,
        category: Optional[str] = None,
        status: Optional[MemoryStatus] = None,
        as_of_time: Optional[float] = None,
        workspace_id: Optional[str] = None,
        tier: Optional[StorageTier] = None,
        session_id: Optional[str] = None,
        is_trusted: bool = True,
        branch: Optional[str] = None,
        scope: Optional[str] = None,
        limit: Optional[int] = None,
        order_desc: bool = True,
    ) -> List[MemoryNode]:
        with self._lock:
            cursor = self.conn.cursor()
            query = "SELECT * FROM memories WHERE 1=1"
            params: List[Any] = []

            if branch is not None:
                query += " AND branch = ?"
                params.append(branch)

            if category:
                query += " AND category = ?"
                params.append(category)

            if scope:
                query += " AND scope = ?"
                params.append(scope)

            if status:
                query += " AND status = ?"
                params.append(status.value if isinstance(status, MemoryStatus) else str(status))

            if tier:
                query += " AND tier = ?"
                params.append(tier.value if isinstance(tier, StorageTier) else str(tier))

            if session_id:
                query += " AND source_session = ?"
                params.append(session_id)

            if as_of_time is not None:
                query += " AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
                params.extend([as_of_time, as_of_time])

            if workspace_id:
                if is_trusted:
                    query += " AND (workspace_id = ? OR scope = 'global' OR workspace_id IS NULL)"
                    params.append(workspace_id)
                else:
                    query += " AND workspace_id = ? AND scope != 'global'"
                    params.append(workspace_id)

            query += " ORDER BY created_at DESC, rowid DESC" if order_desc else " ORDER BY created_at ASC, rowid ASC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(max(0, int(limit)))

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_node(r) for r in rows]

    def update_memory_status(
        self,
        memory_id: str,
        status: MemoryStatus,
        valid_to: Optional[float] = None
    ) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            val_status = status.value if isinstance(status, MemoryStatus) else str(status)
            if valid_to is not None:
                cursor.execute(
                    "UPDATE memories SET status = ?, valid_to = ? WHERE id = ?",
                    (val_status, float(valid_to), memory_id)
                )
            else:
                cursor.execute(
                    "UPDATE memories SET status = ? WHERE id = ?",
                    (val_status, memory_id)
                )
            if val_status in (MemoryStatus.TOMBSTONED.value, MemoryStatus.QUARANTINED.value):
                _fts_delete_entries(cursor, [memory_id], self._fts_enabled)
            self.conn.commit()

    def delete_memory_physical(self, memory_id: str) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            _fts_delete_entries(cursor, [memory_id], self._fts_enabled)
            cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            cursor.execute("DELETE FROM lineage_edges WHERE parent_id = ? OR child_id = ?", (memory_id, memory_id))
            cursor.execute("DELETE FROM embeddings WHERE memory_id = ?", (memory_id,))
            self.conn.commit()

    def delete_fts_entries(self, memory_ids: List[str]) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            _fts_delete_entries(cursor, memory_ids, self._fts_enabled)
            self.conn.commit()

    def delete_fts_for_branch(self, branch: str, include_tombstoned: bool = False) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            _fts_delete_for_branch(cursor, branch, include_tombstoned, self._fts_enabled)
            self.conn.commit()

    def search_fts(
        self,
        query: str,
        limit: int = 50,
        workspace_id: Optional[str] = None,
        as_of_time: Optional[float] = None,
        is_trusted: bool = True,
        branch: Optional[str] = "main",
        category: Optional[str] = None,
        session_id: Optional[str] = None,
        include_ephemeral: bool = True,
    ) -> List[Tuple[MemoryNode, float]]:
        with self._lock:
            cursor = self.conn.cursor()
            return _fts_query_memories(
                cursor,
                query=query,
                row_mapper=self._row_to_node,
                enabled=self._fts_enabled,
                limit=limit,
                workspace_id=workspace_id,
                as_of_time=as_of_time,
                is_trusted=is_trusted,
                branch=branch,
                category=category,
                session_id=session_id,
                include_ephemeral=include_ephemeral,
            )

    def save_embedding(self, memory_id: str, vector: List[float]) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO embeddings (memory_id, dim, vector_json)
                VALUES (?, ?, ?)
            """, (memory_id, len(vector), json.dumps(vector)))
            self.conn.commit()

    def get_embedding(self, memory_id: str) -> Optional[List[float]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT vector_json FROM embeddings WHERE memory_id = ?", (memory_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return json.loads(row["vector_json"])

    def get_all_active_embeddings(
        self,
        workspace_id: Optional[str] = None,
        as_of_time: Optional[float] = None,
        is_trusted: bool = True,
        branch: Optional[str] = "main",
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            if as_of_time is not None:
                query = """
                    SELECT e.memory_id, e.vector_json, m.content, m.category, m.tier,
                           m.trust_level, m.status, m.source_session, m.workspace_id, m.scope, m.branch
                    FROM embeddings e
                    JOIN memories m ON e.memory_id = m.id
                    WHERE m.status IN ('active', 'conflict', 'historical')
                      AND m.valid_from <= ?
                      AND (m.valid_to IS NULL OR m.valid_to > ?)
                """
                params: List[Any] = [as_of_time, as_of_time]
            else:
                query = """
                    SELECT e.memory_id, e.vector_json, m.content, m.category, m.tier,
                           m.trust_level, m.status, m.source_session, m.workspace_id, m.scope, m.branch
                    FROM embeddings e
                    JOIN memories m ON e.memory_id = m.id
                    WHERE m.status IN ('active', 'conflict')
                """
                params = []

            if branch is not None:
                query += " AND m.branch = ?"
                params.append(branch)

            if workspace_id:
                if is_trusted:
                    query += " AND (m.workspace_id = ? OR m.scope = 'global' OR m.workspace_id IS NULL)"
                    params.append(workspace_id)
                else:
                    query += " AND m.workspace_id = ? AND m.scope != 'global'"
                    params.append(workspace_id)

            cursor.execute(query, params)
            rows = cursor.fetchall()
            results = []
            for r in rows:
                keys = r.keys()
                results.append({
                    "memory_id": r["memory_id"],
                    "vector": json.loads(r["vector_json"]),
                    "content": r["content"],
                    "category": r["category"],
                    "tier": r["tier"] if "tier" in keys else "semantic",
                    "trust_level": r["trust_level"],
                    "status": r["status"],
                    "source_session": r["source_session"] if "source_session" in keys else "default_session",
                    "workspace_id": r["workspace_id"] if "workspace_id" in keys else None,
                    "scope": r["scope"] if "scope" in keys else "project",
                    "branch": r["branch"] if "branch" in keys and r["branch"] else "main",
                })
            return results

    def save_handover_packet(self, packet: HandoverPacket) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO handover_packets (task_id, packet_json, created_at, branch)
                VALUES (?, ?, ?, ?)
            """, (packet.task_id, packet.model_dump_json(), packet.created_at, packet.branch or "main"))
            self.conn.commit()

    def get_handover_packet(self, task_id: str, branch: Optional[str] = None) -> Optional[HandoverPacket]:
        with self._lock:
            cursor = self.conn.cursor()
            if branch is not None:
                cursor.execute(
                    "SELECT packet_json, branch FROM handover_packets WHERE task_id = ? AND branch = ?",
                    (task_id, branch),
                )
            else:
                active = self.get_active_branch()
                cursor.execute(
                    """
                    SELECT packet_json, branch FROM handover_packets
                    WHERE task_id = ?
                    ORDER BY (branch = ?) DESC, created_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (task_id, active),
                )
            row = cursor.fetchone()
            if not row:
                return None
            pkt = HandoverPacket.model_validate_json(row["packet_json"])
            if "branch" in row.keys() and row["branch"]:
                pkt.branch = row["branch"]
            return pkt

    def get_latest_handover_packet(self, branch: Optional[str] = None) -> Optional[HandoverPacket]:
        with self._lock:
            cursor = self.conn.cursor()
            target_branch = branch or self.get_active_branch()
            cursor.execute(
                "SELECT packet_json, branch FROM handover_packets WHERE branch = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (target_branch,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            pkt = HandoverPacket.model_validate_json(row["packet_json"])
            pkt.branch = row["branch"] or target_branch
            return pkt

    # --- Memory Git & Reactive Sync Helpers ---

    def get_active_branch(self) -> str:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT value FROM sync_state WHERE key = 'active_branch'")
            row = cursor.fetchone()
            return str(row["value"]) if row and row["value"] else "main"

    def set_active_branch(self, name: str) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('active_branch', ?)",
                (name,),
            )
            self.conn.commit()

    def create_branch_record(
        self,
        name: str,
        parent_branch: Optional[str] = None,
        parent_commit_id: Optional[str] = None,
        head_commit_id: Optional[str] = None,
        description: str = "",
    ) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO memory_branches (name, parent_branch, parent_commit_id, head_commit_id, created_at, description)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, parent_branch, parent_commit_id, head_commit_id, now, description),
            )
            self.conn.commit()
            return {
                "name": name,
                "parent_branch": parent_branch,
                "parent_commit_id": parent_commit_id,
                "head_commit_id": head_commit_id,
                "created_at": now,
                "description": description,
            }

    def update_branch_parent_commit(self, name: str, parent_commit_id: Optional[str]) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE memory_branches SET parent_commit_id = ? WHERE name = ?",
                (parent_commit_id, name),
            )
            self.conn.commit()

    def get_branch_record(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM memory_branches WHERE name = ?", (name,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_branches(self) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM memory_branches ORDER BY created_at ASC")
            return [dict(r) for r in cursor.fetchall()]

    def update_branch_head(self, name: str, commit_id: Optional[str]) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE memory_branches SET head_commit_id = ? WHERE name = ?",
                (commit_id, name),
            )
            self.conn.commit()

    def delete_branch_record(self, name: str) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            _fts_delete_for_branch(cursor, name, True, self._fts_enabled)
            cursor.execute("DELETE FROM memories WHERE branch = ?", (name,))
            cursor.execute("DELETE FROM handover_packets WHERE branch = ?", (name,))
            cursor.execute("DELETE FROM memory_commits WHERE branch = ?", (name,))
            cursor.execute("DELETE FROM memory_branches WHERE name = ?", (name,))
            self.conn.commit()

    def save_commit(
        self,
        commit_id: str,
        branch: str,
        parent_commit_id: Optional[str],
        message: str,
        snapshot_json: str,
        created_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        ts = float(created_at if created_at is not None else time.time())
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT INTO memory_commits (id, branch, parent_commit_id, message, snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (commit_id, branch, parent_commit_id, message, snapshot_json, ts),
            )
            cursor.execute(
                "UPDATE memory_branches SET head_commit_id = ? WHERE name = ?",
                (commit_id, branch),
            )
            self.conn.commit()
            return {
                "id": commit_id,
                "branch": branch,
                "parent_commit_id": parent_commit_id,
                "message": message,
                "snapshot_json": snapshot_json,
                "created_at": ts,
            }

    def get_commit(self, commit_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM memory_commits WHERE id = ?", (commit_id,))
            row = cursor.fetchone()
            if not row and len(commit_id) >= 4:
                cursor.execute("SELECT * FROM memory_commits WHERE id LIKE ? LIMIT 2", (f"{commit_id}%",))
                matches = cursor.fetchall()
                if len(matches) == 1:
                    row = matches[0]
            return dict(row) if row else None

    def list_commits(self, branch: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            if branch is not None:
                cursor.execute(
                    "SELECT * FROM memory_commits WHERE branch = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (branch, limit),
                )
            else:
                cursor.execute(
                    "SELECT * FROM memory_commits ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                )
            return [dict(r) for r in cursor.fetchall()]

    def record_unlearning_tombstones(
        self,
        entries: List[tuple[str, Optional[str], float]],
    ) -> None:
        if not entries:
            return
        with self._lock:
            cursor = self.conn.cursor()
            cursor.executemany(
                """
                INSERT OR REPLACE INTO unlearning_tombstones (memory_id, logical_key, purged_at)
                VALUES (?, ?, ?)
                """,
                entries,
            )
            self.conn.commit()

    def is_tombstoned_permanent(
        self,
        memory_ids: List[str],
        logical_key: Optional[str] = None,
    ) -> bool:
        cleaned = [str(m) for m in memory_ids if m]
        with self._lock:
            cursor = self.conn.cursor()
            if cleaned:
                placeholders = ",".join("?" for _ in cleaned)
                cursor.execute(
                    f"SELECT 1 FROM unlearning_tombstones WHERE memory_id IN ({placeholders}) LIMIT 1",
                    cleaned,
                )
                if cursor.fetchone() is not None:
                    return True
                cursor.execute(
                    f"SELECT 1 FROM memories WHERE id IN ({placeholders}) AND status = ? LIMIT 1",
                    [*cleaned, MemoryStatus.TOMBSTONED.value],
                )
                if cursor.fetchone() is not None:
                    return True
            if logical_key:
                cursor.execute(
                    "SELECT 1 FROM unlearning_tombstones WHERE logical_key = ? LIMIT 1",
                    (logical_key,),
                )
                if cursor.fetchone() is not None:
                    return True
            return False

    def scrub_unlearned_history(self, purged_ids: List[str]) -> None:
        """
        Scrubs unlearned memory content from stored commits and audit logs so purged
        secrets/PII cannot survive in commit snapshots or reactive sync streams.
        """
        id_set = {str(pid) for pid in purged_ids if pid}
        if not id_set:
            return
        with self._lock:
            cursor = self.conn.cursor()
            _fts_delete_entries(cursor, list(id_set), self._fts_enabled)
            # 1. Scrub audit_events.details_json entries referencing purged target_ids
            placeholders = ",".join("?" for _ in id_set)
            cursor.execute(
                f"SELECT id, details_json FROM audit_events WHERE target_id IN ({placeholders})",
                list(id_set),
            )
            for row in cursor.fetchall():
                try:
                    details = json.loads(row["details_json"]) if row["details_json"] else {}
                except Exception:
                    details = {}
                if "summary" in details:
                    details["summary"] = "[PURGED_VIA_UNLEARNING]"
                cursor.execute(
                    "UPDATE audit_events SET details_json = ? WHERE id = ?",
                    (json.dumps(details), row["id"]),
                )

            # 2. Scrub memory_commits.snapshot_json nodes matching purged IDs
            cursor.execute("SELECT id, snapshot_json FROM memory_commits")
            for crow in cursor.fetchall():
                try:
                    snap = json.loads(crow["snapshot_json"])
                except Exception:
                    continue
                mems = snap.get("memories")
                if not isinstance(mems, list):
                    continue
                changed = False
                for item in mems:
                    if not isinstance(item, dict):
                        continue
                    item_id = str(item.get("id", ""))
                    origin_id = str((item.get("metadata") or {}).get("origin_memory_id", ""))
                    if item_id in id_set or (origin_id and origin_id in id_set):
                        item["content"] = "[PURGED_VIA_UNLEARNING]"
                        item["status"] = MemoryStatus.TOMBSTONED.value
                        if isinstance(item.get("metadata"), dict):
                            item["metadata"] = {"purged": True}
                        changed = True
                if changed:
                    cursor.execute(
                        "UPDATE memory_commits SET snapshot_json = ? WHERE id = ?",
                        (json.dumps(snap), crow["id"]),
                    )
            self.conn.commit()

    def get_data_version(self) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("PRAGMA data_version;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_max_audit_id(self) -> int:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events;")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

    def get_audit_events_since(self, since_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                SELECT id, event_type, target_id, details_json, created_at
                FROM audit_events
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(since_id), int(limit)),
            )
            results: List[Dict[str, Any]] = []
            for row in cursor.fetchall():
                try:
                    details = json.loads(row["details_json"]) if row["details_json"] else {}
                except Exception:
                    details = {}
                results.append({
                    "id": int(row["id"]),
                    "event_type": row["event_type"],
                    "target_id": row["target_id"],
                    "details": details,
                    "created_at": float(row["created_at"]),
                })
            return results

    def get_sync_cursor(self, client_id: str) -> Dict[str, Any]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM sync_cursors WHERE client_id = ?", (client_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return {
                "client_id": client_id,
                "last_audit_id": 0,
                "branch": self.get_active_branch(),
                "updated_at": 0.0,
            }

    def update_sync_cursor(self, client_id: str, last_audit_id: int, branch: str = "main") -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO sync_cursors (client_id, last_audit_id, branch, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (client_id, int(last_audit_id), branch, time.time()),
            )
            self.conn.commit()

    def _row_to_node(self, row: sqlite3.Row) -> MemoryNode:
        keys = row.keys()
        meta = json.loads(row["metadata_json"])
        code_anchor = None
        if "code_anchor" in meta and meta["code_anchor"]:
            try:
                code_anchor = CodeAnchor.model_validate(meta["code_anchor"])
            except Exception:
                code_anchor = None

        return MemoryNode(
            id=row["id"],
            content=row["content"],
            category=row["category"],
            tier=StorageTier(row["tier"]),
            trust_level=float(row["trust_level"]),
            status=MemoryStatus(row["status"]),
            source_session=row["source_session"],
            source_origin=row["source_origin"],
            created_at=float(row["created_at"]),
            valid_from=float(row["valid_from"]),
            valid_to=float(row["valid_to"]) if row["valid_to"] is not None else None,
            parent_ids=json.loads(row["parent_ids_json"]),
            workspace_id=row["workspace_id"] if "workspace_id" in keys else None,
            scope=row["scope"] if "scope" in keys and row["scope"] else "project",
            branch=row["branch"] if "branch" in keys and row["branch"] else "main",
            code_anchor=code_anchor,
            metadata=meta,
        )

    def close(self):
        if self.conn:
            self.conn.close()
