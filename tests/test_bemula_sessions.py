import tempfile
from pathlib import Path

from lethe.core.engine import LetheMemory
from lethe.eval.bemula_probes import BemulaProbeSuite, run_bemula_on_sessions
from lethe.eval.dataset_generator import generate_dataset
from lethe.models import StorageTier


def test_bemula_probes_on_30_session_dataset():
    with tempfile.TemporaryDirectory() as tmpdir:
        dataset_path = Path(tmpdir) / "sessions_30.json"
        generate_dataset(dataset_path)
        results = run_bemula_on_sessions(dataset_path=dataset_path, target_secret="9876543210")

        assert len(results) == 5
        for probe_name, outcome in results.items():
            assert outcome.startswith("PASS"), f"{probe_name} failed: {outcome}"
            assert "FAIL" not in outcome


def test_bemula_boolean_probe_detects_prefix_leak_before_purge():
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "test_leak.db")
        mem.store(
            "My phone number starting with 987 is 9876543210",
            category="contact",
            tier=StorageTier.SEMANTIC,
            source="user_explicit",
        )
        suite = BemulaProbeSuite(mem, target_secret="9876543210")
        outcome = suite.test_boolean()
        assert outcome.startswith("FAIL")
        mem.close()
