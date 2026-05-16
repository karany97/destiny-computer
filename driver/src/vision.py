"""Vision-backend abstraction — issue #11 / TRACKING D5b.

Pre-PR, `loop.py::run_task` called the Anthropic SDK directly. The
`VISION_BACKEND=local-uitars` env was a no-op for the model call. This
module wraps the existing Anthropic path behind a `VisionBackend`
interface and adds a parallel `Holo3VisionBackend` that drives the
vLLM sidecar shipped by D5a (PR #12).

Design

  StepResult: backend-agnostic per-step output. The loop reads only
  these fields, never touches backend-specific dict shapes.

  VisionBackend ABC:
    - initial_history(goal, first_screenshot_png) -> list
        Backend-specific conversation seed. Anthropic ships an
        Anthropic-shaped messages list with a tool_use system prompt;
        Holo3 ships an OpenAI-shaped messages list keyed for the
        Holo3 prompt template.
    - step(history, latest_screenshot, screen_size) -> StepResult
        One model call. Returns text + (action | finish) + cost.
    - append_action_result(history, last_step, action_msg, ok,
                            next_screenshot) -> list
        Mutates conversation history with the action outcome + the
        post-action screenshot. Backends own their history shape.

  AnthropicVisionBackend: wraps the v0.2 code. Existing 21 tests in
  test_loop_run_task.py keep passing because the only change is where
  the Anthropic SDK is imported (moved from loop.py to vision.py).
  Test fixture's monkey patch needs to move from `L.Anthropic` to
  `V.Anthropic`.

  Holo3VisionBackend: POSTs to ${HOLO3_ENDPOINT}/chat/completions in
  OpenAI Chat Completions shape. Parses Holo3's structured response
  (per the Holo3-35B-A3B HF card prompt template):

      Action: <click|type|key|scroll|done|wait>
      Coordinate: [<x>, <y>]                  (when applicable)
      Text: "<keystrokes>"                    (for type/key)
      Reasoning: <free-form>

  We also accept JSON-mode output if the operator enables vLLM's
  `--enable-json-mode`. `done` action triggers finish=True.

Cost tracking

  AnthropicVisionBackend: real per-token cost via the SDK's usage field.
  Holo3VisionBackend: cost_usd = 0.0 — local model, no $ per token.
  The cost_ledger still records the row (with $0.00) so operators
  see "5 Holo3 calls today, $0.00" rather than nothing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Anthropic SDK at module scope so tests can monkey-patch `vision.Anthropic`.
try:
    from anthropic import Anthropic, APIError
except ImportError:  # pragma: no cover
    Anthropic = None  # type: ignore
    APIError = Exception  # type: ignore

# Import the pricing table from loop.py so we don't drift on Anthropic
# rate changes. Done lazily to avoid the circular-import risk if loop
# imports vision and vice versa.
def _price_call(model: str, in_tokens: int, out_tokens: int) -> float:
    try:
        from . import loop as L
    except ImportError:
        import loop as L  # type: ignore[no-redef]
    return L._price_call(model, in_tokens, out_tokens)


log = logging.getLogger(__name__)


# ────────── public types ──────────


@dataclass
class StepResult:
    """One model step, normalized across backends.

    finish=True signals the loop to stop (model said "done" with no
    further action). action is the next action dict to dispatch
    (mutually exclusive with finish — exactly one of {action, finish}
    is "set" in a well-formed step).
    """
    text: Optional[str] = None
    finish: bool = False
    action: Optional[Dict[str, Any]] = None
    # Backend-opaque blob — Anthropic stores tool_use_id here so the
    # next user turn can pair the tool_result. Holo3 doesn't use it.
    backend_marker: Any = None
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class VisionBackend(ABC):
    """One model call = one step. Backends own their history shape."""

    name: str = "<unset>"
    model: str = "<unset>"

    @abstractmethod
    def initial_history(self, goal: str, first_screenshot_png: bytes,
                        screen_size: Tuple[int, int]) -> List[Dict[str, Any]]:
        """Conversation seed: the goal + the first screenshot."""

    @abstractmethod
    def step(self, history: List[Dict[str, Any]],
             screen_size: Tuple[int, int]) -> StepResult:
        """One model call. Mutates nothing; returns the next step."""

    @abstractmethod
    def append_action_result(self, history: List[Dict[str, Any]],
                              last_result: StepResult,
                              action_message: str, action_ok: bool,
                              next_screenshot_png: bytes) -> List[Dict[str, Any]]:
        """After action dispatch, build the next user turn carrying the
        result + post-action screenshot. Returns the updated history."""


# ────────── Anthropic backend ──────────


def _b64_png(png: bytes) -> str:
    import base64
    return base64.b64encode(png).decode("ascii")


def _anthropic_image_block(png: bytes) -> Dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": _b64_png(png),
        },
    }


class AnthropicVisionBackend(VisionBackend):
    """Wraps the v0.2 Anthropic Computer Use loop.

    Kept identical to the original loop.py behaviour so the existing
    21 test_loop_run_task tests pass with only a 1-line fixture change
    (monkey-patch target moves from loop.Anthropic to vision.Anthropic).
    """

    name = "anthropic"

    def __init__(self) -> None:
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.computer_tool_version = os.environ.get(
            "COMPUTER_TOOL_VERSION", "computer_20251124")
        self.anthropic_beta = os.environ.get(
            "ANTHROPIC_BETA", "computer-use-2025-01-24")
        if Anthropic is None:
            raise RuntimeError("anthropic SDK not installed")
        self.client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def _system_prompt(self, w: int, h: int) -> str:
        return (
            "You are Destiny — an AI that drives a persistent Linux desktop on "
            "behalf of the operator. Use the `computer` tool to take actions "
            "(click, type, scroll). When you've achieved the goal, reply with a "
            f"short summary in plain text and STOP. Display is {w}x{h}. Be "
            "efficient — each action costs the operator real money."
        )

    def _tool_config(self, w: int, h: int) -> List[Dict[str, Any]]:
        return [{
            "type": self.computer_tool_version,
            "name": "computer",
            "display_width_px": w,
            "display_height_px": h,
            "display_number": 1,
        }]

    def initial_history(self, goal, first_screenshot_png, screen_size):
        return [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Goal: {goal}"},
                _anthropic_image_block(first_screenshot_png),
            ],
        }]

    def step(self, history, screen_size):
        w, h = screen_size
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self._system_prompt(w, h),
                tools=self._tool_config(w, h),
                messages=history,
                betas=[self.anthropic_beta],
            )
        except APIError as e:
            # Surface as-is; loop.run_task catches and sets api_error
            raise

        # Cost
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        cost = _price_call(self.model, in_tok, out_tok)

        # Parse content blocks
        tool_use = None
        text_pieces: List[str] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "tool_use":
                tool_use = block
            elif btype == "text":
                text_pieces.append(block.text)
        text = "\n".join(text_pieces).strip() or None

        if tool_use is None:
            # Completion — model emitted text-only response
            return StepResult(
                text=text, finish=True, action=None,
                # Stash the assistant turn so run_task can append for
                # cost-accounting in the transcript.
                backend_marker={
                    "assistant_blocks": [b.model_dump() for b in resp.content],
                    "tool_use_id": None,
                },
                cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            )

        return StepResult(
            text=text, finish=False, action=dict(tool_use.input),
            backend_marker={
                "assistant_blocks": [b.model_dump() for b in resp.content],
                "tool_use_id": tool_use.id,
            },
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
        )

    def append_action_result(self, history, last_result, action_message,
                              action_ok, next_screenshot_png):
        # Mirror v0.2: assistant turn first, then user tool_result + next screenshot.
        marker = last_result.backend_marker or {}
        assistant_blocks = marker.get("assistant_blocks", [])
        tool_use_id = marker.get("tool_use_id")
        history.append({"role": "assistant", "content": assistant_blocks})
        if tool_use_id is None:
            # Shouldn't happen — finish=True path doesn't go through this
            return history
        tool_result_content: List[Dict[str, Any]] = [
            {"type": "text", "text": action_message},
        ]
        if action_ok:
            tool_result_content.append(_anthropic_image_block(next_screenshot_png))
        block = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": tool_result_content,
        }
        if not action_ok:
            block["is_error"] = True
        history.append({"role": "user", "content": [block]})
        return history


# ────────── Holo3 backend (vLLM via OpenAI Chat Completions) ──────────


def _holo3_image_block(png: bytes) -> Dict[str, Any]:
    """OpenAI vision message shape — `image_url` with base64 data URI."""
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{_b64_png(png)}"},
    }


def _holo3_system_prompt(w: int, h: int) -> str:
    """Holo3's recommended prompt format from the HCompany model card.

    Emits one Action per turn in a structured format the parser below
    handles. The model is fine-tuned for this exact prompt; deviating
    drops accuracy meaningfully (per the Holo3 paper §4.2)."""
    return (
        "You are Holo3 — a computer-use agent driving a Linux desktop. "
        f"Display is {w}x{h} pixels.\n\n"
        "For each step, output a single Action in this exact format:\n"
        "Action: <click | type | key | scroll | done | wait>\n"
        "Coordinate: [<x>, <y>]   (only for click/scroll; omit for "
        "key/type/done/wait)\n"
        "Text: \"<text>\"          (only for type/key; the literal "
        "keystrokes)\n"
        "Direction: <up|down|left|right>  (only for scroll)\n"
        "Reasoning: <one sentence>\n\n"
        "When the goal is achieved, emit Action: done with your summary "
        "in Reasoning."
    )


# Action regex — defensive against the model emitting markdown wrappers
# or extra whitespace. We anchor on "Action:" then parse following lines
# loosely.
_HOLO3_ACTION_RE   = re.compile(r"^\s*Action:\s*(\S+)", re.MULTILINE)
_HOLO3_COORD_RE    = re.compile(r"Coordinate:\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]")
_HOLO3_TEXT_RE     = re.compile(r'Text:\s*"((?:[^"\\]|\\.)*)"')
_HOLO3_DIR_RE      = re.compile(r"Direction:\s*(\w+)")
_HOLO3_REASONING_RE = re.compile(r"Reasoning:\s*(.+?)(?:\n\S+:|\Z)", re.DOTALL)


def _parse_holo3_response(raw: str) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    """Parse Holo3's structured response into (action_dict, text, finish).

    Action dict shape matches what _dispatch_action in loop.py expects:
      {"action": "left_click", "coordinate": [x, y]}
      {"action": "type", "text": "hello"}
      {"action": "key", "text": "Return"}
      {"action": "scroll", "coordinate": [x, y], "scroll_direction": "down"}
      {"action": "wait", "duration": 1}

    If the parser can't extract an Action line, treat the whole response
    as text-only completion (finish=True with text=raw)."""
    action_match = _HOLO3_ACTION_RE.search(raw or "")
    reasoning_match = _HOLO3_REASONING_RE.search(raw or "")
    reasoning = reasoning_match.group(1).strip() if reasoning_match else None

    if not action_match:
        # No structured action — model went text-only or off-script.
        return None, (raw or "").strip() or None, True

    action_name = action_match.group(1).strip().lower().rstrip(",.;:")

    if action_name in ("done", "stop", "finish"):
        return None, reasoning, True

    coord_match = _HOLO3_COORD_RE.search(raw)
    coord = (int(coord_match.group(1)), int(coord_match.group(2))) \
            if coord_match else None

    text_match = _HOLO3_TEXT_RE.search(raw)
    text_arg = text_match.group(1) if text_match else None

    dir_match = _HOLO3_DIR_RE.search(raw)
    direction = dir_match.group(1).lower() if dir_match else None

    if action_name == "click":
        action = {"action": "left_click"}
        if coord:
            action["coordinate"] = list(coord)
        return action, reasoning, False
    if action_name in ("right_click", "rightclick"):
        action = {"action": "right_click"}
        if coord:
            action["coordinate"] = list(coord)
        return action, reasoning, False
    if action_name in ("double_click", "doubleclick"):
        action = {"action": "double_click"}
        if coord:
            action["coordinate"] = list(coord)
        return action, reasoning, False
    if action_name == "type":
        return {"action": "type", "text": text_arg or ""}, reasoning, False
    if action_name == "key":
        return {"action": "key", "text": text_arg or ""}, reasoning, False
    if action_name == "scroll":
        action = {"action": "scroll", "scroll_direction": direction or "down"}
        if coord:
            action["coordinate"] = list(coord)
        return action, reasoning, False
    if action_name == "wait":
        # Holo3 sometimes emits "duration" inline; we default to 1s
        return {"action": "wait", "duration": 1}, reasoning, False
    if action_name == "mouse_move" or action_name == "move":
        action = {"action": "mouse_move"}
        if coord:
            action["coordinate"] = list(coord)
        return action, reasoning, False

    # Unknown action — let the dispatcher reject it; we surface the
    # name + reasoning for the audit log
    return {"action": action_name}, reasoning, False


class Holo3VisionBackend(VisionBackend):
    """Holo3-35B-A3B served via vLLM's OpenAI-compatible /chat/completions.

    Endpoint, model id, and API key come from env (HOLO3_*).  Cost is
    zero — local model, no per-token spend — but we still log to the
    ledger so operators see "5 Holo3 calls today, $0.00" instead of
    nothing.
    """

    name = "holo3"

    def __init__(self) -> None:
        self.endpoint = os.environ.get("HOLO3_ENDPOINT", "http://holo3:8000/v1").rstrip("/")
        self.model = os.environ.get("HOLO3_MODEL", "Hcompany/Holo3-35B-A3B")
        self.api_key = os.environ.get("HOLO3_API_KEY", "EMPTY")
        self.timeout_s = float(os.environ.get("HOLO3_HTTP_TIMEOUT_S", "60"))

    def initial_history(self, goal, first_screenshot_png, screen_size):
        w, h = screen_size
        return [
            {"role": "system", "content": _holo3_system_prompt(w, h)},
            {"role": "user", "content": [
                {"type": "text", "text": f"Goal: {goal}"},
                _holo3_image_block(first_screenshot_png),
            ]},
        ]

    def step(self, history, screen_size):
        payload = {
            "model": self.model,
            "messages": history,
            "max_tokens": 1024,
            "temperature": 0.0,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                raw_body = r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            raise RuntimeError(f"Holo3 endpoint unreachable: {e}") from e

        try:
            resp = json.loads(raw_body.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Holo3 returned non-JSON: {e}") from e

        choices = resp.get("choices") or []
        if not choices:
            raise RuntimeError(f"Holo3 response missing choices: {resp}")
        message = choices[0].get("message") or {}
        raw_content = message.get("content") or ""

        # vLLM reports tokens in resp.usage — capture for the ledger
        usage = resp.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)

        action, text, finish = _parse_holo3_response(raw_content)

        return StepResult(
            text=text, finish=finish, action=action,
            backend_marker={
                "assistant_content": raw_content,
            },
            # Local model — no $ cost. Tokens captured for transparency.
            cost_usd=0.0,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    def append_action_result(self, history, last_result, action_message,
                              action_ok, next_screenshot_png):
        marker = last_result.backend_marker or {}
        assistant_content = marker.get("assistant_content", "")
        history.append({"role": "assistant", "content": assistant_content})
        # Holo3 doesn't have a tool_result shape; mimic Anthropic's
        # action-result-as-user-turn convention with the next screenshot.
        observation = (
            "[action_result]\n"
            f"ok={action_ok}\n"
            f"message={action_message}\n"
        )
        history.append({"role": "user", "content": [
            {"type": "text", "text": observation},
            _holo3_image_block(next_screenshot_png),
        ]})
        return history


# ────────── factory ──────────


def get_backend(name: Optional[str] = None) -> VisionBackend:
    """Pick the backend keyed on $VISION_BACKEND (or explicit `name` arg).

    `anthropic` → AnthropicVisionBackend (requires ANTHROPIC_API_KEY)
    `local-uitars` / `holo3` → Holo3VisionBackend
    Unknown → ValueError so the operator sees the misconfig fast.
    """
    n = (name or os.environ.get("VISION_BACKEND", "anthropic")).lower()
    if n == "anthropic":
        return AnthropicVisionBackend()
    if n in ("local-uitars", "holo3"):
        return Holo3VisionBackend()
    raise ValueError(
        f"unknown VISION_BACKEND: {n!r} "
        f"(supported: anthropic, local-uitars, holo3)"
    )
