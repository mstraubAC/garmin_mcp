"""Unit tests for the SQLite storage layer."""
import time
import uuid

import pytest

from garmin_mcp.auth.storage import Storage


@pytest.fixture
def storage(tmp_path):
    db = Storage(tmp_path / "state.db")
    yield db
    db.close()


# OAuth clients --------------------------------------------------------------


def test_register_and_get_client(storage):
    storage.register_client(
        client_id="abc123",
        client_secret_hash="hash",
        client_metadata={"redirect_uris": ["https://x/cb"], "client_name": "Test"},
        register_ip="1.2.3.4",
    )
    got = storage.get_client("abc123")
    assert got is not None
    assert got["client_id"] == "abc123"
    assert got["client_secret_hash"] == "hash"
    assert got["client_metadata"]["redirect_uris"] == ["https://x/cb"]
    assert got["register_ip"] == "1.2.3.4"
    assert got["last_used_at"] is None


def test_get_client_unknown_returns_none(storage):
    assert storage.get_client("nope") is None


def test_mark_client_used_sets_timestamp(storage):
    storage.register_client("c1", None, {}, None)
    assert storage.get_client("c1")["last_used_at"] is None
    storage.mark_client_used("c1")
    assert storage.get_client("c1")["last_used_at"] is not None


def test_count_clients(storage):
    assert storage.count_clients() == 0
    for i in range(3):
        storage.register_client(f"c{i}", None, {}, None)
    assert storage.count_clients() == 3


def test_cleanup_unused_clients_drops_old_unused(storage):
    """Never-used clients older than the threshold get pruned."""
    storage.register_client("old-unused", None, {}, None)
    storage._conn.execute(
        "UPDATE oauth_clients SET registered_at = ? WHERE client_id = ?",
        (int(time.time()) - 86400 * 2, "old-unused"),
    )
    storage.register_client("recent", None, {}, None)
    storage.register_client("active", None, {}, None)
    storage.mark_client_used("active")

    deleted = storage.cleanup_unused_clients(
        never_used_after=86400, idle_after=86400 * 90
    )
    assert deleted == 1
    assert storage.get_client("old-unused") is None
    assert storage.get_client("recent") is not None
    assert storage.get_client("active") is not None


def test_cleanup_unused_clients_drops_idle(storage):
    """Used clients idle longer than idle_after also get pruned."""
    storage.register_client("idle", None, {}, None)
    storage.mark_client_used("idle")
    storage._conn.execute(
        "UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?",
        (int(time.time()) - 86400 * 100, "idle"),
    )
    deleted = storage.cleanup_unused_clients(
        never_used_after=86400, idle_after=86400 * 90
    )
    assert deleted == 1


# Pending authorizations -----------------------------------------------------


def _store_pending(storage, state="state-1"):
    storage.store_pending_authorization(
        state=state,
        client_id="client-1",
        claude_redirect_uri="https://claude.example.com/cb",
        claude_state="claude-state-xyz",
        code_challenge="abcdef",
        code_challenge_method="S256",
        redirect_uri_provided_explicitly=True,
        scopes=["mcp.use"],
        resource="https://garmin-mcp.example.com/mcp",
    )


def test_consume_pending_authorization_returns_and_deletes(storage):
    _store_pending(storage)
    got = storage.consume_pending_authorization("state-1")
    assert got is not None
    assert got["client_id"] == "client-1"
    assert got["scopes"] == ["mcp.use"]
    assert got["redirect_uri_provided_explicitly"] is True
    # second call returns None (already consumed)
    assert storage.consume_pending_authorization("state-1") is None


def test_consume_pending_authorization_unknown_state(storage):
    assert storage.consume_pending_authorization("nope") is None


def test_consume_pending_authorization_expired(storage):
    storage.store_pending_authorization(
        state="exp",
        client_id="c",
        claude_redirect_uri="https://x/cb",
        claude_state=None,
        code_challenge="x",
        code_challenge_method="S256",
        redirect_uri_provided_explicitly=False,
        scopes=[],
        resource=None,
        ttl_seconds=-1,  # already expired
    )
    assert storage.consume_pending_authorization("exp") is None


def test_cleanup_expired_pending(storage):
    storage.store_pending_authorization(
        "valid",
        "c",
        "https://x/cb",
        None,
        "x",
        "S256",
        False,
        [],
        None,
        ttl_seconds=600,
    )
    storage.store_pending_authorization(
        "expired",
        "c",
        "https://x/cb",
        None,
        "x",
        "S256",
        False,
        [],
        None,
        ttl_seconds=-1,
    )
    assert storage.cleanup_expired_pending() == 1


# OAuth codes ----------------------------------------------------------------


def test_consume_authorization_code(storage):
    storage.store_authorization_code(
        code="code-1",
        client_id="c",
        user_id="u",
        redirect_uri="https://x/cb",
        redirect_uri_provided_explicitly=True,
        code_challenge="ch",
        scopes=["mcp.use"],
        resource=None,
    )
    got = storage.consume_authorization_code("code-1")
    assert got is not None
    assert got["user_id"] == "u"
    # second consume returns None
    assert storage.consume_authorization_code("code-1") is None


def test_load_authorization_code_does_not_consume(storage):
    storage.store_authorization_code(
        "code-2", "c", "u", "https://x/cb", True, "ch", [], None
    )
    assert storage.load_authorization_code("code-2") is not None
    # still loadable
    assert storage.load_authorization_code("code-2") is not None


def test_authorization_code_expired_returns_none(storage):
    storage.store_authorization_code(
        "exp", "c", "u", "https://x/cb", True, "ch", [], None, ttl_seconds=-1
    )
    assert storage.load_authorization_code("exp") is None
    assert storage.consume_authorization_code("exp") is None


# Refresh tokens -------------------------------------------------------------


def test_refresh_token_lifecycle(storage):
    storage.store_refresh_token(
        "hash1", "c", "u", ["mcp.use"], expires_at=int(time.time()) + 3600
    )
    got = storage.load_refresh_token("hash1")
    assert got is not None
    assert got["user_id"] == "u"

    storage.revoke_refresh_token("hash1")
    assert storage.load_refresh_token("hash1") is None


def test_refresh_token_expired(storage):
    storage.store_refresh_token("h", "c", "u", [], expires_at=int(time.time()) - 1)
    assert storage.load_refresh_token("h") is None


# Users ----------------------------------------------------------------------


def test_get_or_create_user_creates_then_reuses(storage):
    counter = iter(["uid-1", "uid-2"])
    factory = lambda: next(counter)

    u1 = storage.get_or_create_user(factory, "sub1", "tid1", "a@x.com", "Alice")
    assert u1["user_id"] == "uid-1"

    # same (sub, tid) returns the existing user; factory NOT called again
    u2 = storage.get_or_create_user(factory, "sub1", "tid1", "ignored", "ignored")
    assert u2["user_id"] == "uid-1"

    # different sub creates new
    u3 = storage.get_or_create_user(factory, "sub2", "tid1", "b@x.com", "Bob")
    assert u3["user_id"] == "uid-2"


# Rate limit buckets ---------------------------------------------------------


def test_bucket_upsert_and_get(storage):
    assert storage.get_bucket("k") is None
    storage.upsert_bucket("k", 5.0, 1000)
    assert storage.get_bucket("k") == (5.0, 1000)
    storage.upsert_bucket("k", 3.0, 2000)
    assert storage.get_bucket("k") == (3.0, 2000)


# Cross-cutting --------------------------------------------------------------


def test_storage_persists_across_reopens(tmp_path):
    db_path = tmp_path / "state.db"
    s1 = Storage(db_path)
    s1.register_client("persist", None, {"name": "x"}, None)
    s1.close()

    s2 = Storage(db_path)
    assert s2.get_client("persist") is not None
    s2.close()


def test_storage_creates_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "subdir" / "state.db"
    s = Storage(db_path)
    s.close()
    assert db_path.exists()
