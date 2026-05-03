"""HTTP transport for the Garmin MCP server.

Run via uvicorn:
    uvicorn garmin_mcp.server:app --host 127.0.0.1 --port 8000

Or via the installed script:
    garmin-mcp-http

Two operating modes:

* **No-auth single-user mode** (`make_app()` without `auth_provider`)
  Used by tests and for local stdio-style use over HTTP. No /authorize, no
  /token, no DCR — the MCP endpoint is open. Bind only to 127.0.0.1.

* **OAuth-protected multi-user mode** (`make_app(auth_provider=...)`)
  FastMCP's resource-server side enforces a Bearer JWT issued by the
  proxy. Adds `/.well-known/oauth-*`, `/authorize`, `/token`, `/register`,
  and a `/callback` for Entra to return to. This is what `main()` builds
  from env vars when launched as `garmin-mcp-http`.

In Step 4 the same single Garmin account (env-var creds) backs every
authenticated user. Step 5 replaces `SingleUserClientCache` with a
per-user lookup that decrypts each user's stored Garmin tokens.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
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
from garmin_mcp.auth.audit import AuditLog
from garmin_mcp.auth.entra import EntraOIDCClient
from garmin_mcp.auth.garmin_tokens import GarminTokenStore
from garmin_mcp.auth.jwt import JwtSigner
from garmin_mcp.auth.onboarding import OnboardingManager
from garmin_mcp.auth.onboarding_routes import build_routes as build_onboarding_routes
from garmin_mcp.auth.provider import GarminMcpProvider
from garmin_mcp.auth.storage import Storage
from garmin_mcp.auth.throttle import RegistrationGuard, TokenBucket
from garmin_mcp.user_context import (
    ClientCache,
    MultiUserClientCache,
    SingleUserClientCache,
    set_client_cache,
)


def build_mcp(
    auth_provider: GarminMcpProvider | None = None,
    public_url: str | None = None,
) -> FastMCP:
    """Construct the FastMCP instance with every module's tools registered.

    When `auth_provider` is set, the MCP endpoint requires a Bearer JWT and
    the OAuth endpoints are exposed at the `/.well-known/...`, `/authorize`,
    `/token`, `/register` paths.
    """
    kwargs: dict = {"name": "Garmin Connect v1.0"}
    if auth_provider is not None:
        if not public_url:
            raise ValueError("public_url required when auth_provider is set")
        # FastMCP wraps the provider as its own TokenVerifier, calling
        # `load_access_token` on each request. Our provider sets the
        # per-request user_id ContextVar there.
        kwargs.update(
            auth_server_provider=auth_provider,
            auth=AuthSettings(
                issuer_url=AnyHttpUrl(public_url),
                resource_server_url=AnyHttpUrl(f"{public_url}/mcp"),
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=["mcp.use"],
                    default_scopes=["mcp.use"],
                ),
                required_scopes=["mcp.use"],
            ),
        )

    mcp = FastMCP(**kwargs)
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


def _build_callback_route(auth_provider: GarminMcpProvider) -> Route:
    """The /callback Entra redirects to after the user signs in."""

    async def callback(request: Request) -> Response:
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return Response("missing code or state", status_code=400)
        try:
            redirect_url = await auth_provider.complete_authorization(state, code)
        except Exception as e:
            return Response(f"authentication failed: {e}", status_code=400)
        return RedirectResponse(redirect_url, status_code=302)

    return Route("/callback", callback, methods=["GET"])


def make_app(
    client_provider=_default_client_provider,
    client_cache: ClientCache | None = None,
    auth_provider: GarminMcpProvider | None = None,
    public_url: str | None = None,
    onboarding_manager: OnboardingManager | None = None,
) -> Starlette:
    """Build the ASGI app.

    Two ways to wire the per-user Garmin client lookup:

      * `client_provider` — legacy single-user mode: a callable that returns
        one Garmin client; we wrap it in `SingleUserClientCache`. This is
        what tests and the no-auth dev path use.
      * `client_cache` — production multi-user mode: pass a fully-built
        `MultiUserClientCache` (or any `ClientCache`). When set,
        `client_provider` is ignored.

    `auth_provider` enables OAuth and adds the `/callback` route; when an
    `onboarding_manager` is also provided, the onboarding HTML routes are
    mounted as well.
    """
    mcp = build_mcp(auth_provider=auth_provider, public_url=public_url)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_):
        if client_cache is not None:
            set_client_cache(client_cache)
        else:
            set_client_cache(SingleUserClientCache(client_provider()))
        async with mcp.session_manager.run():
            yield

    routes: list = [Route("/healthz", healthz, methods=["GET"])]
    if auth_provider is not None:
        routes.append(_build_callback_route(auth_provider))
    if onboarding_manager is not None:
        routes.extend(build_onboarding_routes(onboarding_manager))
    routes.append(Mount("/", app=mcp_app))

    return Starlette(routes=routes, lifespan=lifespan)


def make_production_app() -> Starlette:
    """Build the OAuth-protected, multi-user ASGI app from env vars.

    Required env vars:
        MCP_PUBLIC_URL, JWT_SIGNING_KEY,
        ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET,
        GARMIN_MCP_DATA_KEY  (Fernet key — see `garmin_tokens.GarminTokenStore`)

    Optional:
        GARMIN_MCP_DATA_PATH (default /var/lib/garmin-mcp/state.db)
        MCP_REGISTRATION_TOKEN (lockdown mode for /register)
    """
    public_url = os.environ["MCP_PUBLIC_URL"].rstrip("/")
    db_path = os.environ.get("GARMIN_MCP_DATA_PATH", "/var/lib/garmin-mcp/state.db")
    storage = Storage(db_path)
    jwt_signer = JwtSigner(
        signing_key=os.environ["JWT_SIGNING_KEY"],
        issuer=public_url,
        audience=f"{public_url}/mcp",
    )
    entra = EntraOIDCClient(
        tenant_id=os.environ["ENTRA_TENANT_ID"],
        client_id=os.environ["ENTRA_CLIENT_ID"],
        client_secret=os.environ["ENTRA_CLIENT_SECRET"],
        redirect_uri=f"{public_url}/callback",
    )
    audit = AuditLog()
    per_ip_bucket = TokenBucket(storage, capacity=5, refill_per_second=5 / 3600)
    guard = RegistrationGuard(
        storage=storage,
        per_ip_bucket=per_ip_bucket,
        shared_token=os.environ.get("MCP_REGISTRATION_TOKEN"),
    )
    token_store = GarminTokenStore(storage, os.environ["GARMIN_MCP_DATA_KEY"])
    onboarding = OnboardingManager(token_store)
    client_cache = MultiUserClientCache(token_store)
    provider = GarminMcpProvider(
        storage=storage,
        entra=entra,
        jwt_signer=jwt_signer,
        registration_guard=guard,
        audit=audit,
        garmin_tokens=token_store,
        onboarding=onboarding,
        public_url=public_url,
    )
    return make_app(
        client_cache=client_cache,
        auth_provider=provider,
        public_url=public_url,
        onboarding_manager=onboarding,
    )


# Default `app` for `uvicorn garmin_mcp.server:app` — no-auth, kept stable
# so existing tests and local-dev usage don't break. Production uses
# `make_production_app()` via `main()`.
app = make_app()


def main() -> None:
    """Entry point for the `garmin-mcp-http` script.

    Builds the OAuth-protected production app from env vars and runs uvicorn.
    """
    import uvicorn

    host = os.environ.get("GARMIN_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("GARMIN_MCP_PORT", "8000"))
    prod_app = make_production_app()
    uvicorn.run(
        prod_app,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
