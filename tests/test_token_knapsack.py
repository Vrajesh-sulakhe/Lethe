import pytest
from lethe.models import MemoryNode, StorageTier, MemoryStatus
from lethe.core.knapsack import KnapsackPacker

def test_knapsack_strict_token_budget_packing():
    packer = KnapsackPacker()

    memories_with_scores = [
        (
            MemoryNode(
                id="m1",
                content="User lives in Bengaluru and works as a senior software architect.",
                category="profile",
                tier=StorageTier.SEMANTIC,
                trust_level=1.0,
                status=MemoryStatus.ACTIVE
            ),
            0.95  # high relevance
        ),
        (
            MemoryNode(
                id="m2",
                content="The weather today is cloudy with occasional light rain showers in the southern district.",
                category="weather",
                tier=StorageTier.SEMANTIC,
                trust_level=0.5,
                status=MemoryStatus.ACTIVE
            ),
            0.40  # low relevance
        ),
        (
            MemoryNode(
                id="m3",
                content="User dog name is Bruno.",
                category="pets",
                tier=StorageTier.RELATIONAL,
                trust_level=1.0,
                status=MemoryStatus.ACTIVE
            ),
            0.80  # high relevance, very short
        ),
    ]

    # Test small budget: 25 tokens
    packed_small, tokens_small = packer.pack(memories_with_scores, token_budget=25)
    assert tokens_small <= 25
    assert len(packed_small) >= 1
    # m3 (dog Bruno) or m1 should fit
    assert any(m.id == "m3" for m in packed_small)

    # Test larger budget: 45 tokens
    packed_med, tokens_med = packer.pack(memories_with_scores, token_budget=45)
    assert tokens_med <= 45
    assert len(packed_med) >= 2
    # m1 and m3 fit within 45 tokens, m2 is excluded
    assert any(m.id == "m1" for m in packed_med)
    assert any(m.id == "m3" for m in packed_med)

    # Test huge budget: 1000 tokens
    packed_all, tokens_all = packer.pack(memories_with_scores, token_budget=1000)
    assert len(packed_all) == 3
    assert tokens_all <= 1000
