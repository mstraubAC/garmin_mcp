"""Unit tests for token-bucket rate limiting and registration guard."""
import time

import pytest

from garmin_mcp.auth.storage import Storage
from garmin_mcp.auth.throttle import RegistrationGuard, TokenBucket


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "state.db")
    yield s
    s.close()


# Token bucket --------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_starts_full(storage):
    bucket = TokenBucket(storage, capacity=3, refill_per_second=1.0)
    for _ in range(3):
        assert await bucket.try_consume("k") is True
    assert await bucket.try_consume("k") is False


@pytest.mark.asyncio
async def test_bucket_refills_over_time(storage, monkeypatch):
    bucket = TokenBucket(storage, capacity=2, refill_per_second=10.0)
    fake_time = [1_000_000]

    def fake_now_ms():
        return fake_time[0]

    monkeypatch.setattr("garmin_mcp.auth.throttle._now_ms", fake_now_ms)

    assert await bucket.try_consume("k")
    assert await bucket.try_consume("k")
    assert await bucket.try_consume("k") is False

    # 1s later, 10 tokens worth of refill — capped at capacity=2
    fake_time[0] += 1000
    assert await bucket.try_consume("k")
    assert await bucket.try_consume("k")
    assert await bucket.try_consume("k") is False


@pytest.mark.asyncio
async def test_bucket_keys_are_isolated(storage):
    bucket = TokenBucket(storage, capacity=1, refill_per_second=0.001)
    assert await bucket.try_consume("a")
    assert await bucket.try_consume("a") is False
    # different key has its own bucket
    assert await bucket.try_consume("b")


@pytest.mark.asyncio
async def test_bucket_persists_across_instances(tmp_path):
    s = Storage(tmp_path / "state.db")
    b1 = TokenBucket(s, capacity=1, refill_per_second=0.0001)
    assert await b1.try_consume("k")
    assert await b1.try_consume("k") is False
    s.close()

    s2 = Storage(tmp_path / "state.db")
    b2 = TokenBucket(s2, capacity=1, refill_per_second=0.0001)
    # bucket state survived restart
    assert await b2.try_consume("k") is False
    s2.close()


def test_bucket_rejects_invalid_config(storage):
    with pytest.raises(ValueError):
        TokenBucket(storage, capacity=0, refill_per_second=1)
    with pytest.raises(ValueError):
        TokenBucket(storage, capacity=1, refill_per_second=0)


# Registration guard --------------------------------------------------------


def _guard(storage, **overrides):
    bucket = TokenBucket(storage, capacity=5, refill_per_second=5 / 3600)
    return RegistrationGuard(storage=storage, per_ip_bucket=bucket, **overrides)


def test_check_shared_token_passes_when_unset(storage):
    g = _guard(storage)
    assert g.check_shared_token(None) is True
    assert g.check_shared_token("anything") is True


def test_check_shared_token_requires_bearer(storage):
    g = _guard(storage, shared_token="secret-abc")
    assert g.check_shared_token(None) is False
    assert g.check_shared_token("secret-abc") is False  # not "Bearer ..."
    assert g.check_shared_token("Bearer secret-abc") is True
    assert g.check_shared_token("Bearer wrong") is False
    assert g.check_shared_token("Bearer  secret-abc  ") is True  # extra spaces


def test_check_redirect_uri_https_or_localhost(storage):
    g = _guard(storage)
    assert g.check_redirect_uri("https://app.example.com/cb") is True
    assert g.check_redirect_uri("http://localhost:8000/cb") is True
    assert g.check_redirect_uri("http://127.0.0.1/cb") is True
    assert g.check_redirect_uri("http://[::1]/cb") is True
    assert g.check_redirect_uri("http://attacker.example.com/cb") is False
    assert g.check_redirect_uri("javascript:alert(1)") is False
    assert g.check_redirect_uri("") is False


def test_check_redirect_uri_length_limit(storage):
    g = _guard(storage, max_field_length=20)
    assert g.check_redirect_uri("https://x/" + "a" * 30) is False


def test_check_field_lengths(storage):
    g = _guard(storage, max_field_length=10)
    assert g.check_field_lengths({"name": "short"}) is True
    assert g.check_field_lengths({"name": "this is way too long for the limit"}) is False
    assert g.check_field_lengths({"non_string": 12345, "name": "ok"}) is True


def test_under_global_cap(storage):
    g = _guard(storage, global_cap=2)
    assert g.under_global_cap() is True
    storage.register_client("c1", None, {}, None)
    assert g.under_global_cap() is True
    storage.register_client("c2", None, {}, None)
    assert g.under_global_cap() is False


@pytest.mark.asyncio
async def test_check_per_ip_uses_bucket(storage):
    g = _guard(storage)
    for _ in range(5):
        assert await g.check_per_ip("1.2.3.4")
    assert await g.check_per_ip("1.2.3.4") is False
    assert await g.check_per_ip("5.6.7.8")  # different IP, fresh bucket
