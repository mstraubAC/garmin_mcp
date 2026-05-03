"""HTTP transport for the Garmin MCP server.

Run via uvicorn:
    uvicorn garmin_mcp.server:app --host 127.0.0.1 --port 8000

Or via the installed script:
    garmin-mcp-http

In this step (no auth yet) the server runs in single-user mode using the same
GARMIN_EMAIL / GARMIN_PASSWORD / GARMINTOKENS env vars as the stdio entrypoint.
The ASGI app is bound to 127.0.0.1 by default — do not expose it publicly until
the OAuth proxy lands in a later step.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from garmin_mcp import (
    activity_management,
    challenges,
    data_management,
    devices,
    email,
    gear_management,
    health_wellness,
    init_api,
    nutrition,
    password,
    training,
    user_profile,
    weight_management,
    womens_health,
    workout_templates,
    workouts,
)
from garmin_mcp.user_context import SingleUserClientCache, set_client_cache


def build_mcp() -> FastMCP:
    """Construct the FastMCP instance with every module's tools registered."""
    mcp = FastMCP("Garmin Connect v1.0")
    activity_management.register_tools(mcp)
    health_wellness.register_tools(mcp)
    user_profile.register_tools(mcp)
    devices.register_tools(mcp)
    gear_management.register_tools(mcp)
    weight_management.register_tools(mcp)
    challenges.register_tools(mcp)
    training.register_tools(mcp)
    workouts.register_tools(mcp)
    data_management.register_tools(mcp)
    womens_health.register_tools(mcp)
    nutrition.register_tools(mcp)
    workout_templates.register_resources(mcp)
    return mcp


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _default_client_provider():
    client = init_api(email, password)
    if client is None:
        print(
            "Failed to initialize Garmin Connect client. "
            "Set GARMIN_EMAIL/GARMIN_PASSWORD or run `garmin-mcp-auth` first.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return client


def make_app(client_provider=_default_client_provider) -> Starlette:
    """Build the ASGI app.

    Args:
        client_provider: Callable returning a Garmin client. Default reads env
            vars and logs in. Tests can pass a stub returning a mock.
    """
    mcp = build_mcp()
    # Build the streamable-HTTP app eagerly so `mcp.session_manager` exists by
    # the time lifespan runs.
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_):
        set_client_cache(SingleUserClientCache(client_provider()))
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


app = make_app()


def main() -> None:
    """Entry point for the `garmin-mcp-http` script."""
    import uvicorn

    host = os.environ.get("GARMIN_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("GARMIN_MCP_PORT", "8000"))
    uvicorn.run(
        app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
