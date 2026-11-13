import json
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import MCPServer

LETHE_MCP_INSTRUCTIONS = (
    "Lethe is the user's shared memory across all their AI apps and IDEs (Claude Desktop, Claude Code, "
    "Cursor, Antigravity / Gemini, Codex, ChatGPT Desktop).\n"
    "THE ONE RULE: call the `lethe` tool FIRST on EVERY user message, before you reply, passing the user's "
    "latest message verbatim as `message` (and your app name as `client`).\n"
    "- It returns the relevant slice of memory (identity, preferences, decisions, what the user was doing in "
    "other tools). Use it silently as background truth; never say 'I don't know' about the user without it.\n"
    "- It automatically saves durable facts the user states. Never ask 'should I save this?' and never wait "
    "for the user to type a command.\n"
    "- If the user types just 'lethe', pass it through: Lethe returns their full memory.\n"
    "- Pass the actual client workspace_root (absolute project directory) when available; never use the Lethe server installation directory as the project. "
    "Use lethe_projects to discover exact workspace identifiers when a project name is ambiguous.\n"
    "- A call instruction is best-effort; automatic enforcement requires a client hook or event integration.\n"
    "Additional tools (`lethe_context`, `lethe_store`, `lethe_recall`, `lethe_forget`, `lethe_branch`, "
    "`lethe_commit`, `lethe_rollback`) remain available for explicit/advanced control."
)


def register_lethe_prompts(server: MCPServer, memory: Any) -> None:
    """Registers actionable MCP prompts discovered automatically by desktop and IDE clients."""

    @server.prompt(
        name="lethe",
        title="Lethe: load my memory",
        description="Load everything Lethe knows about you and your work (identity, preferences, decisions, handovers).",
    )
    def lethe_prompt(question: Optional[str] = None) -> List[Dict[str, str]]:
        message = f"lethe {question}" if question else "lethe"
        with memory.db._lock:
            result = memory.process_turn(message, client_id=None, capture=False)
        return [
            {
                "role": "user",
                "content": (
                    "Here is my Lethe memory. Use it silently as background truth for this conversation, "
                    "and keep calling the `lethe` tool on each of my messages:\n\n"
                    f"{result['context_text']}"
                ),
            }
        ]

    @server.prompt(
        name="session_boot",
        title="Boot Session with Lethe Context",
        description="Load persistent user identity, recent accessible memories, and active cross-tool handovers.",
    )
    def session_boot(
        workspace_id: Optional[str] = None,
        token_budget: int = 5000,
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
            ctx = memory.build_session_context(token_budget=5000, branch=branch)
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
