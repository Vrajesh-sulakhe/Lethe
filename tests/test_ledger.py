import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
import pytest

from lethe.core.engine import LetheMemory
from lethe.models import MemoryStatus, StorageTier
from lethe.storage.ledger import CryptographicLedger


def test_ledger_append_and_hash_chain_continuity():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            # Store 5 memories
            nodes = []
            for i in range(5):
                n = mem.store(f"Architectural decision #{i}: component uses config variant {i}.", category="arch")
                nodes.append(n)

            # Verify 5 blocks recorded
            assert mem.ledger.count_blocks() == 5

            # Verify unbroken cryptographic hash chain
            valid, error = mem.ledger.verify_chain()
            assert valid is True
            assert error is None

            # Verify each node has a cryptographic salt
            for n in nodes:
                assert mem.ledger.has_salt(n.id) is True
        finally:
            mem.close()


def test_salt_shredding_on_unlearning():
    """
    Core invariant:
    When forget() runs:
    1. The node's cryptographic salt in ledger_salts is wiped/shredded.
    2. A TOMBSTONE block is appended referencing the cryptographic DeletionReceipt.
    3. The ledger hash chain remains 100% mathematically valid (verifiable audit trail).
    4. Adversaries cannot brute-force deleted PII because salt entropy is destroyed.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            # 1. Store confidential PII
            secret_node = mem.store(
                "Confidential: Developer personal API key is sk-prod-secret-99482.",
                category="credentials",
            )
            node_id = secret_node.id

            # Verify salt is stored
            assert mem.ledger.has_salt(node_id) is True
            initial_blocks = mem.ledger.count_blocks()
            assert initial_blocks == 1

            # 2. Forget / purge the memory
            receipt = mem.forget(node_id)
            assert receipt.purged_count == 1
            assert node_id in receipt.purged_node_ids

            # 3. VERIFY SALT IS SHREDDED
            assert mem.ledger.has_salt(node_id) is False

            # 4. Verify TOMBSTONE block was appended
            assert mem.ledger.count_blocks() == 2
            blocks = mem.ledger.get_blocks_for_node(node_id)
            assert len(blocks) == 2
            assert blocks[0].action == "STORE"
            assert blocks[1].action == "TOMBSTONE"
            assert blocks[1].metadata.get("salt_shredded") is True
            assert blocks[1].metadata.get("receipt_hash") == receipt.verification_hash

            # 5. VERIFY HASH CHAIN CONTINUITY REMAINS VALID
            valid, error = mem.ledger.verify_chain()
            assert valid is True
            assert error is None
        finally:
            mem.close()


def test_tamper_detection_in_ledger():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        mem = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            mem.store("Fact 1: Postgres port is 5432.", category="db")
            mem.store("Fact 2: Redis port is 6379.", category="db")

            valid, _ = mem.ledger.verify_chain()
            assert valid is True

            # Out-of-band tampering with sequence 1
            cursor = mem.db.conn.cursor()
            cursor.execute("UPDATE ledger_blocks SET payload_hash = 'tampered_hash_value' WHERE sequence = 1")
            mem.db.conn.commit()

            # Ledger verification must fail!
            tampered_valid, reason = mem.ledger.verify_chain()
            assert tampered_valid is False
            assert "Corrupted block hash at sequence 1" in reason
        finally:
            mem.close()


def test_mcp_verify_ledger_tool():
    from lethe.mcp.server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"

        server = create_mcp_server(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            t_store = server._tool_manager._tools["lethe_store"].fn
            t_verify = server._tool_manager._tools["lethe_verify_ledger"].fn

            t_store(content="Initial micro-decision for service mesh.", category="mesh")
            t_store(content="Secondary micro-decision for timeout bounds.", category="mesh")

            res_verify = json.loads(t_verify())
            assert res_verify["ledger_valid"] is True
            assert res_verify["total_blocks"] == 2
            assert res_verify["cryptographic_proof"] == "MERKLE_CHAIN_VALID"
        finally:
            server._lethe_memory.close()


def test_cli_ledger_commands():
    from lethe.cli import cmd_store, cmd_ledger

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        procs_dir = str(Path(tmp_dir) / "procs")

        cmd_store(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            content="CLI ledger test fact A.",
            category="cli",
            tier="semantic",
            source="user_explicit",
            session="cli",
            workspace=None,
            scope="project",
            valid_from=None,
            entity=None,
            attribute=None,
            value=None,
            code_file=None,
            code_symbol=None,
            code_sha=None,
        ))

        # View ledger
        cmd_ledger(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            verify=False,
            limit=10,
        ))

        # Verify ledger
        cmd_ledger(argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            verify=True,
            limit=10,
        ))

