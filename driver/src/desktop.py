"""Desktop action executor — runs xdotool/xwd inside the KasmVNC container.

Every primitive maps to a single `docker exec destiny-desktop bash -c "…"`
call, which keeps the host driver stateless and makes the actions easy to
audit (each call appears verbatim in docker logs).

The Anthropic Computer Use schema (computer_20251124) speaks in pixel
coordinates and named keys. This module is the translator: it takes those
arguments, builds an xdotool command, runs it inside the desktop
container, and returns the result (or a screenshot if requested).

Why xdotool not pyautogui? Because we want to drive a remote desktop via
docker exec, not via X11 forwarding on the host. xdotool runs inside the
container against the local Xvnc display (`:1`), so there's no display
forwarding required and no host X11 dependency.

All actions take a `display` arg (default `:1` — the KasmVNC display)
and a `timeout` arg (default 10s). Failure modes:
  - container unreachable      → DesktopError("container unreachable")
  - xdotool/xwd command failed → DesktopError(f"action failed: {stderr}")
  - subprocess timed out       → DesktopError("action timed out")
The caller (loop.py) catches DesktopError + reports it back to the model
as a tool_result with is_error=true so the model can try again.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Optional, Tuple

log = logging.getLogger(__name__)

DESKTOP_CONTAINER = os.environ.get("DESKTOP_CONTAINER", "destiny-desktop")
DESKTOP_DISPLAY = os.environ.get("DESKTOP_DISPLAY", ":1")
DEFAULT_TIMEOUT_S = 10


class DesktopError(RuntimeError):
    """Raised when a desktop action fails — caught by loop.py."""


@dataclass
class ActionResult:
    """Returned from every action call. screenshot is the post-action PNG bytes."""
    ok: bool
    message: str
    screenshot: Optional[bytes] = None


def _exec(cmd: str, *, timeout: int = DEFAULT_TIMEOUT_S,
          capture_stdout: bool = False) -> Tuple[int, bytes, bytes]:
    """Run `bash -c <cmd>` inside the desktop container with DISPLAY set.

    Returns (returncode, stdout_bytes, stderr_bytes). Raises DesktopError
    on subprocess timeout or docker exec failures.
    """
    full = [
        "docker", "exec",
        "-e", f"DISPLAY={DESKTOP_DISPLAY}",
        DESKTOP_CONTAINER,
        "bash", "-c", cmd,
    ]
    try:
        r = subprocess.run(full, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise DesktopError(f"action timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise DesktopError("docker CLI not found on host") from e
    return r.returncode, r.stdout, r.stderr


def screenshot() -> bytes:
    """Capture a PNG of the entire desktop via xwd|convert.

    xwd dumps the root window in X Window Dump format, ImageMagick `convert`
    transcodes that to PNG on stdout. Both are pre-installed in the standard
    KasmVNC image.
    """
    code, out, err = _exec(
        "xwd -root -silent | convert xwd:- png:-",
        capture_stdout=True,
    )
    if code != 0:
        raise DesktopError(f"screenshot failed: {err.decode('utf-8', 'replace')[:200]}")
    if not out:
        raise DesktopError("screenshot returned empty bytes")
    return out


def get_screen_size() -> Tuple[int, int]:
    """Return (width_px, height_px) of the desktop's primary display."""
    code, out, err = _exec(
        "xdotool getdisplaygeometry",
    )
    if code != 0:
        raise DesktopError(f"getdisplaygeometry failed: {err.decode('utf-8', 'replace')[:200]}")
    parts = out.decode("utf-8").strip().split()
    if len(parts) != 2:
        raise DesktopError(f"unexpected geometry output: {out!r}")
    return int(parts[0]), int(parts[1])


# ─── Primitive actions ─────────────────────────────────────────────────────


def mouse_move(x: int, y: int) -> ActionResult:
    code, _, err = _exec(f"xdotool mousemove --sync {int(x)} {int(y)}")
    if code != 0:
        raise DesktopError(f"mouse_move failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message=f"moved to ({x}, {y})")


def left_click(x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
    if x is not None and y is not None:
        cmd = f"xdotool mousemove --sync {int(x)} {int(y)} click 1"
    else:
        cmd = "xdotool click 1"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"left_click failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message="left-clicked")


def right_click(x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
    if x is not None and y is not None:
        cmd = f"xdotool mousemove --sync {int(x)} {int(y)} click 3"
    else:
        cmd = "xdotool click 3"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"right_click failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message="right-clicked")


def middle_click(x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
    if x is not None and y is not None:
        cmd = f"xdotool mousemove --sync {int(x)} {int(y)} click 2"
    else:
        cmd = "xdotool click 2"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"middle_click failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message="middle-clicked")


def double_click(x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
    if x is not None and y is not None:
        cmd = f"xdotool mousemove --sync {int(x)} {int(y)} click --repeat 2 1"
    else:
        cmd = "xdotool click --repeat 2 1"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"double_click failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message="double-clicked")


def triple_click(x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
    if x is not None and y is not None:
        cmd = f"xdotool mousemove --sync {int(x)} {int(y)} click --repeat 3 1"
    else:
        cmd = "xdotool click --repeat 3 1"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"triple_click failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message="triple-clicked")


def left_mouse_down(x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
    if x is not None and y is not None:
        cmd = f"xdotool mousemove --sync {int(x)} {int(y)} mousedown 1"
    else:
        cmd = "xdotool mousedown 1"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"left_mouse_down failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message="mouse down")


def left_mouse_up(x: Optional[int] = None, y: Optional[int] = None) -> ActionResult:
    if x is not None and y is not None:
        cmd = f"xdotool mousemove --sync {int(x)} {int(y)} mouseup 1"
    else:
        cmd = "xdotool mouseup 1"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"left_mouse_up failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message="mouse up")


def left_click_drag(start_x: int, start_y: int, x: int, y: int) -> ActionResult:
    """Drag from (start_x, start_y) to (x, y) with left button held."""
    cmd = (
        f"xdotool mousemove --sync {int(start_x)} {int(start_y)} "
        f"mousedown 1 mousemove --sync {int(x)} {int(y)} mouseup 1"
    )
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"left_click_drag failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message=f"dragged ({start_x},{start_y}) -> ({x},{y})")


def type_text(text: str, *, delay_ms: int = 30) -> ActionResult:
    """Type a string. xdotool handles unicode + special chars natively.

    delay_ms is the inter-keystroke pause — humans average ~80ms so 30 is
    "fast typist". Pushing under 20 occasionally drops chars on KasmVNC.
    """
    # xdotool takes --delay in ms. Quote with shlex to keep newlines + special chars safe.
    cmd = f"xdotool type --delay {int(delay_ms)} -- {shlex.quote(text)}"
    # Long strings take longer; bump the timeout proportionally (1s + 50ms per char).
    timeout = max(DEFAULT_TIMEOUT_S, int(1 + len(text) * 0.05))
    code, _, err = _exec(cmd, timeout=timeout)
    if code != 0:
        raise DesktopError(f"type_text failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message=f"typed {len(text)} chars")


def key_press(key: str) -> ActionResult:
    """Press a key or chord. Accepts xdotool syntax: 'Return', 'ctrl+c', etc."""
    # Map a few common aliases to xdotool's canonical names so the LLM
    # doesn't have to know the keysym dictionary.
    aliases = {
        "enter": "Return",
        "return": "Return",
        "esc": "Escape",
        "escape": "Escape",
        "tab": "Tab",
        "backspace": "BackSpace",
        "delete": "Delete",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "home": "Home", "end": "End",
        "pageup": "Page_Up", "pagedown": "Page_Down",
        "space": "space",
    }
    # Translate component-wise for chords like "ctrl+enter"
    parts = []
    for p in key.split("+"):
        p_stripped = p.strip()
        parts.append(aliases.get(p_stripped.lower(), p_stripped))
    canon = "+".join(parts)
    cmd = f"xdotool key -- {shlex.quote(canon)}"
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"key_press failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message=f"pressed {canon}")


def hold_key(key: str, duration_ms: int) -> ActionResult:
    """Hold a key for duration_ms. Useful for sustained scroll or game inputs."""
    # xdotool keydown ... sleep ... keyup
    cmd = (
        f"xdotool keydown -- {shlex.quote(key)} && "
        f"sleep {duration_ms / 1000.0} && "
        f"xdotool keyup -- {shlex.quote(key)}"
    )
    timeout = max(DEFAULT_TIMEOUT_S, int(2 + duration_ms / 1000.0))
    code, _, err = _exec(cmd, timeout=timeout)
    if code != 0:
        raise DesktopError(f"hold_key failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message=f"held {key} for {duration_ms}ms")


def scroll(x: int, y: int, direction: str, amount: int = 3) -> ActionResult:
    """Scroll at (x, y). Direction = up|down|left|right. amount = "clicks"."""
    button_map = {"up": 4, "down": 5, "left": 6, "right": 7}
    button = button_map.get(direction.lower())
    if button is None:
        raise DesktopError(f"unknown scroll direction: {direction}")
    cmd = (
        f"xdotool mousemove --sync {int(x)} {int(y)} "
        f"click --repeat {int(amount)} {button}"
    )
    code, _, err = _exec(cmd)
    if code != 0:
        raise DesktopError(f"scroll failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message=f"scrolled {direction} {amount}x at ({x},{y})")


def wait(seconds: float) -> ActionResult:
    """Sleep inside the container — models sometimes want a pause for a UI to settle."""
    # Cap at 10s to avoid the model gaming the budget by sleeping forever.
    s = max(0.1, min(10.0, float(seconds)))
    code, _, err = _exec(f"sleep {s}", timeout=int(s) + 2)
    if code != 0:
        raise DesktopError(f"wait failed: {err.decode('utf-8', 'replace')[:200]}")
    return ActionResult(ok=True, message=f"waited {s}s")


def cursor_position() -> Tuple[int, int]:
    """Read current mouse position. Useful for the model to confirm a move worked."""
    code, out, err = _exec("xdotool getmouselocation --shell")
    if code != 0:
        raise DesktopError(f"cursor_position failed: {err.decode('utf-8', 'replace')[:200]}")
    txt = out.decode("utf-8")
    x_line = [l for l in txt.splitlines() if l.startswith("X=")][0]
    y_line = [l for l in txt.splitlines() if l.startswith("Y=")][0]
    return int(x_line.split("=", 1)[1]), int(y_line.split("=", 1)[1])
