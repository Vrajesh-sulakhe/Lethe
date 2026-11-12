import hashlib
import time
from typing import List, Dict, Any
from lethe.models import MemoryStatus, DeletionReceipt
from lethe.storage.db import DatabaseManager

class LineageManager:
    """
    Manages the Lineage Directed Acyclic Graph (DAG) for verifiable unlearning.
    Guarantees cascading invalidation of derived summaries, vector embeddings,
    and downstream inferences when a root fact is deleted.
    """
    def __init__(self, db: DatabaseManager):
        self.db = db

    def add_lineage_edge(self, parent_id: str, child_id: str, relation_type: str = "derived_from") -> None:
        cursor = self.db.conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO lineage_edges (parent_id, child_id, relation_type, created_at)
            VALUES (?, ?, ?, ?)
        """, (parent_id, child_id, relation_type, time.time()))
        self.db.conn.commit()

    def get_descendants(self, target_id: str) -> List[str]:
        """
        Traverses the Lineage DAG using recursive SQL Common Table Expression (CTE).
        Returns all downstream nodes derived directly or indirectly from target_id.
        """
        cursor = self.db.conn.cursor()
        cursor.execute("""
            WITH RECURSIVE descendants AS (
                SELECT child_id FROM lineage_edges WHERE parent_id = ?
                UNION
                SELECT e.child_id FROM lineage_edges e
                INNER JOIN descendants d ON e.parent_id = d.child_id
            )
            SELECT DISTINCT child_id FROM descendants;
        """, (target_id,))
        rows = cursor.fetchall()
        return [r["child_id"] for r in rows]

    def cascading_purge(self, target_id: str, reason: str = "user_deletion_request") -> DeletionReceipt:
        """
        Executes complete cascading unlearning:
        1. Identifies the target node and all downstream descendants.
        2. Sets memory status to TOMBSTONED.
        3. Scrubs the content text to prevent memory leaks from summaries or profiles.
        4. Physically deletes all associated vector embeddings.
        5. Computes a cryptographic SHA-256 audit receipt.
        """
        cursor = self.db.conn.cursor()
        
        # Verify target node actually exists before purging
        cursor.execute("SELECT id FROM memories WHERE id = ?", (target_id,))
        if not cursor.fetchone():
            return DeletionReceipt(
                target_id=target_id,
                purged_node_ids=[],
                purged_count=0,
                verification_hash="0" * 64,
                timestamp=time.time()
            )

        descendants = self.get_descendants(target_id)
        all_purged_ids = [target_id] + [d for d in descendants if d != target_id]

        timestamp = time.time()
        for node_id in all_purged_ids:
            # 1. Update status to TOMBSTONED and redact content
            cursor.execute("""
                UPDATE memories
                SET status = 'tombstoned',
                    content = '[PURGED_VIA_UNLEARNING]',
                    valid_to = ?
                WHERE id = ?
            """, (timestamp, node_id))

            # 2. Physically purge from embeddings table
            cursor.execute("DELETE FROM embeddings WHERE memory_id = ?", (node_id,))

        self.db.conn.commit()

        # 3. Generate cryptographic verification hash
        sorted_ids_str = ",".join(sorted(all_purged_ids))
        hash_payload = f"{target_id}:{sorted_ids_str}:{timestamp}:{reason}"
        verification_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

        return DeletionReceipt(
            target_id=target_id,
            purged_node_ids=all_purged_ids,
            purged_count=len(all_purged_ids),
            verification_hash=verification_hash,
            timestamp=timestamp
        )

