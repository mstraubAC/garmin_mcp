"""Integration tests for the HTTP transport (Starlette ASGI app)."""
import asyncio
import socket
import threading

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.testclient import TestClient

from garmin_mcp.server import build_mcp, make_app


@pytest.fixture
def stub_app(mock_garmin_client):
    """ASGI app whose lifespan installs the mock Garmin client."""
    return make_app(client_provider=lambda: mock_garmin_client)


def test_healthz_returns_ok(stub_app):
    with TestClient(stub_app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_mcp_endpoint_present(stub_app):
    """The /mcp endpoint should exist (even if a bare GET is rejected by the
    transport)."""
    with TestClient(stub_app) as client:
        resp = client.get("/mcp")
        # The streamable-HTTP transport rejects unmediated GETs, but a 404
        # would mean the route isn't mounted at all.
        assert resp.status_code != 404


def test_build_mcp_registers_all_modules():
    mcp = build_mcp()
    tool_names = set(mcp._tool_manager._tools.keys())
    # Spot-check tools from each module to confirm registration ran.
    expected_samples = {
        "get_devices",                  # devices
        "get_workouts",                 # workouts
        "get_nutrition_daily_meals",    # nutrition
        "get_full_name",                # user_profile
        "get_gear",                     # gear_management
    }
    missing = expected_samples - tool_names
    assert not missing, f"missing expected tools: {missing}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(mock_garmin_client):
    """Run the ASGI app on a real port using uvicorn in a background thread.

    Yields the base URL. Cleanly shuts the server down after the test.
    """
    mock_garmin_client.get_full_name.return_value = "Marcel Test"
    app = make_app(client_provider=lambda: mock_garmin_client)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to come up (max ~3s)
    for _ in range(30):
        if server.started:
            break
        threading.Event().wait(0.1)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("uvicorn did not start in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        # See note in test_oauth_flow.py — give the uvicorn thread's loop
        # time to settle so its asyncio primitives don't leak across tests.
        threading.Event().wait(0.3)


@pytest.mark.timeout(15)
async def test_mcp_client_can_call_tool_over_http(live_server, mock_garmin_client):
    """End-to-end: a real MCP client connects over streamable-HTTP, lists
    tools, and calls one. Verifies the wire is intact and the per-user cache
    resolves to the configured Garmin client."""
    async with streamablehttp_client(f"{live_server}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) > 50  # we register ~100

            result = await session.call_tool("get_full_name", {})
            assert result.content
            assert "Marcel Test" in result.content[0].text
    mock_garmin_client.get_full_name.assert_called()
