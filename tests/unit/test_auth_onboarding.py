"""Unit tests for the onboarding state machine + worker thread.

`garth.login()` is replaced by a test double that lets us script the MFA
behavior synchronously: success without MFA, success after MFA, MFA timeout,
wrong password, etc.
"""

import time

import pytest
from cryptography.fernet import Fernet
from garminconnect import GarminConnectAuthenticationError

from garmin_mcp.auth.garmin_tokens import GarminTokenStore
from garmin_mcp.auth.onboarding import (
    MAX_MFA_ATTEMPTS,
    OnboardingError,
    OnboardingManager,
    OnboardingState,
)
from garmin_mcp.auth.storage import Storage

# Test doubles --------------------------------------------------------------


class _FakeGarmin:
    """Stand-in for `garminconnect.Garmin` whose `login()` triggers the MFA
    callback if `wants_mfa` is True. Tests configure behavior per-instance
    via class attributes the factory closure reads."""

    def __init__(self, *, email, password, is_cn, prompt_mfa):
        self._email = email
        self._password = password
        self._prompt_mfa = prompt_mfa
        self.garth = self
        # Behavior knobs are set by the factory just below
        self._behavior = "ok_no_mfa"
        self._collected_mfa = None

    def login(self):
        if self._behavior == "bad_password":
            raise GarminConnectAuthenticationError("invalid credentials")
        if self._behavior == "needs_mfa":
            code = self._prompt_mfa()
            self._collected_mfa = code
            if code != "123456":
                raise GarminConnectAuthenticationError("MFA code incorrect")
        if self._behavior == "needs_mfa_then_succeeds":
            self._collected_mfa = self._prompt_mfa()
        return self

    def dumps(self) -> str:
        return f"garth-blob-for-{self._email}"


def _factory_with_behavior(behavior: str):
    def make(**kwargs):
        client = _FakeGarmin(**kwargs)
        client._behavior = behavior
        return client

    return make


# Fixtures ------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "state.db")
    s.get_or_create_user(lambda: "u1", "sub1", "tid", "alice@x.com", "Alice")
    yield s
    s.close()


@pytest.fixture
def token_store(storage):
    return GarminTokenStore(storage, Fernet.generate_key().decode())


def _wait_for_state(manager, ticket, predicate, timeout=2.0):
    """Poll the session's state until `predicate(state)` is True."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = manager.get(ticket)
        if s and predicate(s.state):
            return s
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting; last state = {manager.get(ticket).state}")


# Tests ---------------------------------------------------------------------


def test_create_session_returns_unique_tickets(token_store):
    mgr = OnboardingManager(token_store)
    a = mgr.create_session("u1")
    b = mgr.create_session("u1")
    assert a.ticket != b.ticket
    assert a.state == OnboardingState.NEW


def test_concurrent_sessions_capped(token_store):
    mgr = OnboardingManager(token_store, max_concurrent_sessions=2)
    mgr.create_session("u1")
    mgr.create_session("u2")
    with pytest.raises(OnboardingError, match="too many"):
        mgr.create_session("u3")


def test_credentials_succeed_without_mfa(token_store):
    mgr = OnboardingManager(token_store, garmin_factory=_factory_with_behavior("ok_no_mfa"))
    session = mgr.create_session("u1")
    mgr.submit_credentials(session.ticket, "alice@x.com", "secret")

    s = _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.COMPLETE)  # noqa: F841
    assert token_store.load("u1") == "garth-blob-for-alice@x.com"
    assert s.error_message is None


def test_credentials_fail_with_bad_password(token_store):
    mgr = OnboardingManager(token_store, garmin_factory=_factory_with_behavior("bad_password"))
    session = mgr.create_session("u1")
    mgr.submit_credentials(session.ticket, "alice@x.com", "wrong")

    s = _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.FAILED)
    assert "email or password" in s.error_message.lower()
    assert token_store.has("u1") is False


def test_mfa_happy_path_completes(token_store):
    mgr = OnboardingManager(
        token_store,
        garmin_factory=_factory_with_behavior("needs_mfa_then_succeeds"),
    )
    session = mgr.create_session("u1")
    mgr.submit_credentials(session.ticket, "alice@x.com", "secret")
    _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.AWAITING_MFA)

    mgr.submit_mfa(session.ticket, "123456")
    s = _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.COMPLETE)  # noqa: F841
    assert token_store.has("u1")


def test_mfa_wrong_code_marks_failed(token_store):
    """Submitting a code that garth rejects flows through the worker and
    marks the session FAILED with a clear error."""
    mgr = OnboardingManager(token_store, garmin_factory=_factory_with_behavior("needs_mfa"))
    session = mgr.create_session("u1")
    mgr.submit_credentials(session.ticket, "alice@x.com", "secret")
    _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.AWAITING_MFA)

    mgr.submit_mfa(session.ticket, "000000")  # not the right code
    s = _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.FAILED)
    assert "mfa" in s.error_message.lower()


def test_mfa_max_attempts_short_circuits(token_store):
    """Submitting > MAX_MFA_ATTEMPTS marks FAILED without dispatching to garth."""
    mgr = OnboardingManager(
        token_store,
        garmin_factory=_factory_with_behavior("needs_mfa_then_succeeds"),
    )
    session = mgr.create_session("u1")
    mgr.submit_credentials(session.ticket, "alice@x.com", "secret")
    _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.AWAITING_MFA)

    # Fake Garmin would accept anything, but we trip the manager-side limit.
    for _ in range(MAX_MFA_ATTEMPTS):
        # Each attempt resumes garth which immediately reprompts; in the
        # real world that would loop. For this test we skip past it by
        # making garth not-prompt-again. Simpler: just verify the limit.
        pass
    # Direct test: bump attempts then submit a final one
    session.mfa_attempts = MAX_MFA_ATTEMPTS
    mgr.submit_mfa(session.ticket, "999999")
    s = _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.FAILED)
    assert "too many" in s.error_message.lower()


def test_submit_mfa_unknown_ticket_raises(token_store):
    mgr = OnboardingManager(token_store)
    with pytest.raises(OnboardingError, match="unknown ticket"):
        mgr.submit_mfa("does-not-exist", "123456")


def test_submit_credentials_wrong_state_raises(token_store):
    mgr = OnboardingManager(token_store)
    s = mgr.create_session("u1")
    s.state = OnboardingState.COMPLETE
    with pytest.raises(OnboardingError, match="not accepting"):
        mgr.submit_credentials(s.ticket, "x", "y")


def test_session_expires(token_store):
    mgr = OnboardingManager(token_store, ticket_ttl_seconds=0)
    session = mgr.create_session("u1")
    time.sleep(0.05)
    s = mgr.get(session.ticket)
    assert s.state == OnboardingState.EXPIRED


def test_on_success_callback_fires_with_user_id(token_store):
    captured = {}

    def on_success(user_id):
        captured["user_id"] = user_id
        return f"https://claude.example.com/cb?code=our-code-for-{user_id}"

    mgr = OnboardingManager(token_store, garmin_factory=_factory_with_behavior("ok_no_mfa"))
    session = mgr.create_session("u1", on_success=on_success)
    mgr.submit_credentials(session.ticket, "alice@x.com", "secret")
    s = _wait_for_state(mgr, session.ticket, lambda st: st == OnboardingState.COMPLETE)  # noqa: F841

    assert captured["user_id"] == "u1"
    assert s.redirect_url == "https://claude.example.com/cb?code=our-code-for-u1"


def test_active_count_excludes_terminal_states(token_store):
    mgr = OnboardingManager(token_store, garmin_factory=_factory_with_behavior("ok_no_mfa"))
    s1 = mgr.create_session("u1")
    mgr.create_session("u2")
    assert mgr.active_count() == 2

    mgr.submit_credentials(s1.ticket, "alice@x.com", "secret")
    _wait_for_state(mgr, s1.ticket, lambda st: st == OnboardingState.COMPLETE)
    assert mgr.active_count() == 1
