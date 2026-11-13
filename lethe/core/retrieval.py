import re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from lethe.core.drift import check_code_drift
from lethe.core.knapsack import (
    KnapsackPacker,
    compute_effective_token_budget,
    count_tokens,
    format_citation_line,
)
from lethe.models import MemoryNode, MemoryStatus, StorageTier
from lethe.storage.tiers import EmbeddingHelper

CORE_IDENTITY_CATEGORIES = frozenset({
    "user_identity",
    "identity",
    "user_profile",
    "profile",
    "bio",
})

GLOBAL_PERSONAL_CATEGORIES = frozenset({
    "user_identity",
    "identity",
    "user_profile",
    "profile",
    "bio",
    "preferences",
    "preference",
    "personal",
})

_CONVERSATIONAL_PREFIXES = (
    "lethe_recall",
    "lethe recall",
    "lethe",
    "hey",
    "hi",
    "hello",
    "please",
    "pls",
    "can you",
    "could you",
    "would you",
    "tell me",
    "check",
    "recall",
    "remember",
    "find",
    "show me",
    "give me",
    "summarize",
    "list",
)

_BROAD_IDENTITY_PATTERNS = re.compile(
    r"^(?:what do you know about me|what all do you know about me|"
    r"tell me about (?:me|myself)|about me|my profile|user profile|"
    r"who i am and what i do|everything you know about me)$"
)

_STRICT_IDENTITY_PATTERNS = re.compile(
    r"^(?:who am i|who i am|what(?:'s| is)? my name|whats my name|my name|"
    r"do you know (?:who i am|my name)|"
    r"what(?:'s| is)? my (?:username|handle|user name|identity)|"
    r"my (?:username|handle|identity)|user identity|who is (?:the )?user)$"
)

_BROWSE_FILLER_WORDS = frozenset({
    "a", "an", "the", "my", "our", "your", "me", "we", "you", "i", "it",
    "this", "that", "these", "those", "is", "are", "was", "were", "do",
    "did", "have", "has", "can", "could", "would", "what", "which", "who",
    "any", "in", "on", "at", "of", "for", "from", "with", "about", "to",
    "here", "there", "please", "just", "currently", "stored", "saved",
    "known", "know", "active", "context", "session", "workspace", "project",
    "lethe", "memories", "memory", "recent", "recently", "latest", "newest",
    "last", "all", "everything", "anything", "overview", "summary", "list",
    "show", "give", "tell", "remember", "recall", "updates", "history",
})

_STRONG_BROWSE_CUES = frozenset({
    "recent", "recently", "latest", "newest", "memories", "memory",
    "everything", "overview", "remember", "recall", "stored", "saved",
    "summary", "list", "history", "context", "updates", "all",
})

_SELF_IDENTITY_MARKERS = (
    "user's name",
    "users name",
    "my name is",
    "username",
    "handle:",
    "user is ",
)

_PROFILE_USER_RE = re.compile(
    r"\b(?:user|user's|users|my|me|i'm|i\s+am|username|handle)\b"
)

_GENERIC_QUERY_WORDS = frozenset({
    "number", "name", "info", "information", "details", "detail",
    "secret", "give", "tell", "show", "my", "the",
})


class RecallIntent(str, Enum):
    USER_IDENTITY = "user_identity"
    BROWSE_RECENT = "browse_recent"
    SPECIFIC_SEARCH = "specific_search"


def _strip_prefixes(text: str) -> str:
    current = text
    changed = True
    while changed and current:
        changed = False
        for prefix in _CONVERSATIONAL_PREFIXES:
            if current.startswith(prefix + " "):
                current = current[len(prefix) + 1:].strip()
                changed = True
                break
    return current


def classify_recall_intent(
    query: str,
    entity_filter: Optional[str] = None,
) -> Tuple[RecallIntent, bool]:
    """Classifies recall query intent into identity, browse-recent, or specific search."""
    if entity_filter:
        return RecallIntent.SPECIFIC_SEARCH, False

    raw = (query or "").strip()
    if not raw or raw in {"*", "?"}:
        return RecallIntent.BROWSE_RECENT, False

    cleaned = re.sub(r"[^a-z0-9_'\s]+", " ", raw.lower()).strip()
    norm = " ".join(cleaned.split())
    if not norm:
        return RecallIntent.BROWSE_RECENT, False

    stripped = _strip_prefixes(norm)
    for candidate in (norm, stripped):
        if _BROAD_IDENTITY_PATTERNS.match(candidate):
            return RecallIntent.USER_IDENTITY, True
        if _STRICT_IDENTITY_PATTERNS.match(candidate):
            return RecallIntent.USER_IDENTITY, False

    tokens = re.findall(r"[a-z0-9_]+", norm)
    has_browse_cue = any(tok in _STRONG_BROWSE_CUES for tok in tokens)
    specific_tokens = [tok for tok in tokens if tok not in _BROWSE_FILLER_WORDS]
    if has_browse_cue and not specific_tokens:
        return RecallIntent.BROWSE_RECENT, False

    return RecallIntent.SPECIFIC_SEARCH, False


def default_scope_for_category(category: str, explicit_scope: Optional[str]) -> str:
    """Defaults personal and identity categories to global scope unless explicitly set."""
    if explicit_scope is not None:
        return explicit_scope
    if (category or "").strip().lower() in GLOBAL_PERSONAL_CATEGORIES:
        return "global"
    return "project"


def is_visible_memory(
    node: MemoryNode,
    target_ws: Optional[str],
    security: Any,
    as_of_time: Optional[float],
    session_id: Optional[str],
    include_ephemeral: bool,
) -> bool:
    """Enforces status, bitemporal window, ephemeral session, and workspace trust rules."""
    allowed_statuses = (
        (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT, MemoryStatus.HISTORICAL)
        if as_of_time is not None
        else (MemoryStatus.ACTIVE, MemoryStatus.CONFLICT)
    )
    if node.status not in allowed_statuses:
        return False

    if as_of_time is not None:
        if node.valid_from > as_of_time:
            return False
        if node.valid_to is not None and node.valid_to <= as_of_time:
            return False

    if node.tier == StorageTier.EPHEMERAL and not include_ephemeral:
        if not session_id or node.source_session != session_id:
            return False

    return bool(security.validate_read(target_ws, node))


def _identity_match_score(node: MemoryNode, broad_profile: bool) -> float:
    cat = (node.category or "").strip().lower()
    content_low = node.content.lower()

    if cat in {"user_identity", "identity", "user_profile"}:
        return 1.0
    if any(marker in content_low for marker in _SELF_IDENTITY_MARKERS):
        return 0.96
    if cat in {"profile", "bio"} and _PROFILE_USER_RE.search(content_low):
        return 0.92
    if not broad_profile:
        return 0.0
    if cat in {"preferences", "preference", "personal"}:
        return 0.88
    if node.scope == "global" and cat not in {"profile", "bio"}:
        return 0.78
    return 0.0


def retrieve_identity_candidates(
    db: Any,
    security: Any,
    target_ws: Optional[str],
    target_branch: str,
    is_trusted: bool,
    as_of_time: Optional[float],
    session_id: Optional[str],
    include_ephemeral: bool,
    broad_profile: bool,
) -> List[Tuple[MemoryNode, float]]:
    """Retrieves user identity and personal profile memories safely across tools."""
    nodes = db.list_memories(
        as_of_time=as_of_time,
        workspace_id=target_ws,
        is_trusted=is_trusted,
        branch=target_branch,
        order_desc=True,
        limit=200,
    )
    scored: List[Tuple[MemoryNode, float]] = []
    for node in nodes:
        if not is_visible_memory(node, target_ws, security, as_of_time, session_id, include_ephemeral):
            continue
        score = _identity_match_score(node, broad_profile=broad_profile)
        if score > 0.0:
            scored.append((node, score))

    scored.sort(key=lambda pair: (pair[1], pair[0].created_at), reverse=True)
    return scored


def retrieve_browse_candidates(
    db: Any,
    security: Any,
    target_ws: Optional[str],
    target_branch: str,
    is_trusted: bool,
    as_of_time: Optional[float],
    session_id: Optional[str],
    include_ephemeral: bool,
    limit: int = 80,
) -> List[Tuple[MemoryNode, float]]:
    """Retrieves newest accessible memories ranked cleanly by recency."""
    nodes = db.list_memories(
        as_of_time=as_of_time,
        workspace_id=target_ws,
        is_trusted=is_trusted,
        branch=target_branch,
        order_desc=True,
        limit=max(20, limit),
    )
    visible = [
        node for node in nodes
        if is_visible_memory(node, target_ws, security, as_of_time, session_id, include_ephemeral)
    ]
    return [
        (node, max(0.50, 1.0 - idx * 0.005))
        for idx, node in enumerate(visible)
    ]


def _extract_stemmed_tokens(text: str) -> List[str]:
    cleaned = re.sub(r"'(?:s|t|re|ve|ll|d|m)\b", "", (text or "").lower())
    return [
        EmbeddingHelper._stem(w)
        for w in re.findall(r"\b[a-zA-Z0-9_]+\b", cleaned)
    ]


def _calculate_term_coverage(specific_q_words: List[str], node: MemoryNode) -> float:
    if not specific_q_words:
        return 1.0
    doc_tokens = set(_extract_stemmed_tokens(f"[{node.category}] {node.content}"))
    matched = [
        w for w in specific_q_words
        if w in doc_tokens
        or (len(w) >= 4 and any(dt.startswith(w) or w.startswith(dt) for dt in doc_tokens if len(dt) >= 4))
    ]
    return len(matched) / len(specific_q_words)


def retrieve_specific_candidates(
    query: str,
    db: Any,
    bitemporal: Any,
    semantic: Any,
    security: Any,
    target_ws: Optional[str],
    target_branch: str,
    is_trusted: bool,
    effective_budget: int,
    min_relevance: float,
    as_of_time: Optional[float],
    entity_filter: Optional[str],
    session_id: Optional[str],
    include_ephemeral: bool,
) -> List[Tuple[MemoryNode, float]]:
    """Runs hybrid relational + semantic + FTS5 retrieval with epistemic term coverage gate."""
    merged: Dict[str, Tuple[MemoryNode, float]] = {}

    if entity_filter:
        parts = entity_filter.split(".")
        if len(parts) == 2:
            exact_match = bitemporal.query_active_attribute(
                parts[0],
                parts[1],
                as_of_time=as_of_time,
                workspace_id=target_ws,
                is_trusted=is_trusted,
                branch=target_branch,
            )
            if exact_match:
                merged[exact_match.id] = (exact_match, 1.0)

    search_k = max(40, min(200, effective_budget // 10 + 30))
    semantic_matches = semantic.search(
        query,
        top_k=search_k,
        min_score=0.0,
        workspace_id=target_ws,
        session_id=session_id,
        include_ephemeral=include_ephemeral,
        as_of_time=as_of_time,
        is_trusted=is_trusted,
        branch=target_branch,
    )
    for node, score in semantic_matches:
        if node.id not in merged or score > merged[node.id][1]:
            merged[node.id] = (node, score)

    for node, fts_score in db.search_fts(
        query,
        limit=search_k,
        workspace_id=target_ws,
        as_of_time=as_of_time,
        is_trusted=is_trusted,
        branch=target_branch,
    ):
        if node.id not in merged or fts_score > merged[node.id][1]:
            merged[node.id] = (node, fts_score)

    q_clean = re.sub(r"'(?:s|t|re|ve|ll|d|m)\b", "", query.lower())
    q_words = re.findall(r"\b[a-zA-Z0-9_]+\b", q_clean)
    specific_q_words = [
        EmbeddingHelper._stem(w)
        for w in q_words
        if w not in EmbeddingHelper.STOP_WORDS
        and w not in _GENERIC_QUERY_WORDS
        and (len(w) > 1 or w.isdigit())
    ]
    if not specific_q_words:
        specific_q_words = [
            EmbeddingHelper._stem(w)
            for w in q_words
            if w not in EmbeddingHelper.STOP_WORDS and (len(w) > 1 or w.isdigit())
        ]

    qualified: List[Tuple[MemoryNode, float]] = []
    for node, score in merged.values():
        if not is_visible_memory(node, target_ws, security, as_of_time, session_id, include_ephemeral):
            continue
        cov = _calculate_term_coverage(specific_q_words, node)
        if (score >= 0.99 or cov >= 0.30) and score >= min_relevance:
            combined = score if score >= 0.99 else (0.65 * score + 0.35 * cov)
            qualified.append((node, combined))

    return qualified


def assemble_session_context(
    db: Any,
    security: Any,
    packer: KnapsackPacker,
    target_ws: Optional[str],
    target_branch: str,
    token_budget: int,
    model_name: Optional[str] = None,
    remaining_context_tokens: Optional[int] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Packs global identity memories and recent workspace context within BPE budget."""
    is_trusted = security.is_workspace_trusted(target_ws)
    effective_budget = compute_effective_token_budget(
        token_budget,
        model_name=model_name,
        remaining_context_tokens=remaining_context_tokens,
    )
    nodes = db.list_memories(
        workspace_id=target_ws,
        is_trusted=is_trusted,
        branch=target_branch,
        order_desc=True,
        limit=120,
    )
    visible = [
        n for n in nodes
        if is_visible_memory(n, target_ws, security, as_of_time=None, session_id=session_id, include_ephemeral=False)
    ]

    candidates: List[Tuple[MemoryNode, float]] = []
    for idx, node in enumerate(visible):
        is_identity = _identity_match_score(node, broad_profile=True) >= 0.88
        score = (1.0 - idx * 0.001) if is_identity else max(0.55, 0.90 - idx * 0.003)
        candidates.append((node, score))

    drift_map: Dict[str, str] = {}
    drift_warnings: List[str] = []
    for node, _ in candidates:
        if node.code_anchor:
            is_drifted, reason = check_code_drift(node.code_anchor, workspace_dir=target_ws)
            if is_drifted and reason:
                drift_map[node.id] = reason
                drift_warnings.append(f"Memory #{node.id[:6]}: {reason}")

    packed, _ = packer.pack(
        candidates,
        effective_budget,
        formatter=lambda n: format_citation_line(n, compact=False, drift_warning=drift_map.get(n.id)),
    )
    compact = False
    if not packed and candidates:
        packed, _ = packer.pack(
            candidates,
            effective_budget,
            formatter=lambda n: format_citation_line(n, compact=True, drift_warning=drift_map.get(n.id)),
        )
        compact = bool(packed)

    lines = [
        format_citation_line(m, compact=compact, drift_warning=drift_map.get(m.id))
        for m in packed
    ]
    while lines and count_tokens("\n".join(lines)) > effective_budget:
        lines.pop()
        packed.pop()

    context_text = "\n".join(lines)
    identity_items = [
        {"id": m.id, "category": m.category, "scope": m.scope, "content": m.content}
        for m in packed
        if _identity_match_score(m, broad_profile=True) >= 0.88
    ]
    recent_items = [
        {"id": m.id, "category": m.category, "scope": m.scope, "content": m.content}
        for m in packed
    ]
    return {
        "effective_token_budget": effective_budget,
        "tokens_used": count_tokens(context_text),
        "identity_memories": identity_items,
        "recent_memories": recent_items,
        "context_text": context_text,
        "drift_warnings": drift_warnings,
    }
