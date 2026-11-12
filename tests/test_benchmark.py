import json
import tempfile
from pathlib import Path

from lethe.eval.benchmark import run_benchmark
from lethe.eval.dataset_generator import generate_dataset


def test_full_30_session_benchmark_and_report_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dataset_path = tmp / "sessions_30.json"
        out_path = tmp / "results_table.json"
        generate_dataset(dataset_path)

        results = run_benchmark(
            dataset_path=dataset_path,
            output_path=out_path,
            token_budget=400,
        )

        assert out_path.exists()
        saved = json.loads(out_path.read_text(encoding="utf-8"))
        assert saved == results

        assert results["baseline_tokens_per_query"] >= 50000
        assert results["lethe_tokens_per_query"] <= 400
        assert results["token_savings_pct"] >= 99.0
        assert results["lethe_latency_ms"] < 50.0
        assert results["latency_reduction_pct"] >= 98.0
        assert results["deletion_accuracy_pct"] == 100.0
        assert results["poisoning_defense_pct"] == 100.0
        assert results["queries_tested"] == 10
