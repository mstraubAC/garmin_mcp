"""
User Profile functions for Garmin Connect MCP Server
"""

import json

from garmin_mcp.user_context import get_garmin_client


def register_tools(app):
    """Register all user profile tools with the MCP server app"""

    @app.tool()
    async def get_full_name() -> str:
        """Get user's full name from profile"""
        try:
            full_name = get_garmin_client().get_full_name()
            return json.dumps({"full_name": full_name}, indent=2)
        except Exception as e:
            return f"Error retrieving user's full name: {str(e)}"

    @app.tool()
    async def get_unit_system() -> str:
        """Get user's preferred unit system from profile"""
        try:
            unit_system = get_garmin_client().get_unit_system()
            return json.dumps({"unit_system": unit_system}, indent=2)
        except Exception as e:
            return f"Error retrieving unit system: {str(e)}"

    @app.tool()
    async def get_user_profile() -> str:
        """Get user profile information"""
        try:
            profile = get_garmin_client().get_user_profile()
            if not profile:
                return "No user profile information found."
            return json.dumps(profile, indent=2)
        except Exception as e:
            return f"Error retrieving user profile: {str(e)}"

    @app.tool()
    async def get_userprofile_settings() -> str:
        """Get user profile settings"""
        try:
            settings = get_garmin_client().get_userprofile_settings()
            if not settings:
                return "No user profile settings found."
            return json.dumps(settings, indent=2)
        except Exception as e:
            return f"Error retrieving user profile settings: {str(e)}"

    return app
