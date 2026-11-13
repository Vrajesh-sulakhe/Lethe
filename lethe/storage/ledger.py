import hashlib
import json
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from lethe.models import MemoryNode
from lethe.storage.db import DatabaseManager


class LedgerBlock:
    """Represents a tamper-evident block in the append-only cryptographic ledger."""

    def __init__(
        self,
        sequence: int,
        prev_hash: str,
        block_hash: str,
        action: str,
        node_id: str,
        payload_hash: str,
        timestamp: float,
        metadata: Dict[str, Any],
    ):
        self.sequence = sequence
        self.prev_hash = prev_hash
        self.block_hash = block_hash
        self.action = action
        self.node_id = node_id
        self.payload_hash = payload_hash
        self.timestamp = timestamp
        self.metadata = metadata

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "prev_hash": self.prev_hash,
            "block_hash": self.block_hash,
            "action": self.action,
            "node_id": self.node_id,
            "payload_hash": self.payload_hash,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class CryptographicLedger:
    """
    Append-only cryptographic state-transition ledger with per-node salt shredding.
    Provides verifiable mathematical proof of database continuity (SOC2 / GDPR Art. 17)
    while completely preventing deleted PII from being brute-forced by adversaries.
    """

    def __init__(self, db: DatabaseManager):
        self.db = db
        self._lock = threading.Lock()

    def get_or_create_salt(self, node_id: str) -> str:
        """Retrieves or generates a cryptographically random 32-char hex salt for a node."""
        with self._lock, self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT salt FROM ledger_salts WHERE node_id = ?", (node_id,))
            row = cursor.fetchone()
            if row:
                return row["salt"]
            salt = secrets.token_hex(16)
            cursor.execute(
                "INSERT INTO ledger_salts (node_id, salt, created_at) VALUES (?, ?, ?)",
                (node_id, salt, time.time()),
            )
            return salt

    def has_salt(self, node_id: str) -> bool:
        """Checks if the cryptographic salt for this node is still intact."""
        with self._lock:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT 1 FROM ledger_salts WHERE node_id = ?", (node_id,))
            return cursor.fetchone() is not None

    def shred_salt(self, node_id: str) -> bool:
        """
        Permanently destroys the cryptographic salt for a node.
        Leaves the historical block in the ledger intact for hash-chain continuity,
        while mathematically destroying the ability to rainbow-table or brute-force
        the original text content.
        """
        with self._lock, self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute("DELETE FROM ledger_salts WHERE node_id = ?", (node_id,))
            return cursor.rowcount > 0

    def append_block(
        self,
        action: str,
        node_id: str,
        payload_hash: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LedgerBlock:
        """Appends a new immutable block to the cryptographic ledger."""
        with self._lock, self.db.conn:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "SELECT sequence, block_hash FROM ledger_blocks ORDER BY sequence DESC LIMIT 1"
            )
            last = cursor.fetchone()
            if last:
                seq = int(last["sequence"]) + 1
                prev_hash = last["block_hash"]
            else:
                seq = 1
                prev_hash = "0" * 64

            now = round(time.time(), 6)
            meta = metadata or {}
            meta_str = json.dumps(meta, sort_keys=True)
            block_data = f"{seq}:{prev_hash}:{action}:{node_id}:{payload_hash}:{now}"
            block_hash = hashlib.sha256(block_data.encode("utf-8")).hexdigest()

            cursor.execute(
                """
                INSERT INTO ledger_blocks (sequence, prev_hash, block_hash, action, node_id, payload_hash, timestamp, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (seq, prev_hash, block_hash, action, node_id, payload_hash, now, meta_str),
            )

            return LedgerBlock(
                sequence=seq,
                prev_hash=prev_hash,
                block_hash=block_hash,
                action=action,
                node_id=node_id,
                payload_hash=payload_hash,
                timestamp=now,
                metadata=meta,
            )

    def record_store(self, node: MemoryNode) -> LedgerBlock:
        """Records a STORE transition for a memory node with salted payload commitment."""
        salt = self.get_or_create_salt(node.id)
        raw_payload = f"{node.content}:{salt}"
        payload_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        return self.append_block(
            action="STORE",
            node_id=node.id,
            payload_hash=payload_hash,
            metadata={
                "category": node.category,
                "tier": node.tier.value,
                "workspace_id": node.workspace_id,
                "trust_level": node.trust_level,
            },
        )

    def record_tombstone(
        self,
        node_id: str,
        receipt_hash: str,
        reason: str = "user_deletion_request",
    ) -> LedgerBlock:
        """
        Executes salt shredding and appends a TOMBSTONE block referencing the deletion receipt.
        """
        # 1. Permanently shred cryptographic salt
        self.shred_salt(node_id)

        # 2. Append verifiable tombstone commitment
        tombstone_payload = f"TOMBSTONED:{receipt_hash}"
        payload_hash = hashlib.sha256(tombstone_payload.encode("utf-8")).hexdigest()
        return self.append_block(
            action="TOMBSTONE",
            node_id=node_id,
            payload_hash=payload_hash,
            metadata={
                "reason": reason,
                "receipt_hash": receipt_hash,
                "salt_shredded": True,
            },
        )

    def verify_chain(self) -> Tuple[bool, Optional[str]]:
        """
        Mathematically verifies the integrity and unbroken continuity of the ledger.
        Checks:
        1. Genesis block references all-zero prev_hash.
        2. Every subsequent block references the exact block_hash of sequence - 1.
        3. Every block_hash recomputes to exact SHA-256(sequence:prev:action:node:payload:timestamp).
        """
        with self._lock:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT * FROM ledger_blocks ORDER BY sequence ASC")
            rows = cursor.fetchall()

            if not rows:
                return True, None

            expected_prev = "0" * 64
            for r in rows:
                seq = r["sequence"]
                prev_hash = r["prev_hash"]
                block_hash = r["block_hash"]
                action = r["action"]
                node_id = r["node_id"]
                payload_hash = r["payload_hash"]
                ts = r["timestamp"]

                if prev_hash != expected_prev:
                    return (
                        False,
                        f"Broken chain link at sequence {seq}: expected prev_hash '{expected_prev}', got '{prev_hash}'",
                    )

                expected_block_data = f"{seq}:{prev_hash}:{action}:{node_id}:{payload_hash}:{ts}"
                expected_hash = hashlib.sha256(expected_block_data.encode("utf-8")).hexdigest()
                if block_hash != expected_hash:
                    return (
                        False,
                        f"Corrupted block hash at sequence {seq}: expected '{expected_hash}', got '{block_hash}'",
                    )

                expected_prev = block_hash

            return True, None

    def get_blocks_for_node(self, node_id: str) -> List[LedgerBlock]:
        """Returns all ledger transitions associated with a specific memory node."""
        with self._lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "SELECT * FROM ledger_blocks WHERE node_id = ? ORDER BY sequence ASC", (node_id,)
            )
            rows = cursor.fetchall()
            return [
                LedgerBlock(
                    sequence=r["sequence"],
                    prev_hash=r["prev_hash"],
                    block_hash=r["block_hash"],
                    action=r["action"],
                    node_id=r["node_id"],
                    payload_hash=r["payload_hash"],
                    timestamp=r["timestamp"],
                    metadata=json.loads(r["metadata_json"]),
                )
                for r in rows
            ]

    def list_blocks(self, limit: int = 100) -> List[LedgerBlock]:
        """Lists latest ledger blocks in ascending sequence."""
        with self._lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "SELECT * FROM ledger_blocks ORDER BY sequence DESC LIMIT ?", (limit,)
            )
            rows = cursor.fetchall()
            blocks = [
                LedgerBlock(
                    sequence=r["sequence"],
                    prev_hash=r["prev_hash"],
                    block_hash=r["block_hash"],
                    action=r["action"],
                    node_id=r["node_id"],
                    payload_hash=r["payload_hash"],
                    timestamp=r["timestamp"],
                    metadata=json.loads(r["metadata_json"]),
                )
                for r in rows
            ]
            blocks.reverse()
            return blocks

    def count_blocks(self) -> int:
        """Returns the total number of blocks in the ledger."""
        with self._lock:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM ledger_blocks")
            row = cursor.fetchone()
            return int(row[0]) if row else 0

