from typing import List, Tuple
from lethe.models import MemoryNode

try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        return len(_ENCODER.encode(text))
except Exception:
    def count_tokens(text: str) -> int:
        # Fallback approximation: 1 token ≈ 4 characters
        return max(1, len(text) // 4)

class KnapsackPacker:
    """
    Greedy knapsack token budget packing algorithm.
    Guarantees that retrieved memories fit strictly within `token_budget`
    while maximizing information utility (Relevance * Trust / Cost).
    """
    def __init__(self):
        pass

    def count_memory_tokens(self, node: MemoryNode) -> int:
        formatted = f"[{node.category.upper()}] {node.content} (Trust: {node.trust_level:.1f})"
        return count_tokens(formatted)

    def pack(
        self,
        candidates_with_scores: List[Tuple[MemoryNode, float]],
        token_budget: int
    ) -> Tuple[List[MemoryNode], int]:
        """
        Packs candidate memories into token_budget.
        Returns (packed_nodes, total_tokens).
        """
        if token_budget <= 0 or not candidates_with_scores:
            return [], 0

        # Calculate cost and utility density for each candidate
        items = []
        for node, relevance in candidates_with_scores:
            cost = self.count_memory_tokens(node)
            utility = relevance * float(node.trust_level)
            density = utility / max(1, cost)
            items.append({
                "node": node,
                "cost": cost,
                "utility": utility,
                "density": density
            })

        # Sort by utility density descending (Greedy Knapsack)
        items.sort(key=lambda x: x["density"], reverse=True)

        selected: List[MemoryNode] = []
        used_tokens = 0

        for item in items:
            if used_tokens + item["cost"] <= token_budget:
                selected.append(item["node"])
                used_tokens += item["cost"]

        return selected, used_tokens
