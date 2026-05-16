"""End-to-end coverage for loop.run_task — the autonomous loop itself.

The loop ties screenshot → model → action → screenshot together. These
tests stub Anthropic's `client.beta.messages.create` (no real API call)
and desktop.py (no real container), then exercise every status the loop
can finish in:

  - completed              (model declared done)
  - api_error              (Anthropic SDK raised APIError)
  - desktop_error          (caught into a tool_result, loop continues)
  - budget_exceeded_steps  (hit max_steps without completion)
  - budget_exceeded_usd    (mid-task cost cap hit)
  - early-refuse           (pre-flight detects over-cap, doesn't start)

The fake message object lets us thread arbitrary tool_use blocks through
the dispatcher without booting a real Anthropic client.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import desktop as D  # type: ignore[import-not-found]
import loop as L  # type: ignore[import-not-found]


# ────────── fake Anthropic response helpers ──────────

def _block(typ, **kw):
    """Construct an MagicMock that quacks like an Anthropic ContentBlock."""
    b = MagicMock()
    b.type = typ
    b.model_dump = MagicMock(return_value={"type": typ, **kw})
    for k, v in kw.items():
        setattr(b, k, v)
    return b


def _tool_use(action_input, tool_use_id="tu_1"):
    """A tool_use block with the given action input dict."""
    b = _block("tool_use", id=tool_use_id, input=action_input)
    return b


def _text(text):
    return _block("text", text=text)


def _fake_resp(*content_blocks, in_tokens=100, out_tokens=20):
    """Build a fake messages.create() response."""
    r = MagicMock()
    r.content = list(content_blocks)
    r.usage = MagicMock()
    r.usage.input_tokens = in_tokens
    r.usage.output_tokens = out_tokens
    return r


@pytest.fixture
def stub_screenshot(monkeypatch):
    """desktop.screenshot returns 8 bytes; get_screen_size returns 1280x720."""
    monkeypatch.setattr(D, "screenshot", lambda: b"\x89PNG\x00\x00\x00\x01")
    monkeypatch.setattr(D, "get_screen_size", lambda: (1280, 720))


@pytest.fixture
def stub_anthropic(monkeypatch):
    """Stub the Anthropic client constructor + messages.create. Returns
    a list of fake_resp responses that the loop will consume one per step.

    Post-D5b (issue #11): the Anthropic SDK instantiation moved from
    loop.py to vision.AnthropicVisionBackend. We patch
    `vision.Anthropic` so when AnthropicVisionBackend() is constructed
    inside run_task, it gets the fake client.

    Also force VISION_BACKEND=anthropic so the loop routes through
    AnthropicVisionBackend (the default, but be explicit so a stray
    env var in CI doesn't flip these tests onto the Holo3 path)."""
    monkeypatch.setenv("VISION_BACKEND", "anthropic")
    responses: list = []

    def _create(**kwargs):
        if not responses:
            raise AssertionError("test exhausted the fake responses queue")
        return responses.pop(0)

    fake_client = MagicMock()
    fake_client.beta.messages.create = _create
    import vision as V  # type: ignore[import-not-found]
    monkeypatch.setattr(V, "Anthropic", lambda **_: fake_client)
    return responses  # mutate to script behavior


# ────────── completion path ──────────

def test_run_task_completes_on_text_only_response(tmp_path, stub_screenshot, stub_anthropic):
    """Model emits a text block with no tool_use → status='completed'."""
    stub_anthropic.append(_fake_resp(_text("done!")))
    transcript = L.run_task(
        goal="open browser",
        task_id="t1",
        transcript_file=tmp_path / "t1.json",
        ledger_file=tmp_path / "led.jsonl",
        max_steps=5,
        max_usd_per_day=10.0,
    )
    assert transcript.status == "completed"
    assert transcript.final_text == "done!"
    assert transcript.finished_at is not None


def test_run_task_writes_transcript_file(tmp_path, stub_screenshot, stub_anthropic):
    stub_anthropic.append(_fake_resp(_text("ok")))
    tp = tmp_path / "t1.json"
    L.run_task(
        goal="g", task_id="t1",
        transcript_file=tp, ledger_file=tmp_path / "l.jsonl",
        max_steps=5, max_usd_per_day=10.0,
    )
    assert tp.exists()
    data = json.loads(tp.read_text())
    assert data["status"] == "completed"
    assert data["task_id"] == "t1"
    assert "steps" in data


def test_run_task_accumulates_cost(tmp_path, stub_screenshot, stub_anthropic):
    """Each model call adds to total_cost_usd."""
    stub_anthropic.append(_fake_resp(_text("done"), in_tokens=1_000_000, out_tokens=0))
    transcript = L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
        max_steps=5, max_usd_per_day=100.0,
    )
    # 1M sonnet input tokens = $3
    assert transcript.total_cost_usd == pytest.approx(3.0)


def test_run_task_logs_cost_to_ledger(tmp_path, stub_screenshot, stub_anthropic):
    stub_anthropic.append(_fake_resp(_text("done"), in_tokens=10_000, out_tokens=500))
    ledger = tmp_path / "l.jsonl"
    L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=ledger,
        max_steps=5, max_usd_per_day=10.0,
    )
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["task_id"] == "t1"


# ────────── pre-flight cap check ──────────

def test_run_task_refuses_when_over_cap(tmp_path, stub_screenshot, stub_anthropic):
    """If today's spend already ≥ cap → status='budget_exceeded_usd', no
    model call attempted (we don't pop a response off the queue)."""
    ledger = tmp_path / "l.jsonl"
    # Pre-load today's spend over the cap
    L._record_cost(ledger, "prior", 5.0, 1)
    transcript = L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=ledger,
        max_steps=5, max_usd_per_day=1.0,
    )
    assert transcript.status == "budget_exceeded_usd"
    assert "already reached" in transcript.error
    # No responses consumed
    assert len(stub_anthropic) == 0


def test_run_task_refuses_at_exact_cap(tmp_path, stub_screenshot, stub_anthropic):
    """Exactly at cap (>=) should also refuse — boundary case."""
    ledger = tmp_path / "l.jsonl"
    L._record_cost(ledger, "prior", 1.0, 1)
    transcript = L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=ledger,
        max_steps=5, max_usd_per_day=1.0,
    )
    assert transcript.status == "budget_exceeded_usd"


# ────────── api_error path ──────────

def test_run_task_handles_anthropic_api_error(tmp_path, stub_screenshot, monkeypatch):
    """When the Anthropic SDK raises APIError, we mark api_error + return."""
    from anthropic import APIError

    fake_client = MagicMock()
    # APIError requires (message, request, body) in newer SDKs; mock the type
    # check loosely by raising a subclass
    class FakeAPIError(APIError):
        def __init__(self):
            self.message = "rate limited"
        def __str__(self):
            return self.message

    fake_client.beta.messages.create = MagicMock(side_effect=FakeAPIError())
    import vision as V  # type: ignore[import-not-found]
    monkeypatch.setenv("VISION_BACKEND", "anthropic")
    monkeypatch.setattr(V, "Anthropic", lambda **_: fake_client)

    transcript = L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
        max_steps=5, max_usd_per_day=10.0,
    )
    assert transcript.status == "api_error"
    assert "rate limited" in transcript.error


# ────────── max_steps reached ──────────

def test_run_task_hits_max_steps(tmp_path, stub_screenshot, stub_anthropic):
    """Model never stops → loop hits max_steps + reports
    budget_exceeded_steps with the partial transcript."""
    # Stub returns tool_use forever; we need max_steps + 1 to be safe
    for _ in range(10):
        stub_anthropic.append(_fake_resp(
            _tool_use({"action": "mouse_move", "coordinate": [0, 0]}, f"tu_{_}"),
            in_tokens=10, out_tokens=5,
        ))

    with patch.object(D, "mouse_move", return_value=D.ActionResult(ok=True, message="moved")):
        transcript = L.run_task(
            goal="g", task_id="t1",
            transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
            max_steps=3, max_usd_per_day=10.0,
        )
    assert transcript.status == "budget_exceeded_steps"
    assert "max_steps=3" in transcript.error
    assert len(transcript.steps) == 3


# ────────── action dispatch path ──────────

def test_run_task_dispatches_left_click(tmp_path, stub_screenshot, stub_anthropic):
    """Model emits tool_use → dispatcher calls left_click → next response completes."""
    stub_anthropic.extend([
        _fake_resp(_tool_use({"action": "left_click", "coordinate": [50, 60]})),
        _fake_resp(_text("clicked then done")),
    ])
    with patch.object(D, "left_click",
                      return_value=D.ActionResult(ok=True, message="clicked")) as click:
        transcript = L.run_task(
            goal="click", task_id="t1",
            transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
            max_steps=5, max_usd_per_day=10.0,
        )
    click.assert_called_once_with(50, 60)
    assert transcript.status == "completed"
    assert len(transcript.steps) == 2  # one action + one completion


def test_run_task_handles_desktop_error_gracefully(tmp_path, stub_screenshot, stub_anthropic):
    """A desktop error in step 1 → tool_result with is_error=true; loop
    continues; model gets another chance."""
    stub_anthropic.extend([
        _fake_resp(_tool_use({"action": "left_click", "coordinate": [99999, 99999]})),
        _fake_resp(_text("oh well, giving up")),
    ])
    with patch.object(D, "left_click", side_effect=D.DesktopError("out of bounds")):
        transcript = L.run_task(
            goal="impossible click", task_id="t1",
            transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
            max_steps=5, max_usd_per_day=10.0,
        )
    # Loop did NOT bail on the desktop error — completed via second turn
    assert transcript.status == "completed"
    # The first step's desktop_result should mention the error
    assert "out of bounds" in transcript.steps[0].desktop_result


# ────────── mid-task budget cap ──────────

def test_run_task_stops_when_mid_task_budget_exceeded(tmp_path, stub_screenshot, stub_anthropic):
    """If a single step pushes today's spend over the cap → bail with
    budget_exceeded_usd at that step."""
    # First call: cheap step
    # Second call: huge step that blows budget
    stub_anthropic.extend([
        _fake_resp(_tool_use({"action": "left_click"}), in_tokens=10_000, out_tokens=100),
        _fake_resp(_tool_use({"action": "left_click"}), in_tokens=1_000_000, out_tokens=0),
        _fake_resp(_text("never reached")),  # shouldn't get here
    ])
    with patch.object(D, "left_click", return_value=D.ActionResult(ok=True, message="x")):
        transcript = L.run_task(
            goal="g", task_id="t1",
            transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
            max_steps=10, max_usd_per_day=2.0,
        )
    # 10K input = ~$0.03, then 1M input = $3 — total $3.03 > cap $2
    assert transcript.status == "budget_exceeded_usd"
    # One unconsumed response should remain (we bailed before step 3)
    assert len(stub_anthropic) == 1


# ────────── screen size fallback ──────────

def test_run_task_falls_back_to_default_resolution(tmp_path, monkeypatch, stub_anthropic):
    """If get_screen_size raises, use env DESKTOP_RESOLUTION (default 1280x720)."""
    monkeypatch.setattr(D, "screenshot", lambda: b"\x89PNG")
    monkeypatch.setattr(D, "get_screen_size",
                        MagicMock(side_effect=D.DesktopError("xdotool gone")))
    stub_anthropic.append(_fake_resp(_text("done")))
    transcript = L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
        max_steps=5, max_usd_per_day=10.0,
    )
    assert transcript.status == "completed"  # didn't crash


def test_run_task_falls_back_to_env_resolution(tmp_path, monkeypatch, stub_anthropic):
    monkeypatch.setattr(D, "screenshot", lambda: b"\x89PNG")
    monkeypatch.setattr(D, "get_screen_size",
                        MagicMock(side_effect=D.DesktopError("gone")))
    monkeypatch.setenv("DESKTOP_RESOLUTION", "1920x1080")
    stub_anthropic.append(_fake_resp(_text("done")))
    transcript = L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
        max_steps=5, max_usd_per_day=10.0,
    )
    assert transcript.status == "completed"


def test_run_task_handles_malformed_env_resolution(tmp_path, monkeypatch, stub_anthropic):
    """DESKTOP_RESOLUTION='garbage' should not crash run_task."""
    monkeypatch.setattr(D, "screenshot", lambda: b"\x89PNG")
    monkeypatch.setattr(D, "get_screen_size",
                        MagicMock(side_effect=D.DesktopError("gone")))
    monkeypatch.setenv("DESKTOP_RESOLUTION", "not-a-resolution")
    stub_anthropic.append(_fake_resp(_text("done")))
    transcript = L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
        max_steps=5, max_usd_per_day=10.0,
    )
    assert transcript.status == "completed"


# ────────── progress_cb integration ──────────

def test_run_task_calls_progress_cb_per_step(tmp_path, stub_screenshot, stub_anthropic):
    """SSE streaming depends on progress_cb firing for every step."""
    stub_anthropic.extend([
        _fake_resp(_tool_use({"action": "left_click"})),
        _fake_resp(_text("done")),
    ])
    seen = []
    def cb(transcript, step):
        seen.append(step.step)

    with patch.object(D, "left_click", return_value=D.ActionResult(ok=True, message="x")):
        L.run_task(
            goal="g", task_id="t1",
            transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
            max_steps=5, max_usd_per_day=10.0,
            progress_cb=cb,
        )
    assert seen == [1, 2]


def test_run_task_progress_cb_called_on_completion(tmp_path, stub_screenshot, stub_anthropic):
    """The terminal completion step should also fire progress_cb."""
    stub_anthropic.append(_fake_resp(_text("first response is the final one")))
    cb_calls = []
    L.run_task(
        goal="g", task_id="t1",
        transcript_file=tmp_path / "t.json", ledger_file=tmp_path / "l.jsonl",
        max_steps=5, max_usd_per_day=10.0,
        progress_cb=lambda t, s: cb_calls.append(s),
    )
    assert len(cb_calls) == 1


# ────────── transcript JSON shape ──────────

def test_to_jsonable_returns_expected_keys():
    """The atelier chat depends on the exact transcript schema. Don't drift."""
    t = L.TaskTranscript(task_id="t1", goal="g", started_at=1000.0)
    out = t.to_jsonable()
    for key in ("task_id", "goal", "started_at", "status", "step_count",
                "total_cost_usd", "finished_at", "final_text", "error"):
        assert key in out


def test_to_jsonable_rounds_cost():
    t = L.TaskTranscript(task_id="t1", goal="g", started_at=1.0)
    t.total_cost_usd = 0.0123456789
    assert t.to_jsonable()["total_cost_usd"] == 0.012346


def test_transcript_default_status_is_running():
    t = L.TaskTranscript(task_id="t1", goal="g", started_at=1.0)
    assert t.status == "running"


def test_transcript_default_steps_empty():
    t = L.TaskTranscript(task_id="t1", goal="g", started_at=1.0)
    assert t.steps == []
    assert t.total_cost_usd == 0.0


def test_step_record_dataclass_fields():
    s = L.StepRecord(
        step=1, started_at=1.0, finished_at=2.0,
        action={"a": "click"}, desktop_result="ok",
        text_from_model="thinking", cost_usd=0.01,
    )
    assert s.step == 1
    assert s.cost_usd == 0.01
    assert s.action == {"a": "click"}
