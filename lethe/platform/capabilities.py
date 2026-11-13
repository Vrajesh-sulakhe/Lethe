"""Documented client surfaces, separate from adapters implemented by Lethe."""

from copy import deepcopy

CLAUDE_MIN_VERSION = (2, 1, 314)
CLIENT_ALIASES = {
    "claude": "claude-code",
    "gemini": "gemini-cli",
    "openai": "chatgpt",
    "openai-desktop": "chatgpt",
    "chatgpt-desktop": "chatgpt",
    "codex-desktop": "codex",
}

_CAPABILITIES = {
    "claude-code": {
        "native_hooks_documented": True,
        "adapter": "claude-code-v1",
        "automatic": True,
        "strict": True,
        "minimum_version": ".".join(map(str, CLAUDE_MIN_VERSION)),
        "events": ["UserPromptSubmit", "PostToolUse:Write|Edit", "Stop"],
        "source": "https://code.claude.com/docs/en/hooks",
        "limitations": "No shell/external edits or deletions; PostToolUse cannot undo completed writes. Hooks must be enabled and the executable available.",
    },
    "cursor": {
        "native_hooks_documented": True,
        "source": "https://cursor.com/docs/agent/hooks",
        "limitations": "beforeSubmitPrompt and afterFileEdit documented; Lethe adapter not implemented.",
    },
    "gemini-cli": {
        "native_hooks_documented": True,
        "source": "https://geminicli.com/docs/hooks/",
        "limitations": "BeforeAgent and AfterTool documented; Lethe adapter not implemented.",
    },
    "codex": {
        "native_hooks_documented": True,
        "source": "https://developers.openai.com/codex/config-reference",
        "limitations": "Lifecycle hooks documented in config reference; Lethe adapter not implemented.",
    },
    "claude-desktop": {"limitations": "MCP configuration supported; no native capture adapter for the chat app."},
    "chatgpt": {"limitations": "Manual connector/custom instructions only; no verified native local configuration or capture adapter."},
    "gemini-desktop": {"limitations": "No verified native local configuration or capture adapter. Gemini CLI is a separate client."},
}


def client_capability(client: str) -> dict:
    canonical = CLIENT_ALIASES.get(client, client)
    result = {
        "client": canonical,
        "automatic": False,
        "strict": False,
        "adapter": None,
        "native_hooks_documented": False,
        "generic_event_interface": True,
        "rules": "best_effort",
        "status": "manual",
    }
    result.update(deepcopy(_CAPABILITIES.get(canonical, {})))
    if result["automatic"]:
        result["status"] = "available_not_verified_installed"
    return result


def capture_capabilities() -> list[dict]:
    return [client_capability(client) for client in _CAPABILITIES]
