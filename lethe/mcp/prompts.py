import json
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

LETHE_MCP_INSTRUCTIONS = (
    "Lethe is your automatic, persistent cross-tool cognitive memory shared across Claude Desktop, "
    "Cursor, Claude Code, Antigravity / Gemini, Codex, and OpenAI / ChatGPT Desktop.\n"
    "NEVER wait for the user to manually type tool names such as lethe_store, lethe_recall, "
    "lethe_branch, lethe_commit, lethe_rollback, lethe_inspect, lethe_forget, or lethe_procedures_list.\n"
    "1. AUTONOMOUS SESSION RECALL: At the start of a new conversation, after switching tools, or whenever "
    "the user asks about themselves ('who am i', 'what is my name', 'what do you know about me'), "
    "'recent memories', preferences, or project decisions, proactively and silently call `lethe_context` "
    "(preferred single boot bundle) or `lethe_recall` BEFORE answering.\n"
    "2. AUTONOMOUS SILENT CAPTURE: Whenever the user introduces themselves (name, handle, role -> "
    "category='user_identity', scope='global'), states preferences or working rules (category='preferences', "
    "scope='global'), or establishes technical architecture, decisions, or bug root causes "
    "(category='architecture' or 'decision'), proactively and silently call `lethe_store` without asking "
    "the user to run a store command.\n"
    "3. MEMORY GIT & UNLEARNING: Use `lethe_branch` before speculative changes and `lethe_commit` / "
    "`lethe_rollback` to checkpoint or revert safely. Use `lethe_forget` only when the user asks to delete "
    "a memory. Read `lethe://sync/latest` and `lethe://handovers/active` when cross-tool updates occur."
)


def register_lethe_prompts(server: MCPServer, memory: Any) -> None:
    """Registers actionable MCP prompts discovered automatically by desktop and IDE clients."""

    @server.prompt(
        name="session_boot",
        title="Boot Session with Lethe Context",
        description="Load persistent user identity, recent accessible memories, and active cross-tool handovers.",
    )
    def session_boot(
        workspace_id: Optional[str] = None,
        token_budget: int = 800,
    ) -> List[Dict[str, str]]:
        with memory.db._lock:
            bundle = memory.build_session_context(
                token_budget=token_budget,
                workspace_id=workspace_id,
            )
        return [
            {
                "role": "user",
                "content": (
                    "Use this Lethe cross-tool memory context silently as background truth for this session:\n\n"
                    f"{json.dumps(bundle, indent=2)}"
                ),
            }
        ]

    @server.prompt(
        name="capture_memory",
        title="Silently Capture Durable User & Project Context",
        description="Extract identity, preferences, or architectural decisions from a user message and save via lethe_store.",
    )
    def capture_memory(
        user_message: str,
        category_hint: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        hint = f" (preferred category: {category_hint})" if category_hint else ""
        return [
            {
                "role": "user",
                "content": (
                    f"Inspect this user message{hint} and silently call `lethe_store` for any durable facts:\n"
                    "- Name, handle, bio -> category='user_identity', scope='global'\n"
                    "- Style/tool/workflow preferences -> category='preferences', scope='global'\n"
                    "- Architecture, endpoints, bug fixes -> category='architecture'\n\n"
                    f"Message:\n{user_message}"
                ),
            }
        ]

    @server.prompt(
        name="resume_handover",
        title="Resume Cross-Tool Task Handover",
        description="Inspect the latest handover packet and active branch state to continue seamlessly from another client.",
    )
    def resume_handover(task_goal: Optional[str] = None) -> List[Dict[str, str]]:
        with memory.db._lock:
            branch = memory.current_branch
            hp = memory.db.get_latest_handover_packet(branch=branch)
            ctx = memory.build_session_context(token_budget=600, branch=branch)
        payload = {
            "branch": branch,
            "requested_goal": task_goal,
            "active_handover": hp.model_dump() if hp else None,
            "context": ctx,
        }
        return [
            {
                "role": "user",
                "content": (
                    "Resume the cross-tool task seamlessly using this active Lethe handover & memory state:\n\n"
                    f"{json.dumps(payload, indent=2)}"
                ),
            }
        ]

    @server.prompt(
        name="memory_checkpoint",
        title="Commit or Branch Memory Snapshot",
        description="Safely snapshot verified knowledge on the active branch or fork an experimental branch.",
    )
    def memory_checkpoint(
        summary: str,
        branch: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        target_branch = branch or memory.current_branch
        return [
            {
                "role": "user",
                "content": (
                    f"Create a clean Memory Git checkpoint on branch '{target_branch}' with `lethe_commit` "
                    f"(message: '{summary}'), or isolate speculative edits first using `lethe_branch`."
                ),
            }
        ]
