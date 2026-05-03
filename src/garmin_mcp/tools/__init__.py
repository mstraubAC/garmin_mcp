"""MCP tool modules — one module per Garmin Connect domain.

Each module registers its tools on the FastMCP instance via a
`register_tools(mcp: FastMCP)` function.  Importing a module
side-effects the MCP instance.

The top-level `garmin_mcp/__init__.py` and `garmin_mcp/server.py`
import from here to keep the tool surface discoverable.
"""
