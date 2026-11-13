import ast
import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple
from lethe.models import CodeAnchor


def compute_file_content_hash(file_path: Path | str) -> Optional[str]:
    """Computes SHA-256 hash of a file's binary content."""
    p = Path(file_path)
    if not p.is_file():
        return None
    try:
        hasher = hashlib.sha256()
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def get_git_commit_sha(repo_dir: Path | str) -> Optional[str]:
    """Returns the current git HEAD commit hash if inside a git repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res.returncode == 0:
            sha = res.stdout.strip()
            return sha if len(sha) >= 7 else None
    except Exception:
        pass
    return None


def check_symbol_in_file(file_path: Path | str, symbol_name: str) -> bool:
    """
    Checks if a function, class, or variable symbol still exists in the specified file.
    Uses Python AST parsing for .py files, and regex token matching for other extensions.
    """
    p = Path(file_path)
    if not p.is_file():
        return False

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    if p.suffix.lower() == ".py":
        try:
            tree = ast.parse(content, filename=str(p))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == symbol_name:
                        return True
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == symbol_name:
                            return True
            return False
        except SyntaxError:
            # Fall back to token regex if file has transient syntax error
            pass

    # Regex token search for symbols across languages
    pattern = rf"\b{re.escape(symbol_name)}\b"
    return bool(re.search(pattern, content))


def check_code_drift(
    anchor: CodeAnchor,
    workspace_dir: Optional[Path | str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates whether code has drifted from the recorded memory anchor:
    1. Checks if referenced file still exists.
    2. Compares content hash against original hash when recorded.
    3. Checks Git history delta between anchor.git_sha and current HEAD.
    4. Verifies whether referenced symbol still exists in AST.

    Returns:
        (is_drifted: bool, warning_message: Optional[str])
    """
    raw_path = Path(anchor.file_path)
    if raw_path.is_absolute():
        resolved_file = raw_path
    elif workspace_dir:
        resolved_file = (Path(workspace_dir) / raw_path).resolve()
    else:
        resolved_file = raw_path.resolve()

    # 1. Existence check
    if not resolved_file.is_file():
        return True, f"Referenced file '{anchor.file_path}' no longer exists on disk."

    # 2. Symbol existence check (prioritized: missing symbol is a critical breaking change)
    if anchor.symbol_name:
        if not check_symbol_in_file(resolved_file, anchor.symbol_name):
            return True, f"Referenced symbol '{anchor.symbol_name}' was removed from '{anchor.file_path}'."

    # 3. Content hash check
    if anchor.content_hash:
        current_hash = compute_file_content_hash(resolved_file)
        if current_hash and current_hash != anchor.content_hash:
            detail = f" (recorded at commit {anchor.git_sha[:8]})" if anchor.git_sha else ""
            return True, f"File '{anchor.file_path}' has changed since this decision was recorded{detail}."

    # 4. Git delta check
    if anchor.git_sha:
        repo_root = resolved_file.parent
        # Walk up to find git root
        while repo_root != repo_root.parent and not (repo_root / ".git").exists():
            repo_root = repo_root.parent

        if (repo_root / ".git").exists():
            try:
                # Check if file had any modifications between anchor.git_sha and HEAD
                rel_file = resolved_file.relative_to(repo_root)
                res = subprocess.run(
                    ["git", "diff", "--name-only", f"{anchor.git_sha}..HEAD", "--", str(rel_file)],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    return True, f"File '{anchor.file_path}' was modified in git history since commit {anchor.git_sha[:8]}."
            except Exception:
                pass

    return False, None
