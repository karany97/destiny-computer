"""Regression guards for scripts/launch-smoke.py.

We can't actually run the smoke against a real fleet from pytest (it
needs docker + Anthropic). These tests verify the script's STRUCTURE:

  - imports cleanly with stdlib-only deps (no httpx / requests creep)
  - has every documented check function
  - _http handles all HTTP error paths gracefully
  - main() returns 1 when any check fails, 0 when all pass
  - env vars are read from the documented names
  - the docstring matches what the script actually does (no drift)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SCRIPT = Path("/tmp/destiny-computer/scripts/launch-smoke.py")


@pytest.fixture
def smoke(monkeypatch):
    """Fresh import of launch-smoke.py with the _results list reset."""
    monkeypatch.delenv("DRIVER_URL", raising=False)
    monkeypatch.delenv("DESTINY_API_TOKEN", raising=False)
    monkeypatch.delenv("SMOKE_RUN_TASK", raising=False)
    spec = importlib.util.spec_from_file_location("smoke", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ────────── module structure ──────────

def test_script_exists():
    assert SCRIPT.exists()
    assert SCRIPT.stat().st_size > 1000  # not a stub


def test_script_is_stdlib_only():
    """Operators run this on a fresh OS — no requests/httpx imports."""
    src = SCRIPT.read_text()
    assert "import requests" not in src
    assert "import httpx" not in src
    assert "from requests" not in src
    assert "from httpx" not in src


def test_smoke_imports_cleanly(smoke):
    # Sanity check: the fixture forced re-import; this would fail on
    # any syntax/import error.
    assert hasattr(smoke, "main")


def test_smoke_defines_documented_checks(smoke):
    """The docstring lists 10 checks. Make sure the corresponding
    functions exist — operators reading the script source shouldn't
    hit "where's the snapshot check?" confusion."""
    expected = [
        "check_health",
        "check_screenshot",
        "check_budget",
        "check_tasks_list",
        "check_run_task",
        "check_snapshot_lifecycle",
    ]
    for name in expected:
        assert hasattr(smoke, name), f"missing check: {name}"


def test_smoke_default_driver_url(smoke):
    assert smoke.DRIVER_URL == "http://127.0.0.1:8090"


def test_smoke_respects_driver_url_env(monkeypatch):
    monkeypatch.setenv("DRIVER_URL", "https://example.com:9090")
    spec = importlib.util.spec_from_file_location("smoke", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DRIVER_URL == "https://example.com:9090"


def test_smoke_strips_trailing_slash_on_url(monkeypatch):
    monkeypatch.setenv("DRIVER_URL", "http://localhost:8090/")
    spec = importlib.util.spec_from_file_location("smoke", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DRIVER_URL == "http://localhost:8090"


def test_smoke_run_task_defaults_false(smoke):
    """Task dispatch costs money — never default to on."""
    assert smoke.RUN_TASK is False


# ────────── _http error handling ──────────

def test_http_returns_zero_status_on_unreachable(smoke):
    """A down driver shouldn't crash — return (0, error_msg, {})."""
    with patch.object(smoke.urllib.request, "urlopen",
                      side_effect=urllib.error.URLError("ECONNREFUSED")):
        code, body, headers = smoke._http("GET", "/health")
    assert code == 0
    assert b"ECONNREFUSED" in body
    assert headers == {}


def test_http_returns_4xx_body_on_http_error(smoke):
    """A 401 should surface the JSON body, not raise."""
    fake_exc = urllib.error.HTTPError(
        url="x", code=401, msg="Unauthorized", hdrs=None,
        fp=type("F", (), {
            "read": lambda self: b'{"detail": "missing token"}',
            "close": lambda self: None,  # avoid PytestUnraisableException
        })(),
    )
    with patch.object(smoke.urllib.request, "urlopen", side_effect=fake_exc):
        code, body, _ = smoke._http("GET", "/sessions")
    assert code == 401
    assert b"missing token" in body


def test_http_sends_bearer_when_token_set(monkeypatch, smoke):
    """When DESTINY_API_TOKEN is set, the Authorization header must
    carry it on every request — critical for gated drivers."""
    monkeypatch.setattr(smoke, "TOKEN", "test-token-xyz")
    seen = {}

    class _FakeResp:
        status = 200
        def read(self): return b"{}"
        @property
        def headers(self): return {"content-type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        return _FakeResp()

    with patch.object(smoke.urllib.request, "urlopen", fake_urlopen):
        smoke._http("GET", "/health")
    auth = seen["headers"].get("Authorization", "")
    assert auth == "Bearer test-token-xyz"


def test_http_does_not_send_authorization_when_token_unset(monkeypatch, smoke):
    monkeypatch.setattr(smoke, "TOKEN", "")
    seen = {}

    class _FakeResp:
        status = 200
        def read(self): return b"{}"
        @property
        def headers(self): return {}
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        return _FakeResp()

    with patch.object(smoke.urllib.request, "urlopen", fake_urlopen):
        smoke._http("GET", "/health")
    # No Authorization header when token isn't set
    assert "Authorization" not in seen["headers"]


def test_http_sends_content_type_json_on_post(monkeypatch, smoke):
    seen = {}

    class _FakeResp:
        status = 202
        def read(self): return b"{}"
        @property
        def headers(self): return {}
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        seen["headers"] = dict(req.headers)
        seen["body"] = req.data
        return _FakeResp()

    with patch.object(smoke.urllib.request, "urlopen", fake_urlopen):
        smoke._http("POST", "/api/task", body={"goal": "do a thing"})
    assert seen["headers"].get("Content-type") == "application/json"
    import json
    assert json.loads(seen["body"]) == {"goal": "do a thing"}


# ────────── _check + summarize ──────────

def test_check_records_results(smoke):
    smoke._results.clear()
    smoke._check("test-pass", True, "ok")
    smoke._check("test-fail", False, "oops")
    assert len(smoke._results) == 2
    assert smoke._results[0][1] is True
    assert smoke._results[1][1] is False


def test_summarize_returns_0_when_all_pass(smoke, capsys):
    smoke._results.clear()
    smoke._check("x", True, "")
    smoke._check("y", True, "")
    rc = smoke._summarize()
    assert rc == 0


def test_summarize_returns_1_when_any_fail(smoke, capsys):
    smoke._results.clear()
    smoke._check("x", True, "")
    smoke._check("y", False, "broken")
    rc = smoke._summarize()
    assert rc == 1


# ────────── docstring / docs drift ──────────

def test_docstring_mentions_skip_default_for_task(smoke):
    """SMOKE_RUN_TASK is a footgun if it defaults on — docstring must
    explicitly say it's off by default."""
    doc = smoke.__doc__ or ""
    assert "SKIP" in doc or "skipped" in doc or "off" in doc


def test_docstring_mentions_exit_code():
    """Operators using this in CI rely on the exit code being a
    meaningful signal. The literal phrase may straddle a newline in
    the source, so we collapse whitespace before matching."""
    doc = " ".join(SCRIPT.read_text().split())
    assert "Exit code" in doc or "exit code" in doc
