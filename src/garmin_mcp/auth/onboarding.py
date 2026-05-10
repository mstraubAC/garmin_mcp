"""Per-user Garmin onboarding flow with MFA support.

`garth.login()` blocks on a `prompt_mfa` callback when Garmin requires a
verification code. We can't block the HTTP request waiting on the user to
fetch the code, so the login runs in a worker thread and the callback waits
on a `threading.Event` until the web layer feeds it the code.

State machine (per `OnboardingTicket`):

    NEW ─submit creds─► AUTHENTICATING ─no MFA─► COMPLETE
                          │
                          └─MFA prompted─► AWAITING_MFA ─submit code─► COMPLETE
                                                                │
                                                                └─wrong code─► AWAITING_MFA
                                                                  (max 3 retries)

Sessions live in memory only; a server restart cancels any in-flight
onboarding (the user starts over). On success the worker calls
`token_store.save(...)`, then `pending_oauth_callback` (set by the OAuth
provider) is invoked to issue our auth code and produce the Claude redirect URL.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from queue import Empty, Queue

from garminconnect import Garmin, GarminConnectAuthenticationError

from garmin_mcp.auth.garmin_tokens import GarminTokenStore

log = logging.getLogger(__name__)

DEFAULT_TICKET_TTL_SECONDS = 5 * 60
DEFAULT_MFA_TIMEOUT_SECONDS = 5 * 60
MAX_MFA_ATTEMPTS = 3


class OnboardingState(str, Enum):
    NEW = "NEW"
    AUTHENTICATING = "AUTHENTICATING"
    AWAITING_MFA = "AWAITING_MFA"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@dataclass
class OnboardingSession:
    ticket: str
    user_id: str
    created_at: float
    expires_at: float
    state: OnboardingState = OnboardingState.NEW
    error_message: str | None = None
    mfa_attempts: int = 0
    # Caller (the OAuth provider) supplies this; called when onboarding
    # succeeds. Returns the URL to redirect the browser to (typically
    # Claude's redirect_uri with our auth code attached).
    on_success: Callable[[str], str] | None = None
    # Populated only on COMPLETE — the URL we redirect the browser to.
    redirect_url: str | None = None
    # Internal: queue the worker waits on for MFA codes
    _mfa_queue: Queue | None = field(default=None, repr=False)
    _mfa_event: threading.Event | None = field(default=None, repr=False)
    _worker: threading.Thread | None = field(default=None, repr=False)


class OnboardingError(Exception):
    pass


class OnboardingManager:
    """Holds in-memory onboarding sessions and the per-session worker threads."""

    def __init__(
        self,
        token_store: GarminTokenStore,
        garmin_factory: Callable[..., Garmin] | None = None,
        ticket_ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
        mfa_timeout_seconds: int = DEFAULT_MFA_TIMEOUT_SECONDS,
        max_concurrent_sessions: int = 10,
    ):
        self._tokens = token_store
        self._garmin_factory = garmin_factory or (lambda **kw: Garmin(**kw))
        self._ticket_ttl = ticket_ttl_seconds
        self._mfa_timeout = mfa_timeout_seconds
        self._max_concurrent = max_concurrent_sessions
        self._sessions: dict[str, OnboardingSession] = {}
        self._lock = threading.Lock()

    # Lifecycle ------------------------------------------------------------

    def create_session(
        self,
        user_id: str,
        on_success: Callable[[str], str] | None = None,
    ) -> OnboardingSession:
        """Allocate a new ticket. Caller must hand the ticket to the user
        and direct them to /onboard?ticket=...
        """
        with self._lock:
            self._evict_expired_locked()
            if (
                len([s for s in self._sessions.values() if not _is_terminal(s.state)])
                >= self._max_concurrent
            ):
                raise OnboardingError("too many concurrent onboarding sessions")
            ticket = secrets.token_urlsafe(24)
            now = time.monotonic()
            session = OnboardingSession(
                ticket=ticket,
                user_id=user_id,
                created_at=now,
                expires_at=now + self._ticket_ttl,
                on_success=on_success,
            )
            self._sessions[ticket] = session
            return session

    def get(self, ticket: str) -> OnboardingSession | None:
        with self._lock:
            session = self._sessions.get(ticket)
            if session is None:
                return None
            if (
                session.state not in (OnboardingState.COMPLETE, OnboardingState.FAILED)
                and time.monotonic() > session.expires_at
            ):
                session.state = OnboardingState.EXPIRED
            return session

    def evict(self, ticket: str) -> None:
        with self._lock:
            self._sessions.pop(ticket, None)

    def _evict_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [t for t, s in self._sessions.items() if now > s.expires_at + 60]
        for t in expired:
            self._sessions.pop(t, None)

    def evict_terminal_sessions(self) -> int:
        """Drop COMPLETE/FAILED/EXPIRED sessions."""
        with self._lock:
            now = time.monotonic()
            to_evict = [
                t
                for t, s in self._sessions.items()
                if _is_terminal(s.state) or now > s.expires_at + 60
            ]
            for t in to_evict:
                self._sessions.pop(t, None)
            return len(to_evict)

    # Credentials submission ----------------------------------------------

    def submit_credentials(
        self,
        ticket: str,
        email: str,
        password: str,
        is_cn: bool = False,
    ) -> OnboardingSession:
        session = self.get(ticket)
        if session is None:
            raise OnboardingError("unknown ticket")
        if session.state != OnboardingState.NEW:
            raise OnboardingError(
                f"session is not accepting credentials (state={session.state.value})"
            )

        session._mfa_queue = Queue(maxsize=1)
        session._mfa_event = threading.Event()
        session.state = OnboardingState.AUTHENTICATING

        worker = threading.Thread(
            target=self._run_login,
            args=(session, email, password, is_cn),
            daemon=True,
            name=f"onboard-{ticket[:8]}",
        )
        session._worker = worker
        worker.start()
        return session

    def _prompt_mfa(self, session: OnboardingSession) -> str:
        """Called from the worker thread by `garth.login()` when an MFA code
        is required. Blocks until the web layer feeds a code via
        `submit_mfa()`."""
        with self._lock:
            session.state = OnboardingState.AWAITING_MFA
        try:
            return session._mfa_queue.get(timeout=self._mfa_timeout)
        except Empty:
            raise OnboardingError("MFA timed out") from None

    def _run_login(
        self,
        session: OnboardingSession,
        email: str,
        password: str,
        is_cn: bool,
    ) -> None:
        try:
            client = self._garmin_factory(
                email=email,
                password=password,
                is_cn=is_cn,
                prompt_mfa=lambda: self._prompt_mfa(session),
            )
            client.login()
            token_blob = client.garth.dumps()
        except GarminConnectAuthenticationError as e:
            self._mark_failed(session, _format_garmin_error(e))
            return
        except OnboardingError as e:
            # MFA timeout / user gave up
            self._mark_failed(session, str(e))
            return
        except Exception as e:  # pragma: no cover — broad safety net
            log.exception("onboarding worker crashed for %s", session.ticket[:8])
            self._mark_failed(session, f"unexpected error: {e}")
            return
        finally:
            # Zero the password reference in this scope; the caller's copy
            # is its own concern.
            password = "x" * len(password)  # noqa: F841

        # Login succeeded — persist tokens and resolve any pending OAuth.
        try:
            self._tokens.save(session.user_id, token_blob)
        except Exception as e:  # pragma: no cover
            self._mark_failed(session, f"could not store tokens: {e}")
            return

        redirect_url = None
        if session.on_success is not None:
            try:
                redirect_url = session.on_success(session.user_id)
            except Exception as e:  # pragma: no cover
                log.exception("on_success callback failed for %s", session.ticket[:8])
                self._mark_failed(session, f"post-onboarding callback failed: {e}")
                return

        with self._lock:
            session.state = OnboardingState.COMPLETE
            session.redirect_url = redirect_url

    def _mark_failed(self, session: OnboardingSession, message: str) -> None:
        with self._lock:
            session.state = OnboardingState.FAILED
            session.error_message = message

    # MFA submission -------------------------------------------------------

    def submit_mfa(self, ticket: str, code: str) -> OnboardingSession:
        session = self.get(ticket)
        if session is None:
            raise OnboardingError("unknown ticket")
        if session.state != OnboardingState.AWAITING_MFA:
            raise OnboardingError(f"session is not awaiting MFA (state={session.state.value})")
        if not code or not code.strip():
            raise OnboardingError("MFA code must not be empty")

        session.mfa_attempts += 1
        if session.mfa_attempts > MAX_MFA_ATTEMPTS:
            self._mark_failed(session, "too many MFA attempts")
            return session

        # Hand the code to the worker; it will resume `garth.login()`. If
        # garth rejects the code, we'll get back a GarminConnectAuthenticationError
        # which the worker turns into FAILED.
        with self._lock:
            session.state = OnboardingState.AUTHENTICATING
        session._mfa_queue.put(code.strip())
        return session

    # Test/inspection helpers ---------------------------------------------

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if not _is_terminal(s.state))


def _is_terminal(state: OnboardingState) -> bool:
    return state in (
        OnboardingState.COMPLETE,
        OnboardingState.FAILED,
        OnboardingState.EXPIRED,
    )


def _format_garmin_error(e: GarminConnectAuthenticationError) -> str:
    msg = str(e).lower()
    if "mfa" in msg or "code" in msg:
        return "Invalid or expired MFA code."
    if "password" in msg or "credentials" in msg or "401" in msg:
        return "Invalid email or password."
    return "Garmin Connect authentication failed."
