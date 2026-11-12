import tempfile
from pathlib import Path
import pytest
from lethe.core.engine import LetheMemory
from lethe.eval.bemula_probes import BemulaProbeSuite
from lethe.eval.handover_demo import simulate_handover
from lethe.eval.benchmark import run_benchmark
from lethe.models import StorageTier

def test_bemula_probe_suite_clean_abstention():
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "test_probes.db")
        secret = "9876543210"

        # Store secret
        m1 = mem.store(
            f"Personal phone number is {secret}",
            category="contact",
            tier=StorageTier.SEMANTIC,
            source="user_explicit"
        )
        # Store derived summary
        m2 = mem.store(
            f"Summary notes for emergency contact {secret}",
            category="summary",
            tier=StorageTier.SEMANTIC,
            source="agent_inferred",
            parent_ids=[m1.id]
        )

        # Before unlearning: Probe finds it
        res_before = mem.recall("What is my phone number?", token_budget=300)
        assert secret in res_before.context_text

        # Purge secret
        receipt = mem.forget(m1.id)
        assert receipt.purged_count == 2

        # Run all 5 Bemula attack probes
        suite = BemulaProbeSuite(mem, target_secret=secret)
        results = suite.run_all_probes()

        for probe_name, outcome in results.items():
            assert "FAIL" not in outcome
            assert "PASS" in outcome

        mem.close()

def test_handover_simulation_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "test_handover.db")
        assert simulate_handover(mem) is True
        mem.close()

def test_benchmark_metrics():
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "test_bench.db")
        mem.store("User is Priya, software engineer", category="bio", tier=StorageTier.SEMANTIC)
        results = run_benchmark(memory=mem)
        assert results["baseline_tokens_per_query"] > 50000
        assert results["token_savings_pct"] >= 99.0
        assert results["lethe_latency_ms"] < 50.0
        mem.close()

