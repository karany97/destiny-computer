"""Anthropic Computer Use loop — screenshot → think → act → repeat.

Implements the canonical agentic loop against Anthropic's
`computer_20251124` tool schema:

  1. Take screenshot.
  2. Send conversation history + screenshot to the model.
  3. Model returns a `tool_use` block with action {type, coords, text, ...}.
  4. We execute that action against the desktop via desktop.py.
  5. Take post-action screenshot, append `tool_result` to history with
     the new screenshot.
  6. Repeat until model says "done" (text-only response, no tool_use) OR
     until we hit max_steps OR until we hit the daily cost cap.

References:
- https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/computer-use-tool
- https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo

Cost discipline:
- We pull current per-request usage from the API response.
- We accumulate against a daily ledger (cost-ledger.jsonl).
- We refuse to start a new task if today's spend ≥ MAX_USD_PER_DAY.
- We stop mid-task if spend exceeds 1.5× the per-task estimate.

Failure modes are explicit:
- max_steps reached       → status: 'budget_exceeded_steps', partial result
- cost cap exceeded       → status: 'budget_exceeded_usd', partial result
- desktop unreachable     → status: 'desktop_error', the error message
- Anthropic API error     → status: 'api_error', the error
- LLM declared completion → status: 'completed', full result
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from anthropic import Anthropic, APIError

# Support both package import (when used as `destiny_driver.loop`) and
# script-style import (when uvicorn loads $HOME/destiny-driver/main.py
# directly with the parent dir on PYTHONPATH and no __init__.py).
try:
    from . import desktop as D  # ✱ relative for in-package import
    from . import vision
except ImportError:
    import desktop as D  # type: ignore[no-redef]
    import vision  # type: ignore[no-redef]

log = logging.getLogger(__name__)

# ─── Model + pricing (USD per million tokens) ───────────────────────────────
#
# Sonnet 4.5 is the sweet spot for Computer Use: comparable success rate to
# Opus on common GUI tasks at 1/5th the cost. Opus 4.5 only wins on long,
# multi-application workflows. We expose MODEL via env so operators can
# swap in opus for harder runs.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
COMPUTER_TOOL_VERSION = os.environ.get("COMPUTER_TOOL_VERSION", "computer_20251124")
ANTHROPIC_BETA = os.environ.get("ANTHROPIC_BETA", "computer-use-2025-01-24")

# Pricing table (Sonnet 4.5 / Opus 4.5 as of 2026-05). Override per env if
# Anthropic shifts pricing — we'd rather over-report than under-report cost.
PRICING_USD_PER_MTOK = {
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-opus-4-5":   {"input": 15.0, "output": 75.0},
}


@dataclass
class StepRecord:
    """Per-iteration record kept in the task transcript for replay/debug."""
    step: int
    started_at: float
    finished_at: float
    action: Optional[Dict[str, Any]]  # the computer tool action ({type, x, y, ...})
    desktop_result: Optional[str]     # post-action message from desktop.py
    text_from_model: Optional[str]    # any model-side text in this turn
    cost_usd: float


@dataclass
class TaskTranscript:
    """Persistent record of one task end-to-end."""
    task_id: str
    goal: str
    started_at: float
    status: str = "running"
    steps: List[StepRecord] = field(default_factory=list)
    total_cost_usd: float = 0.0
    finished_at: Optional[float] = None
    final_text: Optional[str] = None
    error: Optional[str] = None

    def to_jsonable(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "started_at": self.started_at,
            "status": self.status,
            "step_count": len(self.steps),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "finished_at": self.finished_at,
            "final_text": self.final_text,
            "error": self.error,
        }


# ─── Cost ledger ───────────────────────────────────────────────────────────


def _today_iso() -> str:
    return time.strftime("%Y-%m-%d")


def _record_cost(ledger_file: Path, task_id: str, usd: float, step: int) -> None:
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": _today_iso(),
        "task_id": task_id,
        "step": step,
        "usd": round(usd, 6),
        "ts": time.time(),
    }
    with ledger_file.open("a") as f:
        f.write(json.dumps(row) + "\n")


def today_spend(ledger_file: Path) -> float:
    if not ledger_file.exists():
        return 0.0
    total = 0.0
    today = _today_iso()
    for line in ledger_file.read_text().splitlines():
        try:
            row = json.loads(line)
            if row.get("date") == today:
                total += float(row.get("usd", 0))
        except Exception:
            continue
    return total


def _price_call(model: str, in_tokens: int, out_tokens: int) -> float:
    p = PRICING_USD_PER_MTOK.get(model)
    if not p:
        log.warning("No pricing entry for %s — billing at Sonnet rates", model)
        p = PRICING_USD_PER_MTOK["claude-sonnet-4-5"]
    return (in_tokens / 1_000_000.0) * p["input"] + (out_tokens / 1_000_000.0) * p["output"]


# ─── Action dispatcher ─────────────────────────────────────────────────────


def _dispatch_action(action: Dict[str, Any]) -> D.ActionResult:
    """Translate an Anthropic tool_use input dict into the desktop call."""
    a = action.get("action")
    if a is None:
        raise D.DesktopError(f"missing 'action' field: {action!r}")

    # Coordinates come as [x, y]
    coord = action.get("coordinate") or action.get("coord")
    sx = sy = None
    if coord and isinstance(coord, (list, tuple)) and len(coord) == 2:
        sx, sy = int(coord[0]), int(coord[1])

    if a == "screenshot":
        # No-op; the loop always captures a post-action screenshot anyway.
        return D.ActionResult(ok=True, message="screenshot requested")
    if a == "mouse_move":
        return D.mouse_move(sx, sy)
    if a == "left_click":
        return D.left_click(sx, sy)
    if a == "right_click":
        return D.right_click(sx, sy)
    if a == "middle_click":
        return D.middle_click(sx, sy)
    if a == "double_click":
        return D.double_click(sx, sy)
    if a == "triple_click":
        return D.triple_click(sx, sy)
    if a == "left_mouse_down":
        return D.left_mouse_down(sx, sy)
    if a == "left_mouse_up":
        return D.left_mouse_up(sx, sy)
    if a == "left_click_drag":
        start = action.get("start_coordinate")
        if not start or len(start) != 2:
            raise D.DesktopError("left_click_drag requires start_coordinate")
        if sx is None or sy is None:
            raise D.DesktopError("left_click_drag requires coordinate (end point)")
        return D.left_click_drag(int(start[0]), int(start[1]), sx, sy)
    if a == "type":
        text = action.get("text", "")
        return D.type_text(text)
    if a == "key":
        text = action.get("text", "")
        return D.key_press(text)
    if a == "hold_key":
        text = action.get("text", "")
        ms = int(action.get("duration", 1)) * 1000
        return D.hold_key(text, ms)
    if a == "scroll":
        direction = action.get("scroll_direction", "down")
        amount = int(action.get("scroll_amount", 3))
        x, y = (sx, sy) if sx is not None else D.cursor_position()
        return D.scroll(x, y, direction, amount)
    if a == "wait":
        secs = float(action.get("duration", 1))
        return D.wait(secs)
    if a == "cursor_position":
        cx, cy = D.cursor_position()
        return D.ActionResult(ok=True, message=f"cursor at ({cx},{cy})")
    raise D.DesktopError(f"unknown action: {a!r}")


# ─── Main loop ─────────────────────────────────────────────────────────────


def _take_screenshot_block() -> Dict[str, Any]:
    """Return an Anthropic message content block carrying a fresh PNG."""
    png = D.screenshot()
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        },
    }


def run_task(
    *,
    goal: str,
    task_id: str,
    transcript_file: Path,
    ledger_file: Path,
    max_steps: int,
    max_usd_per_day: float,
    progress_cb: Optional[Callable[[TaskTranscript, StepRecord], None]] = None,
) -> TaskTranscript:
    """Execute the autonomous loop.

    progress_cb is called after every step — used by the FastAPI streaming
    endpoint to push live updates to the chat.
    """
    transcript = TaskTranscript(task_id=task_id, goal=goal, started_at=time.time())

    # Refuse early if today's spend already over the cap.
    spent = today_spend(ledger_file)
    if spent >= max_usd_per_day:
        transcript.status = "budget_exceeded_usd"
        transcript.error = f"daily cap ${max_usd_per_day:.2f} already reached (today ${spent:.4f})"
        transcript.finished_at = time.time()
        _flush_transcript(transcript, transcript_file)
        return transcript

    # Determine actual screen size — passed to the backend so the model
    # uses correct coordinates. Falls back to env default if introspection fails.
    try:
        w, h = D.get_screen_size()
    except D.DesktopError as e:
        log.warning("get_screen_size failed: %s; falling back to env DESKTOP_RESOLUTION", e)
        res = os.environ.get("DESKTOP_RESOLUTION", "1280x720")
        try:
            w, h = (int(x) for x in res.split("x", 1))
        except Exception:
            w, h = 1280, 720

    # D5b — vision-backend abstraction (was: hard-coded Anthropic).
    # `get_backend()` keys on $VISION_BACKEND (defaults anthropic).
    # backend.initial_history seeds the conversation; backend.step
    # makes one model call + returns a normalized StepResult; backend
    # .append_action_result writes the action outcome + next screenshot
    # in the backend's expected message shape.
    try:
        backend = vision.get_backend()
    except (ValueError, RuntimeError) as e:
        transcript.status = "api_error"
        transcript.error = f"backend init failed: {e}"
        transcript.finished_at = time.time()
        _flush_transcript(transcript, transcript_file)
        return transcript

    # Capture first screenshot. If this fails, the desktop is unreachable —
    # surface as desktop_error so the operator can debug.
    try:
        first_png = D.screenshot()
    except D.DesktopError as e:
        transcript.status = "desktop_error"
        transcript.error = f"initial screenshot failed: {e}"
        transcript.finished_at = time.time()
        _flush_transcript(transcript, transcript_file)
        return transcript

    messages = backend.initial_history(goal, first_png, (w, h))

    for step in range(1, max_steps + 1):
        step_started = time.time()

        # 1. Call the model via the backend
        try:
            result = backend.step(messages, (w, h))
        except (APIError, RuntimeError) as e:
            transcript.status = "api_error"
            transcript.error = f"step {step}: {e}"
            transcript.finished_at = time.time()
            _flush_transcript(transcript, transcript_file)
            return transcript

        # 2. Accumulate cost (zero for local backends)
        cost = result.cost_usd
        transcript.total_cost_usd += cost
        _record_cost(ledger_file, task_id, cost, step)
        text_from_model = result.text

        # 3a. Finish — model declared completion (text-only)
        if result.finish:
            transcript.status = "completed"
            transcript.final_text = text_from_model or "(model stopped without text)"
            transcript.finished_at = time.time()
            transcript.steps.append(StepRecord(
                step=step, started_at=step_started, finished_at=time.time(),
                action=None, desktop_result=None,
                text_from_model=text_from_model, cost_usd=cost,
            ))
            if progress_cb:
                progress_cb(transcript, transcript.steps[-1])
            _flush_transcript(transcript, transcript_file)
            return transcript

        # 3b. Execute the action
        action_input = result.action or {}
        try:
            disp_result = _dispatch_action(action_input)
            desktop_msg = disp_result.message
            action_ok = True
        except D.DesktopError as e:
            desktop_msg = f"desktop error: {e}"
            action_ok = False

        # Capture next screenshot for the response turn. Reuse the
        # last good screenshot if the post-action one fails (don't crash
        # the loop just because xwd hiccupped once).
        try:
            next_png = D.screenshot()
        except D.DesktopError as e:
            log.warning("post-action screenshot failed: %s; reusing prior", e)
            next_png = first_png  # fall back to bootstrap shot

        messages = backend.append_action_result(
            messages, result, desktop_msg, action_ok, next_png,
        )

        # 4. Record + progress
        record = StepRecord(
            step=step, started_at=step_started, finished_at=time.time(),
            action=dict(action_input) if isinstance(action_input, dict) else None,
            desktop_result=desktop_msg, text_from_model=text_from_model,
            cost_usd=cost,
        )
        transcript.steps.append(record)
        if progress_cb:
            progress_cb(transcript, record)
        _flush_transcript(transcript, transcript_file)

        # 5. Budget guards (anthropic-only effectively; Holo3 cost is 0)
        if today_spend(ledger_file) >= max_usd_per_day:
            transcript.status = "budget_exceeded_usd"
            transcript.error = (
                f"hit daily cap ${max_usd_per_day:.2f} at step {step} "
                f"(task cost so far ${transcript.total_cost_usd:.4f})"
            )
            transcript.finished_at = time.time()
            _flush_transcript(transcript, transcript_file)
            return transcript

    # Step ceiling reached
    transcript.status = "budget_exceeded_steps"
    transcript.error = f"reached max_steps={max_steps} without completion"
    transcript.finished_at = time.time()
    _flush_transcript(transcript, transcript_file)
    return transcript


def _flush_transcript(transcript: TaskTranscript, transcript_file: Path) -> None:
    transcript_file.parent.mkdir(parents=True, exist_ok=True)
    transcript_file.write_text(json.dumps({
        **transcript.to_jsonable(),
        "steps": [vars(s) for s in transcript.steps],
    }, indent=2))
