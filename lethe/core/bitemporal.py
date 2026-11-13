import time
from typing import Optional, List, Dict, Any
from lethe.models import MemoryNode, MemoryStatus
from lethe.storage.db import DatabaseManager

class BitemporalManager:
    """
    Manages Bitemporal validity and Conflict Resolution.
    - System Time: when the memory was recorded.
    - Valid Time: the interval [valid_from, valid_to] during which the fact is true in reality.
    Distinguishes legitimate real-world updates from epistemic contradictions.
    """
    def __init__(self, db: DatabaseManager):
        self.db = db

    def process_statement(self, node: MemoryNode) -> List[Dict[str, Any]]:
        with self.db._lock:
            cursor = self.db.conn.cursor()
            conflicts = []

            entity = node.metadata.get("entity")
            attribute = node.metadata.get("attribute")
            new_val = node.metadata.get("value")

            if not entity or not attribute:
                # Not an entity-attribute fact; save directly
                self.db.save_memory(node)
                return []

            # Find existing active memories for the same entity and attribute in scope & branch
            query = """
                SELECT * FROM memories
                WHERE status IN ('active', 'conflict')
                  AND branch = ?
                  AND json_extract(metadata_json, '$.entity') = ?
                  AND json_extract(metadata_json, '$.attribute') = ?
            """
            params: List[Any] = [node.branch or "main", entity, attribute]
            if node.workspace_id:
                query += " AND (workspace_id = ? OR scope = 'global' OR workspace_id IS NULL)"
                params.append(node.workspace_id)
            cursor.execute(query, params)
            existing_rows = cursor.fetchall()

            if not existing_rows:
                self.db.save_memory(node)
                return []

            saved_node = False
            for row in existing_rows:
                old_node = self.db._row_to_node(row)
                old_val = old_node.metadata.get("value")

                if old_val == new_val:
                    # Same value: maintain consistency without generating conflict
                    continue

                # Check if this is an update (new time strictly after old time) or contradiction
                if node.valid_from > old_node.valid_from and node.trust_level >= old_node.trust_level:
                    # Legitimate update (e.g. moved from Pune to Bengaluru)
                    self.db.update_memory_status(
                        old_node.id,
                        status=MemoryStatus.HISTORICAL,
                        valid_to=node.valid_from
                    )
                    node.status = MemoryStatus.ACTIVE
                    self.db.save_memory(node)
                    saved_node = True
                else:
                    # Overlapping interval with conflicting values (Contradiction!)
                    self.db.update_memory_status(old_node.id, status=MemoryStatus.CONFLICT)
                    node.status = MemoryStatus.CONFLICT
                    self.db.save_memory(node)
                    saved_node = True

                    conflicts.append({
                        "entity": entity,
                        "attribute": attribute,
                        "conflicting_ids": [old_node.id, node.id],
                        "message": f"Contradiction detected for {entity}.{attribute}: '{old_val}' (Source: {old_node.source_origin}) vs '{new_val}' (Source: {node.source_origin})"
                    })

            if not saved_node:
                self.db.save_memory(node)

            return conflicts

    def query_active_attribute(
        self,
        entity: str,
        attribute: str,
        as_of_time: Optional[float] = None,
        workspace_id: Optional[str] = None,
        is_trusted: bool = True,
        branch: str = "main",
    ) -> Optional[MemoryNode]:
        with self.db._lock:
            cursor = self.db.conn.cursor()
            if as_of_time is None:
                as_of_time = time.time()

            query = """
                SELECT * FROM memories
                WHERE json_extract(metadata_json, '$.entity') = ?
                  AND json_extract(metadata_json, '$.attribute') = ?
                  AND branch = ?
                  AND valid_from <= ?
                  AND (valid_to IS NULL OR valid_to > ?)
                  AND status IN ('active', 'conflict', 'historical')
            """
            params: List[Any] = [entity, attribute, branch or "main", as_of_time, as_of_time]
            if workspace_id:
                if is_trusted:
                    query += " AND (workspace_id = ? OR scope = 'global' OR workspace_id IS NULL)"
                    params.append(workspace_id)
                else:
                    query += " AND workspace_id = ? AND scope != 'global'"
                    params.append(workspace_id)
            query += " ORDER BY valid_from DESC LIMIT 1"

            cursor.execute(query, params)
            row = cursor.fetchone()
            if not row:
                return None
            return self.db._row_to_node(row)

