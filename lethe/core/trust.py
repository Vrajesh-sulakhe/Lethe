import re
from typing import Dict, Any, Optional
from lethe.models import MemoryNode, MemoryStatus
from lethe.storage.db import DatabaseManager

class TrustGateManager:
    """
    Guards memory against planted false memories, indirect prompt injections,
    and unauthorized overwrite of sensitive user attributes.
    """
    SUSPICIOUS_PATTERNS = [
        r"system advisory",
        r"system note",
        r"ignore previous instructions",
        r"override .* constraints",
        r"transfer .* funds",
        r"admin override"
    ]

    def __init__(self, db: DatabaseManager):
        self.db = db

    def process_input(self, node: MemoryNode) -> Dict[str, Any]:
        cursor = self.db.conn.cursor()
        entity = node.metadata.get("entity")
        attribute = node.metadata.get("attribute")

        # 1. Check for prompt injection heuristic signatures on untrusted sources
        is_suspicious = False
        if node.trust_level < 0.5:
            lower_content = node.content.lower()
            for pattern in self.SUSPICIOUS_PATTERNS:
                if re.search(pattern, lower_content):
                    is_suspicious = True
                    break

        # 2. Check if this untrusted source attempts to overwrite a higher-trust existing fact in scope & branch
        if entity and attribute:
            query = """
                SELECT * FROM memories
                WHERE status = 'active'
                  AND branch = ?
                  AND json_extract(metadata_json, '$.entity') = ?
                  AND json_extract(metadata_json, '$.attribute') = ?
                  AND trust_level >= 0.8
            """
            params = [node.branch or "main", entity, attribute]
            if node.workspace_id:
                query += " AND (workspace_id = ? OR scope = 'global' OR workspace_id IS NULL)"
                params.append(node.workspace_id)
            cursor.execute(query, params)
            existing_high_trust = cursor.fetchone()

            if existing_high_trust and node.trust_level < 0.5:
                # Malicious or unauthorized overwrite attempt!
                node.status = MemoryStatus.QUARANTINED
                self.db.save_memory(node)
                return {
                    "allowed": False,
                    "action": "QUARANTINED",
                    "reason": f"Untrusted source '{node.source_origin}' (trust: {node.trust_level}) attempted to overwrite verified entity {entity}.{attribute}."
                }

        if is_suspicious and node.trust_level < 0.5:
            node.status = MemoryStatus.QUARANTINED
            self.db.save_memory(node)
            return {
                "allowed": False,
                "action": "QUARANTINED",
                "reason": f"Suspicious prompt injection signature detected from untrusted source '{node.source_origin}'."
            }

        # Otherwise allowed
        self.db.save_memory(node)
        return {
            "allowed": True,
            "action": "SAVED",
            "reason": "Passed trust gate."
        }

