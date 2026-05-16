"""Coverage for the optional Bearer-token auth on destiny-computer.

Mirrors the atelier-os PR #13 test suite (test_api_auth.py) so both
repos enforce identical auth semantics. Differences:
  - env var name is DESTINY_API_TOKEN (not ATELIER_API_TOKEN) so
    operators running both fleets can rotate them independently
  - protected endpoints: /screenshot + every /api/*
  - open endpoints: /health only (no /budget — destiny-computer's
    /budget is a /api/budget already gated, no in-container poller to
    accommodate)
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Fresh import of main with state dir + no token by default.

    We pop ONLY `main` from sys.modules (not desktop or loop) so other
    test files using `importlib.reload(L)` against the loop module
    still find a valid module to reload. Cross-file test isolation was
    a real bug here — see commit log.
    """
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("DESTINY_API_TOKEN", raising=False)
    sys.modules.pop("main", None)
    sys.path.insert(0, "/tmp/destiny-computer/driver/src")
    import main as M  # type: ignore[import-not-found]
    return M


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app.app)


# ────────── back-compat: no token env → all endpoints open ──────────

def test_no_token_env_health_open(client):
    assert client.get("/health").status_code == 200


def test_no_token_env_tasks_list_open(client):
    assert client.get("/api/tasks").status_code == 200


def test_no_token_env_budget_open(client):
    assert client.get("/api/budget").status_code == 200


def test_no_token_env_task_get_returns_starting_not_401(client):
    r = client.get("/api/task/anything")
    assert r.status_code == 200
    assert r.json()["status"] == "starting"


# ────────── with token: protected endpoints require it ──────────

def test_token_required_tasks_list_401(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/tasks")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    assert "missing Authorization" in r.json()["detail"]


def test_token_required_budget_401(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    assert client.get("/api/budget").status_code == 401


def test_token_required_task_get_401(monkeypatch, client):
    """Auth must short-circuit BEFORE the 'starting' placeholder — otherwise
    an unauth'd caller can enumerate task ids by watching for the
    starting-vs-401 response difference."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    assert client.get("/api/task/any-id").status_code == 401


def test_token_required_task_stream_401(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    assert client.get("/api/task/any-id/stream").status_code == 401


def test_token_required_task_post_401(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.post("/api/task", json={"goal": "x" * 5})
    assert r.status_code == 401


def test_token_required_screenshot_401(monkeypatch, client):
    """Screenshots leak the desktop's visible state (browser tabs, files,
    terminal scrollback) — gated when a token is set."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    assert client.get("/screenshot").status_code == 401


# ────────── /health stays open even with token set ──────────

def test_health_open_even_with_token_set(monkeypatch, client):
    """/health must stay open — Docker healthchecks + external monitoring
    rely on it; gating would force shipping the token into every
    healthcheck wrapper."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    assert client.get("/health").status_code == 200


# ────────── wrong-scheme / wrong-token rejection ──────────

def test_wrong_token_401(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/tasks", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"]


def test_missing_bearer_prefix_401(monkeypatch, client):
    """Authorization: s3cret (no scheme) — must not be silently accepted."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/tasks", headers={"Authorization": "s3cret"})
    assert r.status_code == 401
    assert "Bearer" in r.json()["detail"]


def test_basic_auth_scheme_401(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/tasks", headers={"Authorization": "Basic abcdef"})
    assert r.status_code == 401


def test_query_param_token_not_accepted(monkeypatch, client):
    """Token in query string MUST be rejected — would leak via access
    logs, browser history, Referer headers."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    assert client.get("/api/tasks?token=s3cret").status_code == 401


def test_lowercase_bearer_scheme_accepted(monkeypatch, client):
    """RFC 6750 §2.1: scheme is case-insensitive."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/tasks", headers={"Authorization": "bearer s3cret"})
    assert r.status_code == 200


# ────────── right token works ──────────

def test_right_token_tasks_works(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/tasks", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


def test_right_token_screenshot_works(monkeypatch, app, client):
    """With correct token AND a reachable desktop, screenshot still works."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.D, "screenshot", lambda: b"\x89PNG-bytes")
    r = client.get("/screenshot", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_right_token_budget_works(monkeypatch, client):
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/budget", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert "total_usd" in r.json()


# ────────── env-read-at-request-time (rotation works live) ──────────

def test_token_env_change_takes_effect_without_reimport(monkeypatch, client):
    """Adding DESTINY_API_TOKEN to a running fleet → immediately effective.
    Critical for uvicorn --reload + tests that mutate env mid-suite."""
    # 1) open
    assert client.get("/api/tasks").status_code == 200
    # 2) set token → 401 without header
    monkeypatch.setenv("DESTINY_API_TOKEN", "rotated")
    assert client.get("/api/tasks").status_code == 401
    # 3) right token works
    r = client.get("/api/tasks", headers={"Authorization": "Bearer rotated"})
    assert r.status_code == 200
    # 4) clear → open again
    monkeypatch.delenv("DESTINY_API_TOKEN", raising=False)
    assert client.get("/api/tasks").status_code == 200


def test_token_rotation_invalidates_old_token(monkeypatch, client):
    """Operator rotates token → old token immediately stops working."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "old")
    assert client.get("/api/tasks", headers={"Authorization": "Bearer old"}).status_code == 200

    monkeypatch.setenv("DESTINY_API_TOKEN", "new")
    assert client.get("/api/tasks", headers={"Authorization": "Bearer old"}).status_code == 401
    assert client.get("/api/tasks", headers={"Authorization": "Bearer new"}).status_code == 200


# ────────── constant-time + hygiene guards ──────────

def test_constant_time_comparison_used():
    """hmac.compare_digest must appear in main.py — regression guard
    against a future refactor flipping to == comparison."""
    src = open("/tmp/destiny-computer/driver/src/main.py").read()
    assert "hmac.compare_digest" in src, \
        "require_token must use hmac.compare_digest to defeat timing attacks"


def test_one_char_token_handled_gracefully(monkeypatch, client):
    """compare_digest must not crash on length mismatch."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "s3cret")
    r = client.get("/api/tasks", headers={"Authorization": "Bearer a"})
    assert r.status_code == 401  # invalid, never crashes


def test_empty_string_token_disables_auth(monkeypatch, client):
    """DESTINY_API_TOKEN='' is back-compat with unset (operators who
    `export DESTINY_API_TOKEN=` shouldn't lock themselves out)."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "")
    assert client.get("/api/tasks").status_code == 200


def test_failed_auth_does_not_leak_real_token_in_body(monkeypatch, client):
    """The 401 body must never echo the configured secret."""
    monkeypatch.setenv("DESTINY_API_TOKEN", "real-secret-abc-123")
    r = client.get("/api/tasks", headers={"Authorization": "Bearer guessed"})
    assert "real-secret-abc-123" not in r.text


# ────────── env-var-name mirror check (different from atelier-os) ──────────

def test_uses_destiny_api_token_env_not_atelier(monkeypatch, client):
    """The env name is intentionally DIFFERENT from atelier-os's
    ATELIER_API_TOKEN so an operator running both fleets on the same
    host can rotate them independently. Setting ATELIER_API_TOKEN
    here must NOT gate destiny-computer."""
    monkeypatch.setenv("ATELIER_API_TOKEN", "atelier-only")
    monkeypatch.delenv("DESTINY_API_TOKEN", raising=False)
    # Destiny stays open — only its own env name matters
    assert client.get("/api/tasks").status_code == 200
