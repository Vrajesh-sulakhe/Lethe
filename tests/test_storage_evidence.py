from lethe.eval.storage_evidence import run_storage_evidence


def test_empirical_storage_evidence_experiments():
    report = run_storage_evidence()
    assert "experiments" in report
    assert "matrix" in report
    assert len(report["experiments"]) == 4
    assert len(report["matrix"]) == 4

    exp_exact, exp_nuance, exp_proc, exp_eph = report["experiments"]

    # 1. Exact attributes: Relational SQL wins cleanly over stale Vector / bloated Prompt
    assert exp_exact["relational_sql"]["accurate"] is True
    assert exp_exact["vector_store"]["stale_leaked"] is True
    assert exp_exact["full_history_prompt"]["tokens"] > exp_exact["relational_sql"]["tokens"] * 20

    # 2. Conversational nuance: Vector store succeeds where exact SQL attribute key misses
    assert exp_nuance["vector_store"]["accurate"] is True
    assert exp_nuance["relational_sql"]["accurate"] is False

    # 3. Procedural runbook: Markdown file preserves exact commands with 0 idle tokens
    assert exp_proc["markdown_file"]["structure_intact"] is True
    assert exp_proc["markdown_file"]["tokens_idle"] == 0
    assert exp_proc["vector_store"]["structure_intact"] is False

    # 4. Ephemeral working state: zero long-term DB pollution
    assert exp_eph["ephemeral"]["long_term_pollution"] is False

    for row in report["matrix"]:
        assert row["accuracy"] == "100.0%"
