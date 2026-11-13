"""Persist project roots separately from human-readable workspace identifiers."""
import hashlib
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any, Optional


def ensure_project_schema(conn: Any) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspace_roots (
            root TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            display_name TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_roots_id ON workspace_roots(workspace_id)")


def local_absolute_path(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    if os.name != "nt" and PureWindowsPath(value).is_absolute():
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else None


def register_project(db: Any, root: str, workspace_id: Optional[str] = None) -> str:
    path = local_absolute_path(root)
    if path is None or not path.is_dir():
        raise ValueError("workspace_root must be an existing absolute local directory")
    if path == Path.home().resolve() or path == path.parent:
        raise ValueError("Choose a project directory, not a home or filesystem root")
    normalized = os.path.normcase(str(path))
    with db._lock:
        ensure_project_schema(db.conn)
        db.conn.commit()
        db.conn.execute("BEGIN IMMEDIATE")
        try:
            row = db.conn.execute("SELECT workspace_id FROM workspace_roots WHERE root = ?", (normalized,)).fetchone()
            if row:
                if workspace_id and row[0] != workspace_id:
                    raise ValueError("This root is already registered under a different workspace")
                db.conn.commit()
                return row[0]
            name = workspace_id or path.name
            if not name or name == "global_desktop":
                raise ValueError("A project requires a non-global workspace identifier")
            collision = db.conn.execute("SELECT root FROM workspace_roots WHERE workspace_id = ? COLLATE NOCASE", (name,)).fetchone()
            if collision:
                if workspace_id:
                    raise ValueError("Workspace identifier already belongs to a different root")
                name = f"{name}-{hashlib.sha256(normalized.encode()).hexdigest()[:8]}"
            db.conn.execute("INSERT INTO workspace_roots(root, workspace_id, display_name) VALUES (?, ?, ?)", (normalized, name, path.name))
            db.conn.commit()
            return name
        except Exception:
            db.conn.rollback()
            raise


def project_root(db: Any, workspace_id: Optional[str]) -> Optional[str]:
    if not workspace_id:
        return None
    direct = local_absolute_path(workspace_id)
    if direct is not None:
        return str(direct)
    with db._lock:
        ensure_project_schema(db.conn)
        rows = db.conn.execute("SELECT root FROM workspace_roots WHERE workspace_id = ? COLLATE NOCASE", (workspace_id,)).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def visible_projects(memory: Any, requester: Optional[str] = None) -> list[dict]:
    requester = requester or memory.workspace_id
    with memory.db._lock:
        ensure_project_schema(memory.db.conn)
        roots = {r["workspace_id"]: dict(r) for r in memory.db.conn.execute("SELECT * FROM workspace_roots")}
        names = set(memory.db.list_workspaces()) | set(roots)
    projects = []
    for name in sorted(names):
        if name == "global_desktop":
            continue
        allowed = name == requester or (
            requester == "global_desktop"
            and memory.security.is_workspace_trusted(requester)
            and memory.security.is_workspace_trusted(name)
        )
        if allowed:
            projects.append({"workspace_id": name, "display_name": roots.get(name, {}).get("display_name", name), "workspace_root": roots.get(name, {}).get("root")})
    return projects


def resolve_visible_project(memory: Any, name: str, requester: str) -> tuple[Optional[str], list[str]]:
    """Resolve an explicit project selector against requester's visible projects."""
    target = (name or "").strip().lower()
    if not target:
        return None, []
    matches = sorted({
        p["workspace_id"]
        for p in visible_projects(memory, requester)
        if p["workspace_id"].lower() == target or p["display_name"].lower() == target
    })
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return None, matches
    return None, []


def resolve_project_query(memory: Any, query: str, requester: str) -> tuple[str, str, list[str]]:
    """Match whole project names without allowing query text to expand read permissions."""
    matches: dict[str, tuple[str, str]] = {}
    for project in visible_projects(memory, requester):
        names = {project["workspace_id"], project["display_name"]}
        for name in names:
            pattern = rf"(?<![\w.-]){re.escape(name)}(?![\w.-])"
            # 'lethe' is also the tool's invocation prefix, not a project selector.
            if name.lower() == "lethe" and re.match(r"^\s*/?lethe\b", query, re.I):
                continue
            if re.search(pattern, query, re.I):
                matches[project["workspace_id"]] = (pattern, name)
    if len(matches) != 1:
        return requester, query, sorted(matches) if len(matches) > 1 else []
    selected, (pattern, _) = next(iter(matches.items()))
    stripped = re.sub(pattern, " ", query, flags=re.I)
    stripped = re.sub(r"^\s*(?:lethe\s+)?(?:recall\s+)?(?:project\s+|workspace\s+|for\s+)?", "", stripped, flags=re.I).strip()
    if not stripped or re.fullmatch(r"(?:the\s+)?(?:project|workspace|overview|summary)", stripped, re.I):
        stripped = "recent memories"
    return selected, stripped, []
