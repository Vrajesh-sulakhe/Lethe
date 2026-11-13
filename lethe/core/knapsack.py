from typing import Callable, Dict, List, Optional, Tuple
from lethe.models import MemoryNode

try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return len(_ENCODER.encode(text))

    def truncate_to_tokens(text: str, max_tokens: int, suffix: str = " ... [truncated]") -> str:
        if max_tokens <= 0 or not text:
            return ""
        tokens = _ENCODER.encode(text)
        if len(tokens) <= max_tokens:
            return text
        suffix_tokens = _ENCODER.encode(suffix) if suffix else []
        if max_tokens <= len(suffix_tokens):
            out = _ENCODER.decode(tokens[:max_tokens])
            while count_tokens(out) > max_tokens and max_tokens > 0:
                max_tokens -= 1
                out = _ENCODER.decode(tokens[:max_tokens])
            return out
        keep = max_tokens - len(suffix_tokens)
        out = _ENCODER.decode(tokens[:keep]) + suffix
        while count_tokens(out) > max_tokens and keep > 0:
            keep -= 1
            out = _ENCODER.decode(tokens[:keep]) + suffix
        return out
except Exception:
    _ENCODER = None

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    def truncate_to_tokens(text: str, max_tokens: int, suffix: str = " ... [truncated]") -> str:
        if max_tokens <= 0 or not text:
            return ""
        if count_tokens(text) <= max_tokens:
            return text
        char_budget = max(0, max_tokens * 4 - len(suffix))
        if char_budget <= 0:
            return text[: max_tokens * 4]
        return text[:char_budget].rstrip() + suffix


MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "tiny-4k": 4096,
    "local-8k": 8192,
    "llama-3-8b": 8192,
    "gpt-4-8k": 8192,
    "mistral-32k": 32768,
    "gpt-4o": 128000,
    "gpt-4-turbo": 128000,
    "o1": 200000,
    "o3": 200000,
    "codex": 192000,
    "claude-3-5-sonnet": 200000,
    "claude-sonnet-4": 200000,
    "claude-opus-4": 200000,
    "claude-code": 200000,
    "cursor": 200000,
    "gemini-2.5-pro": 1048576,
    "gemini-2.5-flash": 1048576,
    "antigravity": 1048576,
}

# Research-backed slice ceiling (NoLiMa / RULER / Chroma Context Rot show
# sharp accuracy decay past 4K-32K tokens, especially with distractors).
SAFE_SLICE_CEILING_TOKENS = 4096


def resolve_model_context_window(model_name: Optional[str]) -> Optional[int]:
    """Resolves advertised context window size from canonical or partial model name."""
    if not model_name:
        return None
    norm = model_name.strip().lower()
    if norm in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[norm]
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if norm.startswith(key) or key in norm:
            return window
    return None


def compute_effective_token_budget(
    token_budget: int = 500,
    model_name: Optional[str] = None,
    max_context_tokens: Optional[int] = None,
    remaining_context_tokens: Optional[int] = None,
) -> int:
    """
    Computes a safe memory slice budget bounded by caller budget, target model
    context window (max 25% of window and capped at SAFE_SLICE_CEILING_TOKENS
    when model-governed), and remaining available prompt tokens.
    """
    budget = max(0, int(token_budget))
    window = max_context_tokens if max_context_tokens is not None else resolve_model_context_window(model_name)

    if window is not None:
        if window <= 0:
            return 0
        window_cap = min(max(1, int(window * 0.25)), SAFE_SLICE_CEILING_TOKENS)
        budget = min(budget, window_cap)

    if remaining_context_tokens is not None:
        budget = min(budget, max(0, int(remaining_context_tokens)))

    return budget


def format_citation_line(
    node: MemoryNode,
    compact: bool = False,
    drift_warning: Optional[str] = None,
) -> str:
    """Canonical single-line provenance citation format shared between KnapsackPacker and LetheMemory."""
    warning_suffix = f" [DRIFT WARNING: {drift_warning}]" if drift_warning else ""
    if compact:
        return f"[#{node.id[:6]}|{node.category}|{node.trust_level:.1f}] {node.content}{warning_suffix}"
    citation = (
        f"[Memory #{node.id[:6]} | Category: {node.category} | "
        f"Source: {node.source_session} ({node.source_origin}) | Trust: {node.trust_level:.1f}]"
    )
    return f"{citation} {node.content}{warning_suffix}"


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
        token_budget: int,
        formatter: Optional[Callable[[MemoryNode], str]] = None,
        reserved_tokens: int = 0,
    ) -> Tuple[List[MemoryNode], int]:
        """
        Packs candidate memories into token_budget.
        When `formatter` is supplied, validates exact rendered BPE token counts
        after line-joining and reserved overhead (e.g. conflict warnings).
        Returns (packed_nodes, total_tokens).
        """
        if token_budget <= 0 or not candidates_with_scores:
            return [], 0

        if formatter is None and token_budget - max(0, reserved_tokens) <= 0:
            return [], 0

        items = []
        for node, relevance in candidates_with_scores:
            rendered = formatter(node) if formatter is not None else None
            cost = count_tokens(rendered) if rendered is not None else self.count_memory_tokens(node)
            utility = float(relevance) * float(node.trust_level)
            density = utility / max(1, cost)
            items.append({
                "node": node,
                "rendered": rendered,
                "cost": cost,
                "utility": utility,
                "density": density,
            })

        # Sort by utility density descending (Greedy Knapsack)
        items.sort(key=lambda x: x["density"], reverse=True)

        selected: List[MemoryNode] = []
        if formatter is None:
            used_tokens = 0
            available = token_budget - max(0, reserved_tokens)
            for item in items:
                if used_tokens + item["cost"] <= available:
                    selected.append(item["node"])
                    used_tokens += item["cost"]
            return selected, used_tokens

        # Exact BPE-checked rendering when formatter is used
        rendered_lines: List[str] = []
        effective_limit = token_budget - max(0, reserved_tokens)
        if effective_limit <= 0:
            return [], 0

        used_tokens = 0
        for item in items:
            candidate_lines = rendered_lines + [item["rendered"]]
            candidate_tokens = count_tokens("\n".join(candidate_lines))
            if candidate_tokens <= effective_limit:
                selected.append(item["node"])
                rendered_lines = candidate_lines
                used_tokens = candidate_tokens

        return selected, used_tokens
