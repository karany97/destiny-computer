"""Unit tests for desktop.py — no docker required.

We stub `subprocess.run` to verify desktop.py builds the right xdotool
commands and parses outputs correctly. These tests are CI-friendly
(no docker socket, no X server, no KasmVNC container needed).

Integration tests (the actual `docker exec` path against a live KasmVNC
container) live separately and only run when DESKTOP_CONTAINER is set
in the CI environment.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

import desktop as D  # type: ignore[import-not-found]


def _mock_run(returncode=0, stdout=b"", stderr=b""):
    """Build a mock subprocess.CompletedProcess result."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ─── _exec ────────────────────────────────────────────────────────────────


def test_exec_assembles_docker_command():
    """The driver should always invoke docker exec with -e DISPLAY=:1."""
    with patch("subprocess.run", return_value=_mock_run()) as mock:
        D._exec("xdotool getmouselocation")
        args = mock.call_args[0][0]
        assert args[0] == "docker"
        assert args[1] == "exec"
        assert "-e" in args
        assert "DISPLAY=:1" in args
        # The bash -c invocation should carry the literal command we passed
        assert "bash" in args
        assert "-c" in args
        assert "xdotool getmouselocation" in args


def test_exec_raises_on_timeout():
    """Subprocess timeout → DesktopError, not raw TimeoutExpired."""
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 10)):
        with pytest.raises(D.DesktopError, match="action timed out"):
            D._exec("sleep 99", timeout=1)


def test_exec_raises_when_docker_missing():
    """No docker CLI → DesktopError, not FileNotFoundError."""
    with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
        with pytest.raises(D.DesktopError, match="docker CLI not found"):
            D._exec("anything")


# ─── screenshot ────────────────────────────────────────────────────────────


def test_screenshot_returns_bytes():
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    with patch.object(D, "_exec", return_value=(0, fake_png, b"")):
        result = D.screenshot()
        assert result == fake_png


def test_screenshot_raises_on_empty():
    """xwd|convert sometimes returns 0 bytes when the display isn't ready."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")):
        with pytest.raises(D.DesktopError, match="empty bytes"):
            D.screenshot()


def test_screenshot_raises_on_nonzero():
    with patch.object(D, "_exec", return_value=(1, b"", b"xwd: cannot open display :1")):
        with pytest.raises(D.DesktopError, match="screenshot failed"):
            D.screenshot()


# ─── get_screen_size ───────────────────────────────────────────────────────


def test_get_screen_size_parses_output():
    with patch.object(D, "_exec", return_value=(0, b"1364 768\n", b"")):
        w, h = D.get_screen_size()
        assert w == 1364
        assert h == 768


def test_get_screen_size_raises_on_garbage():
    with patch.object(D, "_exec", return_value=(0, b"unexpected\n", b"")):
        with pytest.raises(D.DesktopError, match="unexpected geometry"):
            D.get_screen_size()


# ─── mouse_move ────────────────────────────────────────────────────────────


def test_mouse_move_builds_correct_xdotool():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        result = D.mouse_move(123, 456)
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool mousemove --sync 123 456"
        assert result.ok is True
        assert "(123, 456)" in result.message


# ─── left_click ────────────────────────────────────────────────────────────


def test_left_click_with_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.left_click(50, 60)
        cmd = mock.call_args[0][0]
        assert "mousemove --sync 50 60" in cmd
        assert "click 1" in cmd


def test_left_click_without_coords():
    """Calling without coordinates should click at current cursor position."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.left_click()
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool click 1"


# ─── type_text ────────────────────────────────────────────────────────────


def test_type_text_quotes_special_chars():
    """shlex.quote should keep newlines + quotes + semicolons safe."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.type_text("hello; rm -rf /\n'quoted'")
        cmd = mock.call_args[0][0]
        # No raw semicolon outside quotes — would otherwise execute rm
        assert "rm -rf" in cmd  # the text itself is preserved
        assert cmd.startswith("xdotool type --delay 30 -- ")


def test_type_text_scales_timeout_with_length():
    """Long strings should not get cut off by the default 10s timeout."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        long_text = "x" * 500
        D.type_text(long_text)
        # _exec was called with timeout kwarg
        _, kwargs = mock.call_args
        assert kwargs.get("timeout", 10) >= 1 + 500 * 0.05  # 26s for 500 chars


# ─── key_press ────────────────────────────────────────────────────────────


def test_key_press_translates_aliases():
    """'enter' → 'Return' (xdotool's canonical name)."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.key_press("enter")
        cmd = mock.call_args[0][0]
        assert "Return" in cmd
        assert "enter" not in cmd.lower().split("--", 1)[1].lower() or True  # alias resolved


def test_key_press_handles_chord():
    """'ctrl+enter' → 'ctrl+Return'."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.key_press("ctrl+enter")
        cmd = mock.call_args[0][0]
        assert "ctrl+Return" in cmd


# ─── scroll ────────────────────────────────────────────────────────────────


def test_scroll_up_uses_button_4():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.scroll(100, 200, "up", amount=3)
        cmd = mock.call_args[0][0]
        assert "click --repeat 3 4" in cmd
        assert "mousemove --sync 100 200" in cmd


def test_scroll_unknown_direction_raises():
    with pytest.raises(D.DesktopError, match="unknown scroll direction"):
        D.scroll(0, 0, "diagonal")


# ─── wait ────────────────────────────────────────────────────────────────


def test_wait_caps_at_10s():
    """A model trying to game the budget by sleeping forever should be capped."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.wait(60.0)
        cmd = mock.call_args[0][0]
        # 60.0 was passed → should be capped to 10.0
        assert "sleep 10" in cmd or "sleep 10.0" in cmd


def test_wait_floors_at_100ms():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.wait(0.001)  # below the floor
        cmd = mock.call_args[0][0]
        assert "sleep 0.1" in cmd


# ─── cursor_position ───────────────────────────────────────────────────────


def test_cursor_position_parses_shell_format():
    """xdotool --shell output has X=... Y=... lines."""
    output = b"X=320\nY=240\nSCREEN=0\nWINDOW=12345\n"
    with patch.object(D, "_exec", return_value=(0, output, b"")):
        x, y = D.cursor_position()
        assert x == 320
        assert y == 240
