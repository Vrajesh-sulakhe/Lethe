import tempfile
from pathlib import Path
import pytest

from lethe.core.drift import (
    compute_file_content_hash,
    check_symbol_in_file,
    check_code_drift,
)
from lethe.core.engine import LetheMemory
from lethe.models import CodeAnchor, StorageTier


def test_compute_file_content_hash():
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_file = Path(tmp_dir) / "sample.py"
        test_file.write_text("print('hello world')\n", encoding="utf-8")

        h = compute_file_content_hash(test_file)
        assert h is not None
        assert len(h) == 64

        # Non-existent file returns None
        assert compute_file_content_hash(Path(tmp_dir) / "does_not_exist.py") is None


def test_check_symbol_in_file():
    code = """
import os

DATABASE_URL = "sqlite:///test.db"

class PaymentGateway:
    def process(self):
        pass

def calculate_tax(amount: float) -> float:
    return amount * 0.15

async def async_fetch():
    return True
"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        py_file = Path(tmp_dir) / "module.py"
        py_file.write_text(code, encoding="utf-8")

        assert check_symbol_in_file(py_file, "PaymentGateway") is True
        assert check_symbol_in_file(py_file, "calculate_tax") is True
        assert check_symbol_in_file(py_file, "async_fetch") is True
        assert check_symbol_in_file(py_file, "DATABASE_URL") is True
        assert check_symbol_in_file(py_file, "non_existent_function") is False

        # Non-python fallback (e.g. .ts or .go)
        ts_code = "export interface OrderConfig { timeoutMs: number; }"
        ts_file = Path(tmp_dir) / "config.ts"
        ts_file.write_text(ts_code, encoding="utf-8")
        assert check_symbol_in_file(ts_file, "OrderConfig") is True
        assert check_symbol_in_file(ts_file, "timeoutMs") is True
        assert check_symbol_in_file(ts_file, "MissingInterface") is False


def test_code_anchor_auto_population_and_no_drift():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"
        source_file = Path(tmp_dir) / "auth.py"
        source_file.write_text("def authenticate_jwt():\n    return True\n", encoding="utf-8")

        memory = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id=tmp_dir)
        try:
            node = memory.store(
                content="Always validate JWT expiration before signature verification.",
                category="architecture",
                tier=StorageTier.SEMANTIC,
                code_anchor=CodeAnchor(
                    file_path=str(source_file),
                    symbol_name="authenticate_jwt",
                ),
            )

            assert node.code_anchor is not None
            assert node.code_anchor.file_path == str(source_file)
            assert node.code_anchor.symbol_name == "authenticate_jwt"
            assert node.code_anchor.content_hash is not None

            # Recall when file has not changed
            res = memory.recall("validate JWT signature expiration")
            assert len(res.memories) == 1
            assert len(res.drift_warnings) == 0
            assert "DRIFT WARNING" not in res.context_text
        finally:
            memory.close()


def test_drift_detected_on_content_modification():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"
        source_file = Path(tmp_dir) / "cache.py"
        source_file.write_text("CACHE_TTL = 3600\ndef get_cache(): pass\n", encoding="utf-8")

        memory = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id=tmp_dir)
        try:
            memory.store(
                content="Redis cache TTL is strictly fixed to 3600 seconds.",
                category="cache_config",
                code_anchor=CodeAnchor(
                    file_path=str(source_file),
                    symbol_name="get_cache",
                ),
            )

            # Modify file content (TTL changed) while symbol remains
            source_file.write_text("CACHE_TTL = 7200\ndef get_cache(): pass\n", encoding="utf-8")

            res = memory.recall("Redis cache TTL fixed seconds")
            assert len(res.memories) == 1
            assert len(res.drift_warnings) == 1
            assert "has changed since this decision was recorded" in res.drift_warnings[0]
            assert "DRIFT WARNING: File" in res.context_text
        finally:
            memory.close()


def test_drift_detected_on_symbol_removal():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"
        source_file = Path(tmp_dir) / "orders.py"
        source_file.write_text("def process_order(): pass\n", encoding="utf-8")

        memory = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id=tmp_dir)
        try:
            memory.store(
                content="Order processing pipeline delegates payment checks to process_order.",
                category="order_system",
                code_anchor=CodeAnchor(
                    file_path=str(source_file),
                    symbol_name="process_order",
                ),
            )

            # Remove the symbol completely
            source_file.write_text("def execute_order(): pass\n", encoding="utf-8")

            res = memory.recall("Order processing pipeline delegates payment")
            assert len(res.memories) == 1
            assert len(res.drift_warnings) == 1
            assert "Referenced symbol 'process_order' was removed" in res.drift_warnings[0]
            assert "DRIFT WARNING: Referenced symbol 'process_order' was removed" in res.context_text
        finally:
            memory.close()


def test_drift_detected_on_file_deletion():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"
        source_file = Path(tmp_dir) / "legacy_handler.py"
        source_file.write_text("def legacy(): pass\n", encoding="utf-8")

        memory = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id=tmp_dir)
        try:
            memory.store(
                content="Legacy endpoints route via legacy_handler file.",
                category="routing",
                code_anchor=CodeAnchor(file_path=str(source_file)),
            )

            # Delete file
            source_file.unlink()

            res = memory.recall("Legacy endpoints route via legacy_handler")
            assert len(res.memories) == 1
            assert len(res.drift_warnings) == 1
            assert "no longer exists on disk" in res.drift_warnings[0]
            assert "DRIFT WARNING: Referenced file" in res.context_text
        finally:
            memory.close()


def test_code_anchor_persistence_in_db():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"
        source_file = Path(tmp_dir) / "db_config.py"
        source_file.write_text("POOL_SIZE = 10\n", encoding="utf-8")

        mem1 = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id=tmp_dir)
        node_id = mem1.store(
            content="Database connection pool size is 10 connections.",
            category="database",
            code_anchor=CodeAnchor(file_path=str(source_file), symbol_name="POOL_SIZE"),
        ).id
        mem1.close()

        # Reopen with new instance to verify DB persistence
        mem2 = LetheMemory(db_path=str(db_path), procedures_dir=str(procs_dir), workspace_id=tmp_dir)
        try:
            stored_node = mem2.db.get_memory(node_id)
            assert stored_node is not None
            assert stored_node.code_anchor is not None
            assert stored_node.code_anchor.symbol_name == "POOL_SIZE"
            assert stored_node.code_anchor.file_path == str(source_file)
            assert stored_node.code_anchor.content_hash is not None
        finally:
            mem2.close()


def test_mcp_drift_tool():
    import json
    from lethe.mcp.server import create_mcp_server

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        procs_dir = Path(tmp_dir) / "procs"
        source_file = Path(tmp_dir) / "service.py"
        source_file.write_text("def start_service(): pass\n", encoding="utf-8")

        server = create_mcp_server(db_path=str(db_path), procedures_dir=str(procs_dir))
        try:
            tool_store = server._tool_manager._tools["lethe_store"].fn
            tool_recall = server._tool_manager._tools["lethe_recall"].fn

            # 1. Store via MCP tool
            res_store = json.loads(
                tool_store(
                    content="Service worker startup sequence is managed by start_service.",
                    category="services",
                    code_file=str(source_file),
                    code_symbol="start_service",
                    workspace_id=tmp_dir,
                )
            )
            assert res_store["status"] == "active"
            assert res_store["code_anchor"]["symbol_name"] == "start_service"

            # 2. Recall via MCP tool before modification
            res_recall1 = json.loads(
                tool_recall(
                    query="Service worker startup sequence managed",
                    workspace_id=tmp_dir,
                )
            )
            assert len(res_recall1["drift_warnings"]) == 0

            # 3. Modify source file and recall again
            source_file.write_text("def run_service(): pass\n", encoding="utf-8")
            res_recall2 = json.loads(
                tool_recall(
                    query="Service worker startup sequence managed",
                    workspace_id=tmp_dir,
                )
            )
            assert len(res_recall2["drift_warnings"]) == 1
            assert "Referenced symbol 'start_service' was removed" in res_recall2["drift_warnings"][0]
        finally:
            server._lethe_memory.close()


def test_cli_drift_integration():
    import argparse
    from lethe.cli import cmd_store, cmd_recall

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test.db")
        procs_dir = str(Path(tmp_dir) / "procs")
        source_file = Path(tmp_dir) / "config.py"
        source_file.write_text("MAX_RETRIES = 5\n", encoding="utf-8")

        store_args = argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            content="Max connection retries is fixed to 5.",
            category="network",
            tier="semantic",
            source="user_explicit",
            session="cli_test",
            workspace=tmp_dir,
            scope="project",
            valid_from=None,
            entity=None,
            attribute=None,
            value=None,
            code_file=str(source_file),
            code_symbol="MAX_RETRIES",
            code_sha=None,
        )
        cmd_store(store_args)

        # Modify file
        source_file.write_text("MAX_RETRIES = 10\n", encoding="utf-8")

        recall_args = argparse.Namespace(
            db=db_path,
            procedures_dir=procs_dir,
            query="Max connection retries",
            budget=400,
            as_of=None,
            entity=None,
            workspace=tmp_dir,
            session=None,
            model=None,
        )
        # Should execute cleanly without error
        cmd_recall(recall_args)

