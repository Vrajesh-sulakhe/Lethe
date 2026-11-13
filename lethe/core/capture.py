"""
Deterministic, server-side extraction of durable facts from a raw user message.

This is what makes Lethe capture *automatic*: clients only have to forward the
user's message (via the `lethe` MCP tool or a prompt hook). Lethe — not the
model — decides what is worth remembering, so capture no longer depends on the
model choosing to call `lethe_store`.

Design rules:
- Only first-person statements and explicit "remember ..." requests are captured.
- Questions, requests, pasted code and logs are ignored.
- Anything that looks like a secret is never auto-captured.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

MAX_SENTENCE_CHARS = 400
MIN_SENTENCE_WORDS = 2


@dataclass(frozen=True)
class CapturedFact:
    content: str
    category: str
    rule: str
    explicit: bool = False

    @property
    def capture_hash(self) -> str:
        return capture_hash(self.content)


def capture_hash(content: str) -> str:
    normalized = re.sub(r"[\W_]+", " ", content.lower()).strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


_SENSITIVE_RE = re.compile(
    r"(api[\s_-]?key|passw(?:or)?d|passwd|\bsecret\b|\btoken\b|bearer\s|\bsk-[a-z0-9]|private[\s_-]?key"
    r"|\bssn\b|credit\s?card|\bcvv\b|\botp\b|\bpin\b|-----begin|\b\d{12,19}\b)",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_LOG_LINE_RE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}[t ]\d|\[\w+\]|at\s+\S+\(|traceback|\$\s|>>>|[{}\[\]<>])", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+|\s+(?:and|but|so)\s+(?=i\b|i'm\b|im\b|we\b|my\b)", re.IGNORECASE)
_LEADING_FILLER_RE = re.compile(r"^(?:(?:hi|hello|hey|yo|ok|okay|so|well|btw|by the way|also|and|actually)\b[\s,!.:-]*)+", re.IGNORECASE)

_QUESTION_START_RE = re.compile(
    r"^(?:what|which|who|whom|whose|where|when|why|how|does|did|can|could|would|should|is|are|was|were|will|shall|may|might|have|has|do(?!\s+not|n't))\b",
    re.IGNORECASE,
)

# Words that follow "i am" / "i'm" but are NOT names.
_NON_NAME_WORDS = frozenset("""
a an the not so very really just still also back here there done sure ready busy fine good ok okay great tired
hungry sleepy sick bored happy sad angry confused lost stuck new old trying going working looking using thinking
eating drinking having making doing getting building coding writing reading learning studying playing watching
listening waiting planning starting finishing on in at from with about into currently now always never sorry glad
afraid aware able unable free available interested excited worried late early home out off up down over almost
gonna gotta like totally kinda sorta pretty quite literally definitely probably maybe
""".split())

_TRANSIENT_STATES = frozenset("tired hungry sleepy sick bored happy sad angry confused lost stuck sorry".split())


def _clean_message(message: str) -> str:
    text = _CODE_FENCE_RE.sub(" ", message or "")
    text = _INLINE_CODE_RE.sub(" ", text)
    kept = [ln for ln in text.splitlines() if ln.strip() and not _LOG_LINE_RE.match(ln) and len(ln) <= 2000]
    return "\n".join(kept)


def _split_sentences(text: str) -> List[str]:
    parts = [p.strip(" \t-*•") for p in _SENTENCE_SPLIT_RE.split(text) if p and p.strip()]
    return [p for p in parts if p]


def _is_question(sentence: str) -> bool:
    s = sentence.strip()
    return s.endswith("?") or bool(_QUESTION_START_RE.match(s))


def _title(name: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in name.split())


def _quote(sentence: str) -> str:
    s = sentence.strip().rstrip(".!")
    return f'User stated: "{s}".'


# ---------------------------------------------------------------- rule table
_IDENTITY_NAME_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"\bmy name is\s+([a-z][\w'.-]*(?:\s+[a-z][\w'.-]*)?)", re.IGNORECASE),
    re.compile(r"\b(?:call me|i go by|people call me)\s+([a-z][\w'-]*)", re.IGNORECASE),
)
_SELF_INTRO_RE = re.compile(r"^(?:i am|i'm|im|this is|it's|its)\s+([a-z][\w'-]*)(?:\s+([a-z][\w'-]*))?\s*[.!]*$", re.IGNORECASE)
_HANDLE_RE = re.compile(r"\b(?:my (?:handle|username|github|user ?name) is)\s+@?([\w.-]+)", re.IGNORECASE)
_ROLE_RE = re.compile(r"\b(?:i am|i'm|im)\s+(?:a|an)\s+(?!bit\b|little\b|lot\b|big fan\b|huge fan\b|fan\b)([a-z][\w -]{2,60})", re.IGNORECASE)
_WORK_RE = re.compile(r"\bi (?:work|am working|'m working|intern|study|am studying|'m studying)\s+(?:at|for|as|in)\s+\S", re.IGNORECASE)
_LOCATION_RE = re.compile(r"\bi(?:'m| am)?\s*(?:live|living|stay|staying|based|located|from)\s+(?:in|at|from|out of)?\s*[a-z]|\bi (?:moved|relocated|shifted) to\b", re.IGNORECASE)

_PREFERENCE_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"\bi (?:really |totally |absolutely |kinda |just )?(?:like|love|prefer|enjoy|adore|hate|dislike|despise|can't stand|cannot stand|don't like|do not like)\b", re.IGNORECASE),
    re.compile(r"\bmy (?:favou?rite|preferred|go-to|usual)\b", re.IGNORECASE),
    re.compile(r"\bi(?:'m| am)\s+(?:a\s+)?(?:big\s+|huge\s+)?fan of\b", re.IGNORECASE),
    re.compile(r"\bi (?:always|usually|mostly|normally|typically|never|only) (?:use|code|write|work|eat|drink|prefer|run|deploy|test|go)\b", re.IGNORECASE),
    re.compile(r"\bi(?:'m| am)\s+(?:a\s+)?(?:vegetarian|vegan|allergic|lactose|jain|pescatarian)", re.IGNORECASE),
)
_ACTIVITY_RE = re.compile(
    r"\bi(?:'m| am| was| have been| had been| just| am currently|'m currently)?\s*"
    r"(?:eating|drinking|ate|drank|watching|reading|playing|listening to|learning|studying|building|cooking|working on|preparing for|training for)\b"
    r"|\bi play\b|\bi'?ve been (?:eating|drinking|watching|reading|playing|learning|building|working on)\b",
    re.IGNORECASE,
)
_DECISION_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"\b(?:we|team|i)\s+(?:have\s+|'ve\s+)?(?:decided|chose|picked|selected|settled on|went with|are going with|will use|switched to|migrated to|moved to|standardi[sz]ed on|agreed)\b", re.IGNORECASE),
    re.compile(r"\bwe(?:'re| are)?\s+(?:use|using|building|working on|implementing|migrating to|moving to|moved to|switching to)\b", re.IGNORECASE),
    re.compile(r"\blet'?s\s+(?:use|go with|stick with|switch to|keep)\b", re.IGNORECASE),
    re.compile(r"^(?:decision|architecture|stack)\s*[:=-]", re.IGNORECASE),
    re.compile(r"\b(?:our|the)\s+(?:stack|database|db|backend|frontend|framework|deadline|api|server|port|url|host|service|auth|storage|cache|queue|repo|branch|model|deployment|project|goal)\s+(?:is|will be|uses|runs on)\b", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?(?:root cause|bug|issue|problem|fix)\s+(?:is|was)\b", re.IGNORECASE),
)
_CONVENTION_RES: Tuple[re.Pattern, ...] = (
    re.compile(r"^(?:please\s+)?(?:always|never|don't ever|do not ever|make sure to|avoid)\b(?!\s+mind)", re.IGNORECASE),
    re.compile(r"\b(?:from now on|going forward|in future|in the future|as a rule)\b", re.IGNORECASE),
)
_EXPLICIT_RE = re.compile(
    r"^(?:lethe[\s,:]+)?(?:please\s+)?(?:remember|memori[sz]e|note|keep in mind|don't forget|do not forget|for the record|fyi|store|save)"
    r"(?:\s+(?:that|this))?\s*[:,-]?\s+(.{3,})$",
    re.IGNORECASE,
)


def _classify(sentence: str) -> Optional[CapturedFact]:
    s = _LEADING_FILLER_RE.sub("", sentence).strip()
    if not s:
        return None

    explicit = _EXPLICIT_RE.match(s)
    if explicit:
        body = explicit.group(1).strip().rstrip(".!")
        nested = _classify(body) if not _EXPLICIT_RE.match(body) else None
        if nested:
            return CapturedFact(nested.content, nested.category, nested.rule, explicit=True)
        return CapturedFact(_quote(body), "note", "explicit_remember", explicit=True)

    if _is_question(s) or len(s.split()) < MIN_SENTENCE_WORDS:
        return None

    for pat in _IDENTITY_NAME_RES:
        m = pat.search(s)
        if m:
            name = " ".join(w for w in m.group(1).split() if w.lower() not in _NON_NAME_WORDS)
            if name:
                return CapturedFact(f"User's name is {_title(name)}.", "user_identity", "name")

    m = _SELF_INTRO_RE.match(s)
    if m:
        words = [w for w in (m.group(1), m.group(2)) if w]
        if words and words[0].lower() not in _NON_NAME_WORDS and not words[0].lower().endswith("ing"):
            name_words = [words[0]] + [w for w in words[1:] if w.lower() not in _NON_NAME_WORDS and not w.lower().endswith("ing")]
            return CapturedFact(f"User's name is {_title(' '.join(name_words))}.", "user_identity", "self_intro")

    m = _HANDLE_RE.search(s)
    if m:
        return CapturedFact(f"User's handle/username is {m.group(1)}.", "user_identity", "handle")

    if _ROLE_RE.search(s) or _WORK_RE.search(s):
        return CapturedFact(_quote(s), "user_identity", "role")

    if _LOCATION_RE.search(s):
        return CapturedFact(_quote(s), "personal", "location")

    for pat in _PREFERENCE_RES:
        if pat.search(s):
            return CapturedFact(_quote(s), "preferences", "preference")

    if _ACTIVITY_RE.search(s):
        return CapturedFact(_quote(s), "personal", "activity")

    for pat in _DECISION_RES:
        if pat.search(s):
            return CapturedFact(_quote(s), "decision", "decision")

    for pat in _CONVENTION_RES:
        if pat.search(s):
            return CapturedFact(_quote(s), "conventions", "convention")

    return None


def extract_durable_facts(message: str, max_facts: int = 8) -> Tuple[List[CapturedFact], int]:
    """
    Returns (facts, skipped_sensitive_count) extracted from a raw user message.
    Pure function: no I/O, deterministic, safe to call on every turn.
    """
    facts: List[CapturedFact] = []
    seen: set[str] = set()
    skipped_sensitive = 0

    for sentence in _split_sentences(_clean_message(message)):
        if len(sentence) > MAX_SENTENCE_CHARS:
            continue
        if _SENSITIVE_RE.search(sentence):
            skipped_sensitive += 1
            continue
        fact = _classify(sentence)
        if not fact:
            continue
        if fact.capture_hash in seen:
            continue
        seen.add(fact.capture_hash)
        facts.append(fact)
        if len(facts) >= max_facts:
            break
    return facts, skipped_sensitive


_LETHE_PREFIX_RE = re.compile(r"^\s*/?lethe\b[\s,:;-]*", re.IGNORECASE)
_BOOT_WORDS = frozenset({"", "context", "boot", "status", "load", "sync", "recall", "everything", "all", "memory", "memories", "hi", "hey", "hello", "start"})


def parse_lethe_invocation(message: str) -> Tuple[bool, str]:
    """
    Detects a manual 'lethe ...' invocation typed in chat.
    Returns (is_boot_request, query_text).
    - 'lethe' / 'lethe context' / 'lethe recall' -> full boot context.
    - 'lethe <question>' -> recall for <question>.
    - anything else -> normal turn on the raw message.
    """
    raw = (message or "").strip()
    m = _LETHE_PREFIX_RE.match(raw)
    if not m:
        return False, raw
    rest = raw[m.end():].strip()
    if rest.lower().strip(" .!?") in _BOOT_WORDS:
        return True, ""
    return False, rest

