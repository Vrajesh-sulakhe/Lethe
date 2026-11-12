import tempfile
import time
from pathlib import Path
import pytest

from lethe.core.engine import LetheMemory
from lethe.core.knapsack import count_tokens
from lethe.models import MemoryNode, MemoryStatus, StorageTier


def _build_filler_paragraph(topic_idx: int, target_tokens: int = 80) -> str:
    """Builds a deterministic filler note of ~target_tokens with zero target needle overlap."""
    base = (
        f"Engineering retrospective batch {topic_idx}: evaluated PostgreSQL vacuum "
        f"scheduling, Kubernetes pod disruption budgets, Prometheus scrape intervals, "
        f"and GRPC connection pool keepalive tuning across staging clusters. "
    )
    text = base
    while count_tokens(text) < target_tokens:
        text += (
            f"Sub-metric {topic_idx}: p99 handshake latency remained steady during "
            f"synthetic load replay without heap fragmentation. "
        )
    return text.strip()


def test_post_format_token_budget_never_exceeded():
    """
    Verifies strict post-formatting token compliance across small, medium, conflict-heavy,
    and abstention queries: count_tokens(res.context_text) <= token_budget and
    res.tokens_used == count_tokens(res.context_text).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "budget.db")

        # Conflicting relational entries trigger "[EPISTEMIC CONFLICT WARNING: ...]" banner
        mem.store(
            content="Database engine is PostgreSQL 16 for billing ledger.",
            category="architecture",
            tier=StorageTier.RELATIONAL,
            source="user_explicit",
            session_id="s1",
            valid_from=1000.0,
            metadata={"entity": "billing", "attribute": "db", "value": "PostgreSQL 16"},
        )
        mem.store(
            content="Billing ledger database engine note says MySQL 8.",
            category="architecture",
            tier=StorageTier.RELATIONAL,
            source="tool_verified",
            session_id="s2",
            valid_from=1000.0,
            metadata={"entity": "billing", "attribute": "db", "value": "MySQL 8"},
        )
        mem.store(
            content="Billing backup bucket retention policy is 365 days in cold archive.",
            category="architecture",
            tier=StorageTier.SEMANTIC,
            source="user_explicit",
            session_id="s3",
        )

        # Oversized single record (~220 tokens)
        long_note = "Billing disaster recovery architecture note: " + ("verify checksums and snapshot manifests before promotion; " * 22)
        mem.store(
            content=long_note,
            category="architecture",
            tier=StorageTier.SEMANTIC,
            source="user_explicit",
            session_id="s4",
        )

        for budget in [0, 5, 15, 30, 50, 120, 400, 1000]:
            res = mem.recall("billing ledger database architecture policy", token_budget=budget)
            actual_tokens = count_tokens(res.context_text)
            assert actual_tokens <= budget, (
                f"Budget {budget} violated: rendered {actual_tokens} tokens: {res.context_text!r}"
            )
            assert res.tokens_used == actual_tokens, (
                f"Reported tokens_used={res.tokens_used} != actual={actual_tokens}"
            )

            # Unknown query abstention must ALSO obey tight budgets
            abstain_res = mem.recall("What is my secret Mars rover launch code?", token_budget=budget)
            assert abstain_res.is_abstention is True
            abstain_tokens = count_tokens(abstain_res.context_text)
            assert abstain_tokens <= budget, (
                f"Abstention budget {budget} violated: {abstain_tokens} tokens"
            )
            assert abstain_res.tokens_used == abstain_tokens

        # At budget=80 with the oversized long_note matching, Lethe must deliver a bounded slice (not fail silently)
        res_slice = mem.recall("disaster recovery checksums snapshot manifests", token_budget=80)
        assert not res_slice.is_abstention
        assert "checksums" in res_slice.context_text
        assert count_tokens(res_slice.context_text) <= 80
        assert res_slice.tokens_used == count_tokens(res_slice.context_text)

        mem.close()


def test_model_context_window_clamping_for_small_vs_large_models():
    """
    Tests multi-model context limit governance when the Memory Bus ("the cake") exceeds
    smaller model windows (e.g., 4k/8k) or remaining context budget.
    """
    from lethe.core.knapsack import compute_effective_token_budget, MODEL_CONTEXT_WINDOWS

    assert "claude" in " ".join(MODEL_CONTEXT_WINDOWS.keys()).lower()
    assert MODEL_CONTEXT_WINDOWS["tiny-4k"] == 4096

    # Requesting 10,000 tokens for a 4K model must auto-clamp to safe slice ceiling (<= 1024 = 25% of 4096)
    clamped_4k = compute_effective_token_budget(token_budget=10000, model_name="tiny-4k")
    assert 0 < clamped_4k <= 1024

    # Remaining context override must never allow memory slice to blow remaining window
    clamped_remaining = compute_effective_token_budget(
        token_budget=2000,
        model_name="claude-sonnet-4",
        remaining_context_tokens=120,
    )
    assert clamped_remaining <= 120

    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "models.db")
        for i in range(40):
            mem.store(
                content=f"Microservice topology node {i}: service-{i} handles auth token verification via Redis cluster {i}.",
                category="infra",
                tier=StorageTier.SEMANTIC,
            )

        # Even if caller asks for huge budget=50000, tiny-4k model stays safely bounded <= 1024
        res = mem.recall(
            "microservice topology service auth verification Redis cluster",
            token_budget=50000,
            model_name="tiny-4k",
        )
        assert not res.is_abstention
        assert count_tokens(res.context_text) <= 1024
        assert res.tokens_used == count_tokens(res.context_text)

        mem.close()


@pytest.mark.parametrize("scale_tokens,filler_tokens_each", [
    (4_000, 120),
    (32_000, 200),
    (64_000, 250),
    (128_000, 320),
])
def test_long_context_scaling_and_lost_in_the_middle_immunity(scale_tokens, filler_tokens_each):
    """
    Validates Lethe across 4K, 32K, 64K, and 128K+ cumulative stored corpus sizes:
    1. Positional Invariance: Needles placed at 0% (start), 50% (exact middle), and 100% (end)
       of the corpus are retrieved with 100% accuracy (immune to U-shaped Lost in the Middle decay).
    2. Output Slice Remains Constant & Bounded: Delivered slice stays <= 400 tokens (>99% savings at 128K).
    3. Bounded Retrieval Latency: Recall completes fast (< 250 ms even at 128K corpus).
    4. Distractor Resistance (Chroma Context Rot): Topically adjacent distractors do not drown exact needles.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / f"scale_{scale_tokens}.db")

        needle_start = "Project Orion emergency failover endpoint is orion-start-771.internal:9443."
        needle_mid = "Project Zephyr signing key rotation vault is zephyr-mid-404.vault.local."
        needle_end = "Project Hyperion rate limiter circuit threshold is hyperion-end-882 req/sec."

        total_fillers = max(6, scale_tokens // filler_tokens_each)
        mid_idx = total_fillers // 2

        mem.store(needle_start, category="endpoints", tier=StorageTier.SEMANTIC, session_id="s_start")

        # Insert realistic + topically adjacent distractors around beginning, middle, and end
        for i in range(total_fillers):
            if i == mid_idx:
                mem.store(needle_mid, category="security", tier=StorageTier.SEMANTIC, session_id="s_mid")
            if i % 15 == 0:
                # Chroma-style semantic distractor containing similar project/endpoint vocabulary
                content = (
                    f"Staging cluster checklist {i}: failover endpoint docs, key vault audit notes, "
                    f"and rate limiter planning templates (draft {i}, non-production placeholder)."
                )
            else:
                content = _build_filler_paragraph(i, target_tokens=filler_tokens_each)
            mem.store(content, category="engineering", tier=StorageTier.SEMANTIC, session_id=f"s_{i}")

        mem.store(needle_end, category="limits", tier=StorageTier.SEMANTIC, session_id="s_end")

        stored_total_tokens = sum(count_tokens(m.content) for m in mem.db.list_memories())
        assert stored_total_tokens >= scale_tokens * 0.85

        for label, query, expected in [
            ("start_0pct", "What is the Project Orion emergency failover endpoint?", "orion-start-771.internal:9443"),
            ("middle_50pct", "Where is the Project Zephyr signing key rotation vault?", "zephyr-mid-404.vault.local"),
            ("end_100pct", "What is the Project Hyperion rate limiter circuit threshold?", "hyperion-end-882"),
        ]:
            res = mem.recall(query, token_budget=400)
            assert not res.is_abstention, f"Failed recall at {scale_tokens} tokens ({label})"
            assert expected in res.context_text, (
                f"Missed {label} needle at corpus size {stored_total_tokens} tokens! Got: {res.context_text}"
            )
            assert count_tokens(res.context_text) <= 400
            assert res.tokens_used == count_tokens(res.context_text)
            assert res.latency_ms < 250.0, f"Latency too high at {scale_tokens} ({label}): {res.latency_ms} ms"

        mem.close()


def test_chroma_distractor_flood_and_candidate_pool():
    """
    Tests Chroma Context Rot resistance when 120 topically similar short distractors
    share overlapping words with the target needle (and previously crowded out top_k=10).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "distractors.db")

        for i in range(60):
            mem.store(
                content=f"Database query latency note {i}: billing optimizer review pending.",
                category="notes",
                tier=StorageTier.SEMANTIC,
                session_id=f"d_{i}",
            )

        mem.store(
            content="Verified specification: billing database query optimizer timeout threshold is exactly 4250ms.",
            category="spec",
            tier=StorageTier.SEMANTIC,
            session_id="s_needle",
        )

        for i in range(60, 120):
            mem.store(
                content=f"Database query audit {i}: billing metrics latency summary only.",
                category="notes",
                tier=StorageTier.SEMANTIC,
                session_id=f"d_{i}",
            )

        res = mem.recall(
            "What is the billing database query optimizer timeout threshold?",
            token_budget=180,
        )
        assert not res.is_abstention
        assert "4250ms" in res.context_text, f"Needle drowned by distractors! Got: {res.context_text}"
        assert count_tokens(res.context_text) <= 180
        mem.close()


def test_nolima_zero_lexical_overlap_vs_relational_tier_routing():
    """
    Tests the documented NoLiMa zero-lexical-overlap boundary:
    - Offline semantic hashing deliberately abstains rather than guessing when query/doc share zero stemmed terms.
    - Tier 1 RELATIONAL entity-attribute lookup bridges zero-overlap queries deterministically in sub-ms latency.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        mem = LetheMemory(db_path=Path(tmpdir) / "nolima.db")

        mem.store(
            content="Candidate holds an MD and practices cardiology in Vienna.",
            category="profile",
            tier=StorageTier.RELATIONAL,
            metadata={"entity": "candidate", "attribute": "work_city", "value": "Vienna"},
        )

        # Pure semantic paraphrase with zero lexical overlap triggers honest abstention
        res_sem = mem.recall("Which European municipality does the physician live inside?", token_budget=200)
        assert res_sem.is_abstention is True

        # Exact Tier 1 Relational routing retrieves Vienna deterministically with zero hallucination
        res_rel = mem.recall(
            "Which European municipality does the physician live inside?",
            token_budget=200,
            entity_filter="candidate.work_city",
        )
        assert not res_rel.is_abstention
        assert "Vienna" in res_rel.context_text
        assert count_tokens(res_rel.context_text) <= 200
        mem.close()
