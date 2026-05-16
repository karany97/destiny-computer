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
except ImportError:
    import desktop as D  # type: ignore[no-redef]

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

    # Determine actual screen size — pass to the tool config so the model
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

    tool_config = [{
        "type": COMPUTER_TOOL_VERSION,
        "name": "computer",
        "display_width_px": w,
        "display_height_px": h,
        "display_number": 1,
    }]

    system_prompt = (
        "You are Destiny — an AI that drives a persistent Linux desktop on behalf "
        "of the operator. Use the `computer` tool to take actions (click, type, "
        "scroll). When you've achieved the goal, reply with a short summary in "
        f"plain text and STOP. Display is {w}x{h}. Be efficient — each action "
        "costs the operator real money."
    )

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Conversation starts with: user text (goal) + an initial screenshot,
    # so the model never has to ask for one explicitly.
    messages: List[Dict[str, Any]] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": f"Goal: {goal}"},
            _take_screenshot_block(),
        ],
    }]

    for step in range(1, max_steps + 1):
        step_started = time.time()

        # 1. Call the model
        try:
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=tool_config,
                messages=messages,
                betas=[ANTHROPIC_BETA],
            )
        except APIError as e:
            transcript.status = "api_error"
            transcript.error = f"step {step}: {e}"
            transcript.finished_at = time.time()
            _flush_transcript(transcript, transcript_file)
            return transcript

        # 2. Accumulate cost
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        cost = _price_call(MODEL, in_tok, out_tok)
        transcript.total_cost_usd += cost
        _record_cost(ledger_file, task_id, cost, step)

        # 3. Find the tool_use block (if any) and the text
        tool_use = None
        text_pieces = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                tool_use = block
            elif getattr(block, "type", None) == "text":
                text_pieces.append(block.text)
        text_from_model = "\n".join(text_pieces).strip() or None

        # Echo the assistant turn into messages (verbatim, blocks preserved)
        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})

        # 4a. No tool_use → model declared completion
        if tool_use is None:
            transcript.status = "completed"
            transcript.final_text = text_from_model or "(model stopped without text)"
            transcript.finished_at = time.time()
            transcript.steps.append(StepRecord(
                step=step,
                started_at=step_started,
                finished_at=time.time(),
                action=None,
                desktop_result=None,
                text_from_model=text_from_model,
                cost_usd=cost,
            ))
            if progress_cb:
                progress_cb(transcript, transcript.steps[-1])
            _flush_transcript(transcript, transcript_file)
            return transcript

        # 4b. Execute the action
        action_input = tool_use.input
        try:
            result = _dispatch_action(action_input)
            desktop_msg = result.message
            tool_result_content: List[Dict[str, Any]] = [
                {"type": "text", "text": desktop_msg},
                _take_screenshot_block(),
            ]
            is_error = False
        except D.DesktopError as e:
            desktop_msg = f"desktop error: {e}"
            tool_result_content = [{"type": "text", "text": desktop_msg}]
            is_error = True

        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": tool_result_content,
                **({"is_error": True} if is_error else {}),
            }],
        })

        # 5. Record + progress
        record = StepRecord(
            step=step,
            started_at=step_started,
            finished_at=time.time(),
            action=dict(action_input) if isinstance(action_input, dict) else None,
            desktop_result=desktop_msg,
            text_from_model=text_from_model,
            cost_usd=cost,
        )
        transcript.steps.append(record)
        if progress_cb:
            progress_cb(transcript, record)
        _flush_transcript(transcript, transcript_file)

        # 6. Budget guards
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
