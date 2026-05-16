"""Coverage for main.py FastAPI endpoints.

Uses FastAPI's TestClient — no real docker, no real Anthropic, no real
KasmVNC. Each test stubs the bits it needs (desktop reachability, model
calls, ledger contents).

Endpoints under test:
  GET    /health
  GET    /screenshot
  POST   /api/task
  GET    /api/task/{id}
  GET    /api/task/{id}/stream  (smoke — stream lifecycle is heavy)
  GET    /api/tasks
  GET    /api/budget
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Fresh import of main with state dir pointed at tmp_path.

    Pop ONLY `main` from sys.modules — test_loop_ledger.py relies on
    `importlib.reload(L)` against the loop module, and popping `loop`
    here breaks the reload (sys.modules name mismatch).
    """
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    sys.modules.pop("main", None)
    sys.path.insert(0, "/tmp/destiny-computer/driver/src")
    import main as M  # type: ignore[import-not-found]
    return M


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app.app)


# ────────── /health ──────────

def test_health_returns_200(monkeypatch, app, client):
    """Even when desktop is unreachable, /health returns 200 (it just
    reports `ok: false`)."""
    monkeypatch.setattr(app, "_desktop_reachable", lambda: False)
    r = client.get("/health")
    assert r.status_code == 200


def test_health_includes_service_metadata(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    body = client.get("/health").json()
    assert body["service"] == "destiny-computer-driver"
    assert "version" in body
    assert "model" in body
    assert "desktop_container" in body
    assert "max_steps_per_task" in body
    assert "max_usd_per_day" in body


def test_health_ok_field_tracks_reachability(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: False)
    assert client.get("/health").json()["ok"] is False
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    assert client.get("/health").json()["ok"] is True


def test_health_today_spend_field_present(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    body = client.get("/health").json()
    assert "today_spend_usd" in body
    assert isinstance(body["today_spend_usd"], (int, float))


# ────────── /screenshot ──────────

def test_screenshot_503_when_desktop_unreachable(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: False)
    r = client.get("/screenshot")
    assert r.status_code == 503


def test_screenshot_returns_png_bytes(monkeypatch, app, client):
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.D, "screenshot", lambda: fake_png)
    r = client.get("/screenshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == fake_png


def test_screenshot_500_on_desktop_error(monkeypatch, app, client):
    """A xwd|convert failure during screenshot → 500 with the message."""
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.D, "screenshot",
                        MagicMock(side_effect=app.D.DesktopError("xwd failed")))
    r = client.get("/screenshot")
    assert r.status_code == 500
    assert "xwd failed" in r.json()["detail"]


# ────────── POST /api/task ──────────

def test_submit_task_503_when_desktop_unreachable(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: False)
    r = client.post("/api/task", json={"goal": "do a thing"})
    assert r.status_code == 503


def test_submit_task_402_when_over_budget(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 999.0)
    r = client.post("/api/task", json={"goal": "do a thing"})
    assert r.status_code == 402
    assert "already reached" in r.json()["detail"]


def test_submit_task_500_when_no_anthropic_key(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post("/api/task", json={"goal": "do a thing"})
    assert r.status_code == 500
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_submit_task_returns_202_with_task_id(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    # Don't actually run the loop
    monkeypatch.setattr(app, "_run_task_blocking", lambda *a, **k: None)
    r = client.post("/api/task", json={"goal": "click here"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "running"
    assert body["task_id"].startswith("task_")
    assert "stream_url" in body
    assert "transcript_url" in body


def test_submit_task_uses_default_max_steps(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    monkeypatch.setattr(app, "_run_task_blocking", lambda *a, **k: None)
    body = client.post("/api/task", json={"goal": "click here"}).json()
    assert body["max_steps"] == app.MAX_STEPS


def test_submit_task_respects_per_request_max_steps_override(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    monkeypatch.setattr(app, "_run_task_blocking", lambda *a, **k: None)
    body = client.post("/api/task", json={"goal": "go", "max_steps": 10}).json()
    assert body["max_steps"] == 10


# ─────── /api/task payload validation ───────

def test_submit_task_missing_goal_returns_422(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    r = client.post("/api/task", json={})
    assert r.status_code == 422


def test_submit_task_goal_too_short_returns_422(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    r = client.post("/api/task", json={"goal": "x"})  # 1 char, min is 2
    assert r.status_code == 422


def test_submit_task_goal_too_long_returns_422(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    r = client.post("/api/task", json={"goal": "x" * 2001})  # max is 2000
    assert r.status_code == 422


def test_submit_task_max_steps_zero_returns_422(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    r = client.post("/api/task", json={"goal": "go", "max_steps": 0})
    assert r.status_code == 422


def test_submit_task_max_steps_negative_returns_422(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    r = client.post("/api/task", json={"goal": "go", "max_steps": -5})
    assert r.status_code == 422


def test_submit_task_max_steps_too_large_returns_422(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    r = client.post("/api/task", json={"goal": "go", "max_steps": 201})
    assert r.status_code == 422


def test_submit_task_context_too_long_returns_422(monkeypatch, app, client):
    monkeypatch.setattr(app, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(app.L, "today_spend", lambda _: 0.0)
    r = client.post("/api/task", json={
        "goal": "go",
        "context": "x" * 4001,  # max 4000
    })
    assert r.status_code == 422


# ─────── /api/task/{id} ───────

def test_get_task_returns_starting_when_unknown(client):
    """Unknown task → starting placeholder (not 404), so the chat can render."""
    r = client.get("/api/task/unknown-id-xyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "starting"
    assert body["task_id"] == "unknown-id-xyz"
    assert body["step_count"] == 0


def test_get_task_returns_transcript_when_exists(tmp_path, monkeypatch, app, client):
    """When the transcript file exists, return its contents verbatim."""
    transcript = {
        "task_id": "t1", "status": "completed", "step_count": 5,
        "total_cost_usd": 0.42, "goal": "click", "final_text": "done",
        "started_at": 1000.0, "finished_at": 1050.0, "error": None,
        "steps": [],
    }
    (app.TASKS_DIR / "t1.json").write_text(json.dumps(transcript))
    r = client.get("/api/task/t1")
    assert r.status_code == 200
    assert r.json() == transcript


def test_get_task_returns_500_on_malformed_transcript(tmp_path, app, client):
    (app.TASKS_DIR / "broken.json").write_text("not-json")
    r = client.get("/api/task/broken")
    assert r.status_code == 500


# ─────── /api/tasks ───────

def test_list_tasks_returns_empty_when_no_history(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200
    assert r.json() == {"tasks": []}


def test_list_tasks_includes_submitted_status_when_no_transcript_yet(app, client):
    """A task indexed but not yet running shows status=submitted."""
    row = {"id": "t1", "goal": "click", "submitted_at": 1000.0}
    with app.LEGACY_TASKS_FILE.open("a") as f:
        f.write(json.dumps(row) + "\n")
    body = client.get("/api/tasks").json()
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["status"] == "submitted"


def test_list_tasks_merges_transcript_status(app, client):
    """If a transcript exists, its status/step_count/cost are merged in."""
    row = {"id": "t2", "goal": "click", "submitted_at": 1000.0}
    with app.LEGACY_TASKS_FILE.open("a") as f:
        f.write(json.dumps(row) + "\n")
    transcript = {"status": "completed", "step_count": 3, "total_cost_usd": 0.05}
    (app.TASKS_DIR / "t2.json").write_text(json.dumps(transcript))
    body = client.get("/api/tasks").json()
    assert body["tasks"][0]["status"] == "completed"
    assert body["tasks"][0]["step_count"] == 3
    assert body["tasks"][0]["total_cost_usd"] == 0.05


def test_list_tasks_respects_limit(app, client):
    for i in range(30):
        with app.LEGACY_TASKS_FILE.open("a") as f:
            f.write(json.dumps({"id": f"t{i}", "goal": "g", "submitted_at": i}) + "\n")
    body = client.get("/api/tasks?limit=5").json()
    assert len(body["tasks"]) == 5


def test_list_tasks_skips_malformed_index_lines(app, client):
    """A corrupted line in tasks.jsonl shouldn't break the listing."""
    app.LEGACY_TASKS_FILE.write_text(
        json.dumps({"id": "good", "goal": "g", "submitted_at": 1}) + "\n"
        + "garbage line\n"
        + json.dumps({"id": "good2", "goal": "g", "submitted_at": 2}) + "\n"
    )
    body = client.get("/api/tasks").json()
    ids = [r["id"] for r in body["tasks"]]
    assert "good" in ids and "good2" in ids


# ─────── /api/budget ───────

def test_budget_returns_zero_when_no_ledger(client):
    r = client.get("/api/budget")
    assert r.status_code == 200
    body = r.json()
    assert body["total_usd"] == 0.0
    assert body["tasks_run"] == 0


def test_budget_returns_required_fields(client):
    body = client.get("/api/budget").json()
    for key in ("date", "tasks_run", "total_usd", "cap_usd",
                "remaining_usd", "per_task_usd"):
        assert key in body


def test_budget_sums_per_task_breakdown(app, client):
    today = app.L._today_iso()
    with app.LEDGER_FILE.open("w") as f:
        f.write(json.dumps({"date": today, "task_id": "t1", "usd": 0.10}) + "\n")
        f.write(json.dumps({"date": today, "task_id": "t1", "usd": 0.05}) + "\n")
        f.write(json.dumps({"date": today, "task_id": "t2", "usd": 0.20}) + "\n")
    body = client.get("/api/budget").json()
    assert body["total_usd"] == pytest.approx(0.35)
    assert body["per_task_usd"]["t1"] == pytest.approx(0.15)
    assert body["per_task_usd"]["t2"] == pytest.approx(0.20)
    assert body["tasks_run"] == 2


def test_budget_excludes_yesterday(app, client):
    """Only today's spend counts toward the cap — yesterday's reset at midnight."""
    today = app.L._today_iso()
    with app.LEDGER_FILE.open("w") as f:
        f.write(json.dumps({"date": today, "task_id": "t1", "usd": 0.05}) + "\n")
        f.write(json.dumps({"date": "2020-01-01", "task_id": "old", "usd": 999.0}) + "\n")
    body = client.get("/api/budget").json()
    assert body["total_usd"] == pytest.approx(0.05)


def test_budget_remaining_never_negative(app, client):
    """If today's spend is over cap, remaining shows 0 (not negative)."""
    today = app.L._today_iso()
    with app.LEDGER_FILE.open("w") as f:
        f.write(json.dumps({"date": today, "task_id": "t1", "usd": 9999.0}) + "\n")
    body = client.get("/api/budget").json()
    assert body["remaining_usd"] == 0.0


def test_budget_skips_malformed_ledger_lines(app, client):
    """A bad row in the ledger shouldn't break the report."""
    today = app.L._today_iso()
    app.LEDGER_FILE.write_text(
        "not-json\n"
        + json.dumps({"date": today, "usd": 0.05}) + "\n"
    )
    body = client.get("/api/budget").json()
    assert body["total_usd"] == pytest.approx(0.05)


# ─────── helpers ───────

def test_docker_available_with_shutil(monkeypatch, app):
    """Sanity — _docker_available delegates to shutil.which."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/docker" if x == "docker" else None)
    assert app._docker_available() is True
    monkeypatch.setattr(shutil, "which", lambda x: None)
    assert app._docker_available() is False


def test_desktop_reachable_false_when_docker_missing(monkeypatch, app):
    monkeypatch.setattr(app, "_docker_available", lambda: False)
    assert app._desktop_reachable() is False


def test_transcript_path_format(app):
    p = app._transcript_path("task_123")
    assert p.name == "task_123.json"
    assert p.parent == app.TASKS_DIR


def test_index_task_appends_to_legacy_file(app):
    app._index_task("t1", "click here")
    rows = [json.loads(l) for l in app.LEGACY_TASKS_FILE.read_text().splitlines()]
    assert rows[-1]["id"] == "t1"
    assert rows[-1]["goal"] == "click here"
    assert "submitted_at" in rows[-1]


def test_index_task_appends_multiple(app):
    app._index_task("t1", "g1")
    app._index_task("t2", "g2")
    rows = [json.loads(l) for l in app.LEGACY_TASKS_FILE.read_text().splitlines()]
    assert [r["id"] for r in rows[-2:]] == ["t1", "t2"]


# ─────── app metadata ───────

def test_app_title_and_version(app):
    assert app.app.title == "destiny-computer-driver"
    assert app.app.version == "0.2.0"
