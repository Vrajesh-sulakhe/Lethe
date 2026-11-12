"""
Lethe Platform Module.
Provides cross-tool integration, auto-configuration, workspace management,
and real-time developer observability.
"""

from lethe.platform.setup import (
    configure_tool,
    detect_installed_tools,
    generate_mcp_config,
    setup_all_tools,
)

__all__ = [
    "detect_installed_tools",
    "generate_mcp_config",
    "configure_tool",
    "setup_all_tools",
]

