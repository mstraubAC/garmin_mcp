"""
Modular MCP Server for Garmin Connect Data
"""

# Legacy stdio-mode entry point — kept for backward compat with `garmin-mcp` CLI.
# Uses GARMIN_EMAIL/GARMIN_PASSWORD env vars for a single Garmin account.
# Prefer `garmin-mcp-http` for production (multi-user, OAuth, Entra ID).
from garmin_mcp._stdio import main  # noqa: F401
from garmin_mcp.tools import (
    activities,
    challenges,
    data,
    devices,
    gear,
    health,
    nutrition,
    training,
    user_profile,
    weight,
    womens_health,
    workout_templates,
    workouts,
)
from garmin_mcp.user_context import SingleUserClientCache, set_client_cache
