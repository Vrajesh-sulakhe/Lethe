import re
import sqlite3
from typing import Any, Callable, List, Optional, Tuple

from lethe.models import MemoryNode, MemoryStatus

FTS_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "have", "has", "had", "what", "which", "who", "whom",
    "where", "when", "why", "how", "i", "me", "my", "we", "our", "you", "your",
    "it", "its", "this", "that", "these", "those", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "and", "or", "not", "can", "could", "would",
    "should", "please", "about", "tell", "give", "show",
})

_EXCLUDED_FTS_STATUSES = (
    MemoryStatus.TOMBSTONED.value,
    MemoryStatus.QUARANTINED.value,
)


def build_fts_match_expression(query: str) -> str:
    """Builds a safe FTS5 MATCH expression with quoted tokens and OR semantics."""
    cleaned = re.sub(r"'(?:s|t|re|ve|ll|d|m)\b", "", (query or "").lower())
    tokens = re.findall(r"[a-zA-Z0-9_]+", cleaned)
    if not tokens:
        return ""

    filtered = [tok for tok in tokens if tok not in FTS_STOP_WORDS and len(tok) > 1]
    selected = filtered or [tok for tok in tokens if len(tok) > 0]
    deduped = list(dict.fromkeys(selected))
    if not deduped:
        return ""

    return " OR ".join(f'"{tok}"' for tok in deduped[:24])


def ensure_fts_table(cursor: sqlite3.Cursor) -> bool:
    """Creates and backfills the FTS5 virtual table if SQLite supports FTS5."""
    try:
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        )
        if cursor.fetchone() is not None:
            return True

        cursor.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
            "USING fts5(memory_id UNINDEXED, content, category, tokenize='porter unicode61');"
        )
        cursor.execute(
            """
            INSERT INTO memories_fts (memory_id, content, category)
            SELECT id, content, category FROM memories
            WHERE status NOT IN (?, ?);
            """,
            _EXCLUDED_FTS_STATUSES,
        )
        return True
    except sqlite3.OperationalError:
        return False


def searchable_memory_text(node: MemoryNode) -> str:
    parts = [node.content, node.workspace_id or ""]
    if node.code_anchor:
        parts.append(node.code_anchor.file_path)
        if node.code_anchor.file_path.lower().endswith((".html", ".htm")):
            parts.append("HTML page")
    for key in ("project_name", "artifact_kind", "file_path"):
        value = node.metadata.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def sync_fts_node(cursor: sqlite3.Cursor, node: MemoryNode, enabled: bool) -> None:
    """Keeps a single MemoryNode synchronized in the FTS5 index."""
    if not enabled:
        return
    cursor.execute("DELETE FROM memories_fts WHERE memory_id = ?", (node.id,))
    status_val = node.status.value if isinstance(node.status, MemoryStatus) else str(node.status)
    if status_val in _EXCLUDED_FTS_STATUSES:
        return
    cursor.execute(
        "INSERT INTO memories_fts (memory_id, content, category) VALUES (?, ?, ?)",
        (node.id, searchable_memory_text(node), node.category),
    )


def delete_fts_entries(cursor: sqlite3.Cursor, memory_ids: List[str], enabled: bool) -> None:
    """Removes purged, deleted, or quarantined IDs from the FTS5 index."""
    if not enabled or not memory_ids:
        return
    cleaned = [str(mid) for mid in memory_ids if mid]
    if not cleaned:
        return
    placeholders = ",".join("?" for _ in cleaned)
    cursor.execute(f"DELETE FROM memories_fts WHERE memory_id IN ({placeholders})", cleaned)


def delete_fts_for_branch(
    cursor: sqlite3.Cursor,
    branch: str,
    include_tombstoned: bool,
    enabled: bool,
) -> None:
    """Removes branch-scoped entries from FTS5 prior to deleting memory rows."""
    if not enabled:
        return
    if include_tombstoned:
        cursor.execute(
            "DELETE FROM memories_fts WHERE memory_id IN (SELECT id FROM memories WHERE branch = ?)",
            (branch,),
        )
        return
    cursor.execute(
        """
        DELETE FROM memories_fts
        WHERE memory_id IN (SELECT id FROM memories WHERE branch = ? AND status != ?)
        """,
        (branch, MemoryStatus.TOMBSTONED.value),
    )


def query_fts_memories(
    cursor: sqlite3.Cursor,
    query: str,
    row_mapper: Callable[[sqlite3.Row], MemoryNode],
    enabled: bool,
    limit: int = 50,
    workspace_id: Optional[str] = None,
    as_of_time: Optional[float] = None,
    is_trusted: bool = True,
    branch: Optional[str] = "main",
    category: Optional[str] = None,
    session_id: Optional[str] = None,
    include_ephemeral: bool = True,
) -> List[Tuple[MemoryNode, float]]:
    """Executes a scoped FTS5 BM25 search with strict workspace/branch/status filters."""
    if not enabled:
        return []

    match_expr = build_fts_match_expression(query)
    if not match_expr:
        return []

    sql = """
        SELECT m.*, bm25(memories_fts) AS fts_rank
        FROM memories_fts
        JOIN memories m ON m.id = memories_fts.memory_id
        WHERE memories_fts MATCH ?
    """
    params: List[Any] = [match_expr]

    if as_of_time is not None:
        sql += " AND m.status IN ('active', 'conflict', 'historical') AND m.valid_from <= ? AND (m.valid_to IS NULL OR m.valid_to > ?)"
        params.extend([as_of_time, as_of_time])
    else:
        sql += " AND m.status IN ('active', 'conflict')"

    if branch is not None:
        sql += " AND m.branch = ?"
        params.append(branch)

    if category:
        sql += " AND m.category = ?"
        params.append(category)

    if not include_ephemeral:
        if session_id:
            sql += " AND (m.tier != 'ephemeral' OR m.source_session = ?)"
            params.append(session_id)
        else:
            sql += " AND m.tier != 'ephemeral'"

    if workspace_id:
        if is_trusted:
            sql += " AND (m.workspace_id = ? OR m.scope = 'global' OR m.workspace_id IS NULL)"
            params.append(workspace_id)
        else:
            sql += " AND m.workspace_id = ? AND m.scope != 'global'"
            params.append(workspace_id)

    sql += " ORDER BY fts_rank ASC, m.created_at DESC LIMIT ?"
    params.append(max(1, int(limit)))

    try:
        cursor.execute(sql, params)
    except sqlite3.OperationalError:
        return []

    results: List[Tuple[MemoryNode, float]] = []
    for row in cursor.fetchall():
        abs_rank = abs(float(row["fts_rank"]))
        # Keep score <= 0.95 so epistemic term coverage gate remains strictly enforced
        normalized_score = min(0.95, 0.25 + 0.65 * (abs_rank / (1.0 + abs_rank)))
        results.append((row_mapper(row), normalized_score))
    return results
