"""Server-rendered HTML onboarding flow (htmx for the MFA partial reload).

Endpoints:
    GET  /onboard?ticket=...      → email/password form (or MFA / status panel)
    POST /onboard/credentials     → starts the worker
    GET  /onboard/status?ticket=… → htmx polls this; returns the current panel
    POST /onboard/mfa             → submits MFA code

We deliberately keep the HTML inline (no template engine) — the surface is
small enough that adding jinja2 doesn't pay back, and the strings here are
the entirety of the user-visible UI.

CSRF protection: every session gets a random token at creation time
(OnboardingSession.csrf_token). GET /onboard sets it as an __Host-csrf
cookie and embeds it as a hidden form field. Every POST verifies the
posted field against the session token using hmac.compare_digest.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from garmin_mcp.auth.onboarding import (
    OnboardingError,
    OnboardingManager,
    OnboardingState,
)

log = logging.getLogger(__name__)

HTMX_SCRIPT = '<script src="/static/htmx.min.js"></script>'


def _layout(body: str, title: str = "Garmin MCP onboarding") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
{HTMX_SCRIPT}
<style>
 body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
        max-width: 28rem; margin: 4rem auto; padding: 1rem; color: #222 }}
 h1 {{ font-size: 1.4rem }}
 input[type=text], input[type=email], input[type=password] {{
        width: 100%; padding: .55rem; margin: .25rem 0 .9rem 0;
        border: 1px solid #ccc; border-radius: 4px; font-size: 1rem }}
 button {{ background: #0a5; color: white; border: 0;
        padding: .6rem 1.2rem; border-radius: 4px; font-size: 1rem;
        cursor: pointer }}
 button:hover {{ background: #084 }}
 .err {{ color: #b00; padding: .5rem .75rem; background: #fee;
        border: 1px solid #fbb; border-radius: 4px; margin-bottom: 1rem }}
 .ok  {{ color: #060; padding: .5rem .75rem; background: #efe;
        border: 1px solid #bfb; border-radius: 4px; margin-bottom: 1rem }}
 .muted {{ color: #888; font-size: .9rem }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def _credentials_form(ticket: str, csrf_token: str, error: str | None = None) -> str:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<h1>Connect your Garmin account</h1>
<p class="muted">One-time setup. Your password is used only to mint long-lived
OAuth tokens, then discarded — only the tokens are stored (encrypted at rest).</p>
{err}
<form hx-post="/onboard/credentials" hx-target="#panel" hx-swap="outerHTML">
  <input type="hidden" name="ticket" value="{html.escape(ticket)}">
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
  <label>Garmin email
    <input type="email" name="email" required autocomplete="username">
  </label>
  <label>Garmin password
    <input type="password" name="password" required autocomplete="current-password">
  </label>
  <button type="submit">Sign in to Garmin</button>
</form>"""


def _panel_html(
    state: OnboardingState,
    ticket: str,
    csrf_token: str,
    error: str | None = None,
) -> str:
    """Returns the inner panel for a given session state. Wrapped in
    `<div id="panel">` so htmx can swap it on every poll."""
    if state == OnboardingState.NEW:
        body = _credentials_form(ticket, csrf_token, error)
    elif state == OnboardingState.AUTHENTICATING:
        body = (
            '<h1>Signing in to Garmin…</h1><p class="muted">This usually takes a few seconds.</p>'
        )
    elif state == OnboardingState.AWAITING_MFA:
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        body = f"""<h1>Enter your MFA code</h1>
<p class="muted">Garmin sent a verification code to your email or phone.</p>
{err}
<form hx-post="/onboard/mfa" hx-target="#panel" hx-swap="outerHTML">
  <input type="hidden" name="ticket" value="{html.escape(ticket)}">
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
  <label>Code
    <input type="text" name="code" inputmode="numeric" required
           autocomplete="one-time-code" autofocus>
  </label>
  <button type="submit">Verify</button>
</form>"""
    elif state == OnboardingState.COMPLETE:
        body = (
            '<h1 class="ok">All set!</h1>'
            "<p>Redirecting you back to Claude…</p>"
            "<script>window.location.href = window.__redirect_url;</script>"
        )
    elif state == OnboardingState.FAILED:
        body = f"""<h1>Onboarding failed</h1>
<p class="err">{html.escape(error or "unknown error")}</p>
<p><a href="/onboard?ticket={html.escape(ticket)}&restart=1">Start over</a></p>"""
    elif state == OnboardingState.EXPIRED:
        body = """<h1>Session expired</h1>
<p class="err">This onboarding ticket has expired. Re-open the original
sign-in link from Claude to start over.</p>"""
    else:
        body = "<p>unknown state</p>"

    needs_poll = state in (OnboardingState.AUTHENTICATING,)
    poll = (
        f' hx-get="/onboard/status?ticket={html.escape(ticket)}" '
        f'hx-trigger="every 1s" hx-swap="outerHTML"'
        if needs_poll
        else ""
    )
    return f'<div id="panel"{poll}>{body}</div>'


def _verify_csrf(posted: str, expected: str) -> bool:
    """Constant-time comparison of CSRF tokens. Returns False on any mismatch."""
    return hmac.compare_digest(posted.encode(), expected.encode())


# Route handlers ------------------------------------------------------------


def build_routes(
    manager: OnboardingManager,
    redirect_resolver: Callable[[str], str | None] | None = None,
) -> list[Route]:
    """`redirect_resolver(ticket) -> str | None` is consulted on the
    COMPLETE state to inject the Claude redirect URL into the success page.
    The OnboardingManager already stashes this on the session, so the
    default reads from there."""
    redirect_resolver = redirect_resolver or (
        lambda ticket: (s := manager.get(ticket)) and s.redirect_url
    )

    async def onboard_page(request: Request) -> Response:
        ticket = request.query_params.get("ticket", "")
        session = manager.get(ticket)
        if session is None:
            return HTMLResponse(
                _layout(
                    "<h1>Unknown onboarding ticket</h1>"
                    '<p class="err">This link is invalid or has been used. '
                    "Re-start sign-in from Claude.</p>"
                ),
                status_code=404,
            )
        page = _layout(
            _panel_html(session.state, ticket, session.csrf_token, session.error_message)
        )
        response = HTMLResponse(page)
        # Set the CSRF token as an __Host- cookie so it is bound to the
        # origin and cannot be set by subdomains. The form posts the same
        # value as a hidden field; we compare them on every POST.
        response.set_cookie(
            "__Host-csrf",
            session.csrf_token,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
        return response

    async def submit_credentials(request: Request) -> Response:
        form = await request.form()
        ticket = (form.get("ticket") or "").strip()
        email = (form.get("email") or "").strip()
        password: str = form.get("password") or "" or ""

        # CSRF check — must happen before any state lookup so an attacker
        # who guesses a ticket and sends no token still gets a 400.
        posted_csrf = (form.get("csrf_token") or "").strip()
        session = manager.get(ticket)
        if session is None or not _verify_csrf(posted_csrf, session.csrf_token):
            csrf_token = session.csrf_token if session else ""
            return HTMLResponse(
                _panel_html(
                    OnboardingState.NEW, ticket, csrf_token, "Invalid or missing CSRF token."
                ),
                status_code=400,
            )

        if not all([ticket, email, password]):
            return HTMLResponse(
                _panel_html(
                    OnboardingState.NEW, ticket, session.csrf_token, "All fields are required."
                ),
                status_code=400,
            )

        # Validate client binding (H23/H30) — lenient: only compare if the
        # session was bound during the OAuth callback.
        if session.user_agent_hash is not None:
            current_ua = request.headers.get("User-Agent", "")
            current_hash = hashlib.sha256(current_ua.encode()).hexdigest() if current_ua else ""
            if current_hash != session.user_agent_hash:
                return HTMLResponse(
                    _panel_html(
                        OnboardingState.NEW,
                        ticket,
                        session.csrf_token,
                        "Session binding mismatch — please restart onboarding.",
                    ),
                    status_code=403,
                )
        if session.client_ip is not None:
            xff = request.headers.get("X-Forwarded-For", "")
            current_ip = (
                xff.split(",")[0].strip()
                if xff
                else (request.client.host if request.client else "")
            )
            if current_ip != session.client_ip:
                return HTMLResponse(
                    _panel_html(
                        OnboardingState.NEW,
                        ticket,
                        session.csrf_token,
                        "Session binding mismatch — please restart onboarding.",
                    ),
                    status_code=403,
                )

        try:
            session = manager.submit_credentials(ticket, email, password)
        except OnboardingError as e:
            session_now = manager.get(ticket)
            csrf_token = session_now.csrf_token if session_now else ""
            return HTMLResponse(
                _panel_html(OnboardingState.NEW, ticket, csrf_token, str(e)),
                status_code=400,
            )
        return HTMLResponse(_panel_html(session.state, ticket, session.csrf_token))

    async def status(request: Request) -> Response:
        ticket = request.query_params.get("ticket", "")
        session = manager.get(ticket)
        if session is None:
            return HTMLResponse(
                _panel_html(OnboardingState.EXPIRED, ticket, ""),
                status_code=404,
            )
        # On COMPLETE, inject the redirect URL via a tiny inline script.
        panel = _panel_html(session.state, ticket, session.csrf_token, session.error_message)
        if session.state == OnboardingState.COMPLETE:
            redirect = redirect_resolver(ticket)
            if redirect:
                # Replace the JS placeholder with the actual URL.
                panel = panel.replace(
                    "window.__redirect_url",
                    f'"{html.escape(redirect, quote=True)}"',
                )
        return HTMLResponse(panel)

    async def submit_mfa(request: Request) -> Response:
        form = await request.form()
        ticket = (form.get("ticket") or "").strip()
        code = (form.get("code") or "").strip()

        # CSRF check first.
        posted_csrf = (form.get("csrf_token") or "").strip()
        session = manager.get(ticket)
        if session is None or not _verify_csrf(posted_csrf, session.csrf_token):
            csrf_token = session.csrf_token if session else ""
            return HTMLResponse(
                _panel_html(
                    OnboardingState.AWAITING_MFA,
                    ticket,
                    csrf_token,
                    "Invalid or missing CSRF token.",
                ),
                status_code=400,
            )

        # Validate client binding (H23/H30) — lenient: only compare if bound.
        if session.user_agent_hash is not None:
            current_ua = request.headers.get("User-Agent", "")
            current_hash = hashlib.sha256(current_ua.encode()).hexdigest() if current_ua else ""
            if current_hash != session.user_agent_hash:
                return HTMLResponse(
                    _panel_html(
                        OnboardingState.AWAITING_MFA,
                        ticket,
                        session.csrf_token,
                        "Session binding mismatch — please restart onboarding.",
                    ),
                    status_code=403,
                )
        if session.client_ip is not None:
            xff = request.headers.get("X-Forwarded-For", "")
            current_ip = (
                xff.split(",")[0].strip()
                if xff
                else (request.client.host if request.client else "")
            )
            if current_ip != session.client_ip:
                return HTMLResponse(
                    _panel_html(
                        OnboardingState.AWAITING_MFA,
                        ticket,
                        session.csrf_token,
                        "Session binding mismatch — please restart onboarding.",
                    ),
                    status_code=403,
                )

        try:
            session = manager.submit_mfa(ticket, code)
        except OnboardingError as e:
            current = manager.get(ticket)
            current_state = current.state if current else OnboardingState.EXPIRED
            csrf_token = current.csrf_token if current else ""
            return HTMLResponse(
                _panel_html(current_state, ticket, csrf_token, str(e)),
                status_code=400,
            )
        return HTMLResponse(_panel_html(session.state, ticket, session.csrf_token))

    return [
        Route("/onboard", onboard_page, methods=["GET"]),
        Route("/onboard/credentials", submit_credentials, methods=["POST"]),
        Route("/onboard/status", status, methods=["GET"]),
        Route("/onboard/mfa", submit_mfa, methods=["POST"]),
    ]
