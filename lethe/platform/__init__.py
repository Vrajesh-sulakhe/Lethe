"""
Lethe Platform Module.
Provides cross-tool integration, auto-configuration, workspace management,
and real-time developer observability.
"""

from lethe.platform.client_rules import (
    get_desktop_system_prompt,
    install_antigravity_rules,
    install_claude_memory_rules,
    install_client_rules_for_tools,
    install_codex_rules,
    install_cursor_sync_rule,
)
from lethe.platform.setup import (
    DEFAULT_SETUP_TOOLS,
    TOOL_ALIASES,
    build_mcp_credentials,
    configure_tool,
    detect_installed_tools,
    format_mcp_credentials,
    generate_mcp_config,
    get_default_db_path,
    get_default_procedures_dir,
    install_global_cli,
    setup_all_tools,
)

__all__ = [
    "DEFAULT_SETUP_TOOLS",
    "TOOL_ALIASES",
    "build_mcp_credentials",
    "configure_tool",
    "detect_installed_tools",
    "format_mcp_credentials",
    "generate_mcp_config",
    "get_default_db_path",
    "get_default_procedures_dir",
    "get_desktop_system_prompt",
    "install_antigravity_rules",
    "install_claude_memory_rules",
    "install_client_rules_for_tools",
    "install_codex_rules",
    "install_cursor_sync_rule",
    "install_global_cli",
    "setup_all_tools",
]

