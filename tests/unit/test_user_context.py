"""Unit tests for the per-user client cache."""
import time
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from garmin_mcp.auth.garmin_tokens import GarminTokenStore
from garmin_mcp.auth.storage import Storage
from garmin_mcp.user_context import (
    DEFAULT_USER_ID,
    MultiUserClientCache,
    SingleUserClientCache,
    UserNotOnboardedError,
    current_user_id,
    set_client_cache,
    set_current_user_id,
)


# Helpers --------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def token_store(storage):
    return GarminTokenStore(storage, Fernet.generate_key().decode())


def _seed_user_with_token(storage, token_store, user_id="u1", token="t"):
    storage.get_or_create_user(
        user_id_factory=lambda: user_id,
        entra_sub=f"sub-{user_id}",
        entra_tid="tid",
        email=None,
        display_name=None,
    )
    token_store.save(user_id, token)


def _fake_garmin_factory():
    """Returns a callable that produces a fresh MagicMock with a `garth`
    attribute whose `loads()` doesn't fail."""
    def factory():
        m = MagicMock(name="GarminClient")
        m.garth.loads = MagicMock()
        return m

    return factory


# ContextVar -----------------------------------------------------------------


def test_current_user_id_defaults_when_unset():
    assert current_user_id() == DEFAULT_USER_ID


def test_set_current_user_id_visible_to_current_call():
    set_current_user_id("alice")
    assert current_user_id() == "alice"


# SingleUserClientCache -----------------------------------------------------


def test_single_user_cache_returns_same_client_for_any_user():
    client = MagicMock(name="GarminClient")
    cache = SingleUserClientCache(client)
    assert cache.get_or_load("alice") is client
    assert cache.get_or_load("bob") is client


# MultiUserClientCache ------------------------------------------------------


def test_multi_user_cache_loads_token_then_caches(storage, token_store):
    _seed_user_with_token(storage, token_store, "u1", "garth-blob-1")
    cache = MultiUserClientCache(
        token_store, garmin_factory=_fake_garmin_factory()
    )
    c1 = cache.get_or_load("u1")
    c2 = cache.get_or_load("u1")
    assert c1 is c2  # cached on second call
    c1.garth.loads.assert_called_once_with("garth-blob-1")


def test_multi_user_cache_isolates_users(storage, token_store):
    _seed_user_with_token(storage, token_store, "u1", "blob-1")
    _seed_user_with_token(storage, token_store, "u2", "blob-2")
    cache = MultiUserClientCache(
        token_store, garmin_factory=_fake_garmin_factory()
    )
    c1 = cache.get_or_load("u1")
    c2 = cache.get_or_load("u2")
    assert c1 is not c2
    c1.garth.loads.assert_called_once_with("blob-1")
    c2.garth.loads.assert_called_once_with("blob-2")
    assert cache.size() == 2


def test_multi_user_cache_unknown_user_raises(token_store):
    cache = MultiUserClientCache(token_store)
    with pytest.raises(UserNotOnboardedError) as exc:
        cache.get_or_load("never-onboarded")
    assert exc.value.user_id == "never-onboarded"


def test_multi_user_cache_idle_ttl_evicts(storage, token_store, monkeypatch):
    _seed_user_with_token(storage, token_store, "u1", "blob")
    fake_now = [1000.0]
    monkeypatch.setattr("garmin_mcp.user_context.time.monotonic", lambda: fake_now[0])

    cache = MultiUserClientCache(
        token_store, garmin_factory=_fake_garmin_factory(), idle_ttl_seconds=60
    )
    c1 = cache.get_or_load("u1")
    fake_now[0] += 30  # within TTL
    assert cache.get_or_load("u1") is c1

    fake_now[0] += 100  # past TTL → reload
    c2 = cache.get_or_load("u1")
    assert c2 is not c1


def test_multi_user_cache_invalidate_drops_entry(storage, token_store):
    _seed_user_with_token(storage, token_store, "u1", "blob")
    cache = MultiUserClientCache(
        token_store, garmin_factory=_fake_garmin_factory()
    )
    c1 = cache.get_or_load("u1")
    cache.invalidate("u1")
    c2 = cache.get_or_load("u1")
    assert c1 is not c2


def test_multi_user_cache_invalidate_unknown_is_noop(token_store):
    cache = MultiUserClientCache(token_store)
    cache.invalidate("nope")  # must not raise


# Integration with set_client_cache ----------------------------------------


def test_set_client_cache_replaces_active_cache(storage, token_store):
    _seed_user_with_token(storage, token_store, "u1", "blob")
    multi = MultiUserClientCache(
        token_store, garmin_factory=_fake_garmin_factory()
    )
    set_client_cache(multi)
    set_current_user_id("u1")
    from garmin_mcp.user_context import get_garmin_client
    assert get_garmin_client() is multi.get_or_load("u1")


# RateLimitedGarminProxy ----------------------------------------------------


from garmin_mcp.auth.storage import Storage as StorageCls
from garmin_mcp.auth.throttle import ToolCallGuard, RateLimitExceededError
from garmin_mcp.user_context import RateLimitedGarminProxy


def test_proxy_passes_non_callable_attributes(storage):
    """Non-callable attrs (like garth, session, etc.) pass through directly."""
    client = MagicMock(name="GarminClient")
    client.garth = "some_garth_obj"
    guard = ToolCallGuard(storage)
    proxy = RateLimitedGarminProxy(client, "u1", guard)
    assert proxy.garth == "some_garth_obj"


def test_proxy_blocks_when_rate_limited(storage):
    """Callable methods check rate limit; block on exhaustion."""
    client = MagicMock(name="GarminClient")
    client.get_full_name.return_value = "Alice"
    guard = ToolCallGuard(storage)

    # Exhaust per-user bucket.
    for _ in range(60):
        guard.try_consume_sync("u1")

    proxy = RateLimitedGarminProxy(client, "u1", guard)
    with pytest.raises(RateLimitExceededError) as exc:
        proxy.get_full_name()
    assert exc.value.user_id == "u1"
    client.get_full_name.assert_not_called()


def test_proxy_allows_when_under_limit(storage):
    """Callable methods pass through when rate limit allows."""
    client = MagicMock(name="GarminClient")
    client.get_full_name.return_value = "Alice"
    guard = ToolCallGuard(storage)
    proxy = RateLimitedGarminProxy(client, "u1", guard)
    result = proxy.get_full_name()
    assert result == "Alice"
    client.get_full_name.assert_called_once()


def test_cache_wraps_with_proxy_when_guard_set(storage, token_store):
    """MultiUserClientCache wraps clients in proxy when guard is provided."""
    _seed_user_with_token(storage, token_store, "u1", "blob")
    guard = ToolCallGuard(storage)
    cache = MultiUserClientCache(
        token_store, garmin_factory=_fake_garmin_factory(), tool_call_guard=guard
    )
    client = cache.get_or_load("u1")
    assert isinstance(client, RateLimitedGarminProxy)
