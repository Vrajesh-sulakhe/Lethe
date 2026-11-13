import sqlite3
import json
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from lethe.models import MemoryNode, MemoryStatus, StorageTier, HandoverPacket, CodeAnchor

SCHEMA_VERSION = 3


def get_current_workspace_id(start_path: Path | str = ".") -> str:
    """
    Resolves the active workspace identifier.
    Traverses upwards to find the enclosing git repository root, or falls back to directory name.
    """
    p = Path(start_path).resolve()
    for parent in [p] + list(p.parents):
        if (parent / ".git").exists():
            return parent.name
    return p.name


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
                scope TEXT NOT NULL DEFAULT 'project'
            );
        """)

        # Migration: ensure workspace_id and scope columns exist
        cursor.execute("PRAGMA table_info(memories);")
        columns = [row["name"] for row in cursor.fetchall()]
        if "workspace_id" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN workspace_id TEXT;")
        if "scope" not in columns:
            cursor.execute("ALTER TABLE memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'project';")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);")

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

        # Handover packets table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS handover_packets (
                task_id TEXT PRIMARY KEY,
                packet_json TEXT NOT NULL,
                created_at REAL NOT NULL
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

            cursor.execute("PRAGMA quick_check;")
            integrity = str(cursor.fetchone()[0]).lower()

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
                    parent_ids_json, metadata_json, workspace_id, scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ))

            # Register lineage edges for parent_ids
            for parent_id in node.parent_ids:
                cursor.execute("""
                    INSERT OR IGNORE INTO lineage_edges (parent_id, child_id, relation_type, created_at)
                    VALUES (?, ?, 'derived_from', ?)
                """, (parent_id, node.id, node.created_at))

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

    def list_memories(
        self,
        category: Optional[str] = None,
        status: Optional[MemoryStatus] = None,
        as_of_time: Optional[float] = None,
        workspace_id: Optional[str] = None,
        tier: Optional[StorageTier] = None,
        session_id: Optional[str] = None,
        is_trusted: bool = True,
    ) -> List[MemoryNode]:
        with self._lock:
            cursor = self.conn.cursor()
            query = "SELECT * FROM memories WHERE 1=1"
            params: List[Any] = []

            if category:
                query += " AND category = ?"
                params.append(category)

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
            self.conn.commit()

    def delete_memory_physical(self, memory_id: str) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            cursor.execute("DELETE FROM lineage_edges WHERE parent_id = ? OR child_id = ?", (memory_id, memory_id))
            cursor.execute("DELETE FROM embeddings WHERE memory_id = ?", (memory_id,))
            self.conn.commit()

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
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cursor = self.conn.cursor()
            if as_of_time is not None:
                query = """
                    SELECT e.memory_id, e.vector_json, m.content, m.category, m.tier,
                           m.trust_level, m.status, m.source_session, m.workspace_id, m.scope
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
                           m.trust_level, m.status, m.source_session, m.workspace_id, m.scope
                    FROM embeddings e
                    JOIN memories m ON e.memory_id = m.id
                    WHERE m.status IN ('active', 'conflict')
                """
                params = []

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
                })
            return results

    def save_handover_packet(self, packet: HandoverPacket) -> None:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO handover_packets (task_id, packet_json, created_at)
                VALUES (?, ?, ?)
            """, (packet.task_id, packet.model_dump_json(), packet.created_at))
            self.conn.commit()

    def get_handover_packet(self, task_id: str) -> Optional[HandoverPacket]:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT packet_json FROM handover_packets WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return HandoverPacket.model_validate_json(row["packet_json"])

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
            code_anchor=code_anchor,
            metadata=meta,
        )

    def close(self):
        if self.conn:
            self.conn.close()
