"""
Lethe Platform Module.
Provides cross-tool integration, auto-configuration, workspace management,
and real-time developer observability.
"""

from lethe.platform.setup import (
    build_mcp_credentials,
    configure_tool,
    detect_installed_tools,
    format_mcp_credentials,
    generate_mcp_config,
    get_default_db_path,
    get_default_procedures_dir,
    setup_all_tools,
)

__all__ = [
    "build_mcp_credentials",
    "configure_tool",
    "detect_installed_tools",
    "format_mcp_credentials",
    "generate_mcp_config",
    "get_default_db_path",
    "get_default_procedures_dir",
    "setup_all_tools",
]

