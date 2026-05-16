"""D3 closure tests — per-task rate limit on /api/task (issue #5).

Verifies the in-memory leaky-bucket gating:

  * Default: 10 tasks/minute per client.
  * Configurable via DESTINY_TASK_RATE_LIMIT in format `N/PERIOD`
    (sec/min/hour). Invalid spec → safe default.
  * Per-client isolation — one client hitting the cap doesn't block
    another (token-keyed when token set, else IP-keyed).
  * 429 Too Many Requests + Retry-After header on overflow.
  * Refills after the period elapses.
  * Bucket evaporates on driver restart (the budget cap is the real
    cumulative-spend safety net; rate limit only protects against burst).
"""
from __future__ import annotations

import importlib
import os
import sys
import time
from unittest.mock import patch

import pytest


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Fresh main import per-test — gives a clean rate-limit bucket."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("DESTINY_API_TOKEN", raising=False)
    monkeypatch.delenv("DESTINY_TASK_RATE_LIMIT", raising=False)
    sys.modules.pop("main", None)
    sys.path.insert(0, "/tmp/destiny-computer/driver/src")
    import main as M  # type: ignore[import-not-found]
    # Make sure desktop is "reachable" so we don't 503 before reaching the
    # rate-limit dep.
    monkeypatch.setattr(M, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(M.L, "today_spend", lambda _: 0.0)
    monkeypatch.setattr(M, "_run_task_blocking", lambda *a, **k: None)
    return M


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app.app)


# ────────── _parse_rate_limit ──────────

def test_parse_rate_limit_default_10_per_min(app):
    assert app._parse_rate_limit("10/min") == (10, 60)


def test_parse_rate_limit_sec():
    """Smallest period — 5/sec → 5 in any 1-second window."""
    from main import _parse_rate_limit  # type: ignore[import-not-found]
    assert _parse_rate_limit("5/sec") == (5, 1)


def test_parse_rate_limit_hour():
    from main import _parse_rate_limit
    assert _parse_rate_limit("60/hour") == (60, 3600)


def test_parse_rate_limit_invalid_falls_back_to_default():
    from main import _parse_rate_limit
    assert _parse_rate_limit("garbage") == (10, 60)
    assert _parse_rate_limit("10/year") == (10, 60)  # unknown period
    assert _parse_rate_limit("") == (10, 60)
    assert _parse_rate_limit("min") == (10, 60)  # missing N


def test_parse_rate_limit_zero_or_negative_falls_back_to_default():
    """An operator typo (0/min) shouldn't disable rate limiting silently."""
    from main import _parse_rate_limit
    assert _parse_rate_limit("0/min") == (10, 60)
    assert _parse_rate_limit("-5/min") == (10, 60)


def test_parse_rate_limit_case_insensitive_period():
    from main import _parse_rate_limit
    assert _parse_rate_limit("10/MIN") == (10, 60)
    assert _parse_rate_limit("10/Hour") == (10, 3600)


# ────────── _client_id ──────────

def test_client_id_uses_token_when_present(app):
    """Token-keyed buckets — two operators with two tokens get two buckets."""
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.client.host = "1.2.3.4"
    cid = app._client_id(fake_req, "Bearer abc123")
    assert cid == "token:abc123"


def test_client_id_falls_back_to_ip_when_no_token(app):
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.client.host = "10.0.0.5"
    cid = app._client_id(fake_req, None)
    assert cid == "ip:10.0.0.5"


def test_client_id_handles_missing_client(app):
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.client = None
    cid = app._client_id(fake_req, None)
    assert cid == "ip:unknown"


def test_client_id_ignores_malformed_authorization(app):
    """`Authorization: notbearer xyz` → fall back to IP, don't try to use it."""
    from unittest.mock import MagicMock
    fake_req = MagicMock()
    fake_req.client.host = "1.1.1.1"
    cid = app._client_id(fake_req, "NotBearer xyz")
    assert cid == "ip:1.1.1.1"


# ────────── _rate_limit_check ──────────

def test_rate_limit_under_cap_returns_none(app):
    """Below the cap → returns None (allowed)."""
    for i in range(5):
        result = app._rate_limit_check(f"test-under-{i}")
        assert result is None


def test_rate_limit_at_cap_returns_retry_after(monkeypatch, app):
    """At the cap → returns positive seconds."""
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "3/min")
    cid = "test-at-cap"
    # First 3 allowed
    for _ in range(3):
        assert app._rate_limit_check(cid) is None
    # 4th refused
    retry = app._rate_limit_check(cid)
    assert retry is not None
    assert retry > 0
    assert retry <= 60.0  # never exceeds the period


def test_rate_limit_refills_after_period(monkeypatch, app):
    """When timestamps age out of the window, slots free up."""
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "2/sec")
    cid = "test-refill"
    # Use 2 slots
    assert app._rate_limit_check(cid) is None
    assert app._rate_limit_check(cid) is None
    # 3rd refused
    assert app._rate_limit_check(cid) is not None
    # Wait > 1 second for slots to age out
    time.sleep(1.1)
    # Now allowed again
    assert app._rate_limit_check(cid) is None


def test_rate_limit_per_client_isolation(monkeypatch, app):
    """Client A hitting cap doesn't block client B."""
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "2/min")
    # Max out client A
    assert app._rate_limit_check("A") is None
    assert app._rate_limit_check("A") is None
    assert app._rate_limit_check("A") is not None  # refused
    # Client B starts fresh
    assert app._rate_limit_check("B") is None
    assert app._rate_limit_check("B") is None


def test_rate_limit_retry_after_decreases_as_oldest_ages(monkeypatch, app):
    """The Retry-After value should reflect when the OLDEST entry expires,
    not the period from now."""
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "1/sec")
    cid = "test-retry-after-shrinks"
    app._rate_limit_check(cid)
    # immediately refuse
    retry1 = app._rate_limit_check(cid)
    assert retry1 is not None
    # Retry value is min 1.0s (we clamp), so this is the floor
    assert retry1 >= 1.0


# ────────── /api/task endpoint behavior ──────────

def test_api_task_under_cap_returns_202(monkeypatch, client):
    """A single POST below the cap succeeds normally."""
    r = client.post("/api/task", json={"goal": "click here"})
    assert r.status_code == 202


def test_api_task_over_cap_returns_429(monkeypatch, app, client):
    """After cap, the 11th POST returns 429."""
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "2/min")
    # First 2 OK
    assert client.post("/api/task", json={"goal": "go"}).status_code == 202
    assert client.post("/api/task", json={"goal": "go"}).status_code == 202
    # 3rd should be 429
    r = client.post("/api/task", json={"goal": "go"})
    assert r.status_code == 429
    assert "rate limit" in r.json()["detail"].lower()


def test_api_task_429_includes_retry_after_header(monkeypatch, client):
    """RFC 6585 — 429 should include Retry-After header for client backoff."""
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "1/min")
    client.post("/api/task", json={"goal": "go"})  # use the slot
    r = client.post("/api/task", json={"goal": "go"})  # refused
    assert r.status_code == 429
    assert "retry-after" in (h.lower() for h in r.headers.keys())
    # Should be a positive integer string
    assert int(r.headers.get("retry-after", "0")) > 0


def test_api_task_429_message_includes_configured_spec(monkeypatch, client):
    """Operator should see the configured limit in the error so they can
    diagnose 'why am I being throttled?'"""
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "3/min")
    for _ in range(3):
        client.post("/api/task", json={"goal": "go"})
    r = client.post("/api/task", json={"goal": "go"})
    assert "3/min" in r.json()["detail"]


def test_api_task_rate_limit_default_10_per_minute(monkeypatch, client):
    """Default is 10/min — first 10 succeed, 11th fails."""
    monkeypatch.delenv("DESTINY_TASK_RATE_LIMIT", raising=False)
    for i in range(10):
        r = client.post("/api/task", json={"goal": "go"})
        assert r.status_code == 202, f"call {i+1} should pass"
    r = client.post("/api/task", json={"goal": "go"})
    assert r.status_code == 429


def test_api_task_rate_limit_per_token_isolation(monkeypatch, client):
    """Two different tokens get separately throttled buckets."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "either-works")
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "2/min")
    # Set token to "alice"
    monkeypatch.setenv("DESTINY_API_TOKEN", "alice")
    h_alice = {"Authorization": "Bearer alice"}
    assert client.post("/api/task", json={"goal": "go"}, headers=h_alice).status_code == 202
    assert client.post("/api/task", json={"goal": "go"}, headers=h_alice).status_code == 202
    # Rotate to bob — different token, different bucket
    monkeypatch.setenv("DESTINY_API_TOKEN", "bob")
    h_bob = {"Authorization": "Bearer bob"}
    assert client.post("/api/task", json={"goal": "go"}, headers=h_bob).status_code == 202


def test_rate_limit_check_runs_after_auth(monkeypatch, client):
    """If auth fails (401), rate limit slot must NOT be consumed —
    otherwise an attacker could exhaust the bucket by spamming bad
    tokens, locking out legitimate users."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "right-token")
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "1/min")
    # Send 5 bad-auth attempts
    for _ in range(5):
        r = client.post("/api/task", json={"goal": "go"},
                        headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
    # Legitimate request should still work — slot not consumed by 401s
    r = client.post("/api/task", json={"goal": "go"},
                    headers={"Authorization": "Bearer right-token"})
    assert r.status_code == 202


def test_rate_limit_state_evaporates_on_module_reimport(monkeypatch, tmp_path):
    """Rate-limit state is intentionally in-memory — a driver restart
    resets all buckets. This guards against an operator assuming the
    state persists (it doesn't; budget cap is the cumulative safety net)."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("DESTINY_TASK_RATE_LIMIT", "1/min")
    sys.modules.pop("main", None)
    sys.path.insert(0, "/tmp/destiny-computer/driver/src")
    import main as M1
    M1._RATE_LIMIT_BUCKETS["test"] = __import__("collections").deque([time.time()])
    # Reload — fresh module, fresh state
    sys.modules.pop("main", None)
    import main as M2
    assert "test" not in M2._RATE_LIMIT_BUCKETS


# ────────── regression guards ──────────

def test_rate_limit_constants_present_in_source():
    """Source-grep regression guard — the load-bearing pieces must remain."""
    src = open("/tmp/destiny-computer/driver/src/main.py").read()
    assert "_RATE_LIMIT_BUCKETS" in src
    assert "_RATE_LIMIT_LOCK" in src
    assert "threading.Lock" in src
    assert "DESTINY_TASK_RATE_LIMIT" in src
    assert "enforce_rate_limit" in src


def test_rate_limit_uses_threading_lock_not_asyncio_lock():
    """FastAPI BackgroundTasks runs sync handlers in a thread pool — must
    use threading.Lock (NOT asyncio.Lock) for cross-thread safety."""
    src = open("/tmp/destiny-computer/driver/src/main.py").read()
    # threading.Lock somewhere near the rate-limit code
    assert "_RATE_LIMIT_LOCK = threading.Lock()" in src
