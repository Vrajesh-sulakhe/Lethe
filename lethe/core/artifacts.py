"""Opt-in, bounded artifact summaries. This module never executes document content.

The host supplies ``memory.capture_config`` (not event data):
``artifacts_enabled=True, allowed_roots=[absolute_workspace_root]``. Roots must
match exactly. Hidden, generated, ignored, linked, binary and sensitive files
are rejected. POSIX descriptor-relative opens prevent symlink-swap escapes;
platforms without that primitive fail closed.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from lethe.core.capture import _SENSITIVE_RE

MAX_ARTIFACT_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 6000
_SUFFIXES = {".html", ".htm", ".md", ".markdown", ".txt", ".rst"}
_EXCLUDED = {"node_modules", "vendor", "dist", "build", "target", "__pycache__", "venv", "coverage"}
_PRIVATE_NAME = re.compile(r"(?:credential|secret|password|private[_.-]?key|id_rsa|id_ed25519)", re.I)
_EXTRA_SECRET = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b", re.I)


class ArtifactRejected(ValueError):
    """A static, content-free rejection code safe for receipts."""


def contains_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text) or _EXTRA_SECRET.search(text))


@dataclass(frozen=True)
class Artifact:
    content: str
    relative_path: str
    subject_hash: str
    content_hash: str
    media_type: str


class _SummaryParser(HTMLParser):
    _BLOCKED = {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.title = []
        self.description = []
        self.headings = []
        self.visible = []
        self.remaining = MAX_SUMMARY_CHARS * 2

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = re.sub(r"\s+", "", attrs.get("style") or "").lower()
        hidden = (tag in self._BLOCKED or "hidden" in attrs or
                  (attrs.get("aria-hidden") or "").lower() == "true" or
                  "display:none" in style or "visibility:hidden" in style)
        blocked = hidden or bool(self.stack and self.stack[-1][1])
        if tag == "meta" and not blocked and (attrs.get("name") or "").lower() == "description":
            self.description.append((attrs.get("content") or "")[:600])
        if tag not in self._VOID:
            if len(self.stack) >= 256:
                raise ArtifactRejected("html_too_deep")
            self.stack.append((tag, blocked))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.remaining or (self.stack and self.stack[-1][1]):
            return
        text = " ".join(data.split())[:self.remaining]
        if not text:
            return
        tags = {entry[0] for entry in self.stack}
        if "title" in tags:
            self.title.append(text[:300])
        elif "head" not in tags:
            self.visible.append(text)
            if tags & {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.headings.append(text[:300])
        self.remaining -= len(text)

    def summary(self):
        sections = []
        for label, parts, limit in (("Title", self.title, 300), ("Description", self.description, 600),
                                    ("Headings", self.headings[:12], 1200), ("Text", self.visible, 3800)):
            if parts:
                sections.append(f"{label}: {' '.join(parts)[:limit]}")
        return "\n".join(sections)[:MAX_SUMMARY_CHARS]


def _root_and_relative(workspace_root, path, config):
    if config.get("artifacts_enabled") is not True:
        raise ArtifactRejected("artifacts_disabled")
    if not isinstance(workspace_root, (str, Path)) or not Path(workspace_root).is_absolute():
        raise ArtifactRejected("invalid_root")
    roots = config.get("allowed_roots", [])
    if not isinstance(roots, (list, tuple)):
        raise ArtifactRejected("root_not_allowed")
    root = Path(workspace_root).resolve(strict=True)
    if Path(os.path.abspath(workspace_root)) != root:
        raise ArtifactRejected("symlink_root")
    allowed = {Path(os.path.abspath(p)) for p in roots if isinstance(p, (str, Path)) and Path(p).is_absolute()}
    if root not in allowed or not root.is_dir():
        raise ArtifactRejected("root_not_allowed")
    raw = Path(path)
    if ".." in raw.parts:
        raise ArtifactRejected("path_escape")
    if raw.is_absolute():
        try:
            raw = raw.relative_to(Path(workspace_root))
        except ValueError:
            try:
                raw = raw.relative_to(root)
            except ValueError:
                raise ArtifactRejected("path_escape") from None
    if not raw.parts or any(p.startswith(".") or p.lower() in _EXCLUDED or _PRIVATE_NAME.search(p) for p in raw.parts):
        raise ArtifactRejected("excluded_path")
    if raw.suffix.lower() not in _SUFFIXES:
        raise ArtifactRejected("unsupported_type")
    return root, raw


def _check_ignored(root: Path, relative: Path):
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "-z", "--stdin"],
            input=os.fsencode(relative.as_posix()) + b"\0", capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ArtifactRejected("ignore_check_failed") from None
    if result.returncode == 0:
        raise ArtifactRejected("gitignored")
    if result.returncode == 1:
        return
    # Plain directories without ignore rules are supported, but never guess rules.
    parents = [root, *[root / p for p in relative.parents if p != Path(".")]]
    if any((p / ".git").exists() or (p / ".gitignore").exists() for p in parents):
        raise ArtifactRejected("ignore_check_failed")
    if b"not a git repository" not in result.stderr:
        raise ArtifactRejected("ignore_check_failed")


def _open_directory(path: Path):
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ArtifactRejected("safe_open_unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def artifact_location(workspace_root, path, config):
    """Validate a path without reading it; missing final files support delete events."""
    root, relative = _root_and_relative(workspace_root, path, config)
    fd = _open_directory(root)
    try:
        parent_exists = True
        for component in relative.parts[:-1]:
            try:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                parent_exists = False
                break
            os.close(fd)
            fd = next_fd
        if parent_exists:
            try:
                info = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ArtifactRejected("unsafe_file")
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)
    _check_ignored(root, relative)
    subject = hashlib.sha256((str(root) + "\0" + relative.as_posix()).encode()).hexdigest()
    return root, relative, subject


def capture_artifact(workspace_root, path, config) -> Artifact:
    root, relative, subject = artifact_location(workspace_root, path, config)
    fd = _open_directory(root)
    file_fd = None
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ArtifactRejected("unsafe_file")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactRejected("file_too_large")
        chunks = []
        remaining = MAX_ARTIFACT_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(file_fd)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactRejected("file_too_large")
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ArtifactRejected("file_changed_during_read")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(fd)
    if b"\0" in data:
        raise ArtifactRejected("binary_file")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ArtifactRejected("invalid_encoding") from None
    if contains_sensitive(text):
        raise ArtifactRejected("sensitive_content")
    html = relative.suffix.lower() in {".html", ".htm"}
    if not html:
        text = re.sub(r"```.*?(?:```|$)|~~~.*?(?:~~~|$)", " ", text, flags=re.S)
    parser = _SummaryParser()
    parser.feed(text)
    parser.close()
    summary = parser.summary()
    if not summary:
        raise ArtifactRejected("empty_content")
    if contains_sensitive(summary):
        raise ArtifactRejected("sensitive_content")
    return Artifact(summary, relative.as_posix(), subject, hashlib.sha256(data).hexdigest(),
                    "text/html" if html else "text/plain")
