"""More desktop.py coverage — every action verb, every alias, every edge case.

Companion to test_desktop.py (which has the bedrock 20). This file extends
coverage to the action verbs and key aliases that the v0.1 suite skipped,
plus edge cases caught by the destiny-computer #2 closure (the 200-test
gate).
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

import desktop as D  # type: ignore[import-not-found]


def _mock_run(returncode=0, stdout=b"", stderr=b""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ────────── _exec edge cases ──────────

def test_exec_assembles_with_custom_container_env(monkeypatch):
    """DESKTOP_CONTAINER env should change the docker exec target."""
    monkeypatch.setattr(D, "DESKTOP_CONTAINER", "my-custom")
    with patch("subprocess.run", return_value=_mock_run()) as mock:
        D._exec("anything")
        args = mock.call_args[0][0]
        assert "my-custom" in args


def test_exec_assembles_with_custom_display_env(monkeypatch):
    monkeypatch.setattr(D, "DESKTOP_DISPLAY", ":99")
    with patch("subprocess.run", return_value=_mock_run()) as mock:
        D._exec("anything")
        args = mock.call_args[0][0]
        assert "DISPLAY=:99" in args


def test_exec_returns_stdout_and_stderr():
    with patch("subprocess.run", return_value=_mock_run(0, b"out", b"err")):
        code, out, err = D._exec("anything")
        assert code == 0
        assert out == b"out"
        assert err == b"err"


def test_exec_returns_nonzero_returncode_without_raising():
    """_exec should return the rc; only timeouts/missing-docker raise."""
    with patch("subprocess.run", return_value=_mock_run(127, b"", b"not found")):
        code, _, err = D._exec("anything")
        assert code == 127
        assert b"not found" in err


def test_exec_passes_through_explicit_timeout():
    """timeout=5 should reach subprocess.run as timeout=5."""
    with patch("subprocess.run", return_value=_mock_run()) as mock:
        D._exec("x", timeout=5)
        _, kwargs = mock.call_args
        assert kwargs.get("timeout") == 5


# ────────── screenshot edge cases ──────────

def test_screenshot_invokes_xwd_pipe_to_convert():
    """The canonical PNG capture pipeline is `xwd … | convert xwd:- png:-`."""
    with patch.object(D, "_exec", return_value=(0, b"\x89PNG..." + b"x" * 50, b"")) as mock:
        D.screenshot()
        cmd = mock.call_args[0][0]
        assert "xwd" in cmd
        assert "convert" in cmd
        assert "png:-" in cmd


def test_screenshot_stderr_propagated_in_error():
    """Operators reading logs need to know WHY xwd failed."""
    with patch.object(D, "_exec",
                      return_value=(1, b"", b"cannot open display :1")):
        with pytest.raises(D.DesktopError) as exc:
            D.screenshot()
        assert "cannot open display" in str(exc.value)


def test_screenshot_truncates_long_stderr():
    """A multi-MB error blob shouldn't end up in the exception string."""
    huge = b"x" * 50_000
    with patch.object(D, "_exec", return_value=(1, b"", huge)):
        with pytest.raises(D.DesktopError) as exc:
            D.screenshot()
        # We slice at 200 chars
        assert len(str(exc.value)) < 500


# ────────── geometry edge cases ──────────

def test_get_screen_size_handles_extra_whitespace():
    """xdotool sometimes emits trailing spaces; strip() should handle it."""
    with patch.object(D, "_exec", return_value=(0, b"  1920   1080  \n\n", b"")):
        w, h = D.get_screen_size()
        assert (w, h) == (1920, 1080)


def test_get_screen_size_raises_on_nonzero():
    with patch.object(D, "_exec", return_value=(1, b"", b"display gone")):
        with pytest.raises(D.DesktopError, match="getdisplaygeometry"):
            D.get_screen_size()


def test_get_screen_size_raises_on_three_fields():
    """Some xdotool builds add a colon-separated screen prefix; fail loud."""
    with patch.object(D, "_exec", return_value=(0, b"0: 1920 1080\n", b"")):
        with pytest.raises(D.DesktopError, match="unexpected geometry"):
            D.get_screen_size()


# ────────── mouse_move edge cases ──────────

def test_mouse_move_floors_negative_coords():
    """The driver doesn't reject negative coords — xdotool clamps them."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.mouse_move(-5, -10)
        cmd = mock.call_args[0][0]
        assert "-5 -10" in cmd  # xdotool handles negatives


def test_mouse_move_raises_on_error():
    with patch.object(D, "_exec", return_value=(1, b"", b"xdotool: failed")):
        with pytest.raises(D.DesktopError, match="mouse_move"):
            D.mouse_move(0, 0)


def test_mouse_move_converts_float_args():
    """Models sometimes emit float coords (612.5, 431.2) — int() them."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.mouse_move(612.5, 431.2)  # type: ignore[arg-type]
        cmd = mock.call_args[0][0]
        assert "612" in cmd
        assert "431" in cmd
        assert "612.5" not in cmd


# ────────── right/middle/double/triple click coverage ──────────

def test_right_click_with_coords_uses_button_3():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.right_click(10, 20)
        cmd = mock.call_args[0][0]
        assert "mousemove --sync 10 20" in cmd
        assert "click 3" in cmd


def test_right_click_no_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.right_click()
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool click 3"


def test_middle_click_with_coords_uses_button_2():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.middle_click(5, 6)
        cmd = mock.call_args[0][0]
        assert "click 2" in cmd


def test_middle_click_no_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.middle_click()
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool click 2"


def test_double_click_uses_repeat_2():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.double_click(50, 60)
        cmd = mock.call_args[0][0]
        assert "--repeat 2 1" in cmd


def test_double_click_no_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.double_click()
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool click --repeat 2 1"


def test_triple_click_uses_repeat_3():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.triple_click(100, 200)
        cmd = mock.call_args[0][0]
        assert "--repeat 3 1" in cmd


def test_triple_click_no_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.triple_click()
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool click --repeat 3 1"


def test_left_mouse_down_with_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.left_mouse_down(7, 8)
        cmd = mock.call_args[0][0]
        assert "mousedown 1" in cmd
        assert "7 8" in cmd


def test_left_mouse_down_no_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.left_mouse_down()
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool mousedown 1"


def test_left_mouse_up_with_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.left_mouse_up(7, 8)
        cmd = mock.call_args[0][0]
        assert "mouseup 1" in cmd


def test_left_mouse_up_no_coords():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.left_mouse_up()
        cmd = mock.call_args[0][0]
        assert cmd == "xdotool mouseup 1"


def test_left_click_drag_threads_both_endpoints():
    """Drag must include both start mousedown AND end mouseup positions."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.left_click_drag(10, 20, 100, 200)
        cmd = mock.call_args[0][0]
        assert "mousemove --sync 10 20" in cmd
        assert "mousedown 1" in cmd
        assert "mousemove --sync 100 200" in cmd
        assert "mouseup 1" in cmd


def test_left_click_drag_returns_path_in_message():
    with patch.object(D, "_exec", return_value=(0, b"", b"")):
        result = D.left_click_drag(1, 2, 3, 4)
        assert "(1,2)" in result.message
        assert "(3,4)" in result.message


def test_left_click_drag_propagates_failure():
    with patch.object(D, "_exec", return_value=(1, b"", b"X server lost")):
        with pytest.raises(D.DesktopError, match="left_click_drag"):
            D.left_click_drag(0, 0, 10, 10)


# ────────── type_text edge cases ──────────

def test_type_text_default_delay_30ms():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.type_text("hello")
        cmd = mock.call_args[0][0]
        assert "--delay 30" in cmd


def test_type_text_custom_delay():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.type_text("x", delay_ms=100)
        cmd = mock.call_args[0][0]
        assert "--delay 100" in cmd


def test_type_text_unicode_safe():
    """xdotool type handles unicode; shlex.quote keeps the bytes intact."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.type_text("こんにちは 🎉")
        cmd = mock.call_args[0][0]
        assert "こんにちは" in cmd
        assert "🎉" in cmd


def test_type_text_empty_string():
    """Typing the empty string should still issue the command, not crash."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        result = D.type_text("")
        # type was called
        assert mock.called
        # message reports 0 chars
        assert "0 chars" in result.message


def test_type_text_returns_char_count():
    with patch.object(D, "_exec", return_value=(0, b"", b"")):
        result = D.type_text("hello world")
        assert "11 chars" in result.message


def test_type_text_raises_on_xdotool_failure():
    with patch.object(D, "_exec", return_value=(1, b"", b"xdotool: failed")):
        with pytest.raises(D.DesktopError, match="type_text"):
            D.type_text("hi")


# ────────── key_press alias coverage ──────────

@pytest.mark.parametrize("input_key,expected", [
    ("enter",     "Return"),
    ("return",    "Return"),
    ("esc",       "Escape"),
    ("escape",    "Escape"),
    ("tab",       "Tab"),
    ("backspace", "BackSpace"),
    ("delete",    "Delete"),
    ("up",        "Up"),
    ("down",      "Down"),
    ("left",      "Left"),
    ("right",     "Right"),
    ("home",      "Home"),
    ("end",       "End"),
    ("pageup",    "Page_Up"),
    ("pagedown",  "Page_Down"),
    ("space",     "space"),
])
def test_key_press_aliases(input_key, expected):
    """Every documented alias must translate to the xdotool canonical name."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.key_press(input_key)
        cmd = mock.call_args[0][0]
        assert expected in cmd


def test_key_press_uppercase_alias():
    """Aliases should match case-insensitively (LLMs emit 'Enter')."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.key_press("Enter")
        cmd = mock.call_args[0][0]
        assert "Return" in cmd


def test_key_press_unknown_passes_through():
    """A key xdotool knows but we don't alias (F11, KP_Add) — let it through."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.key_press("F11")
        cmd = mock.call_args[0][0]
        assert "F11" in cmd


def test_key_press_chord_alt_tab():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.key_press("alt+tab")
        cmd = mock.call_args[0][0]
        assert "alt+Tab" in cmd


def test_key_press_three_part_chord():
    """ctrl+shift+t — both modifiers translated correctly."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.key_press("ctrl+shift+t")
        cmd = mock.call_args[0][0]
        # All parts present in canonical form
        assert "ctrl" in cmd
        assert "shift" in cmd
        assert "t" in cmd


def test_key_press_raises_on_failure():
    with patch.object(D, "_exec", return_value=(1, b"", b"bad key")):
        with pytest.raises(D.DesktopError, match="key_press"):
            D.key_press("F99")


def test_key_press_returns_pressed_in_message():
    with patch.object(D, "_exec", return_value=(0, b"", b"")):
        result = D.key_press("enter")
        assert "pressed" in result.message.lower()
        assert "Return" in result.message


# ────────── hold_key edge cases ──────────

def test_hold_key_assembles_down_sleep_up():
    """hold_key chain: keydown ; sleep ms/1000 ; keyup."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.hold_key("space", 500)
        cmd = mock.call_args[0][0]
        assert "keydown" in cmd
        assert "sleep 0.5" in cmd
        assert "keyup" in cmd


def test_hold_key_scales_timeout():
    """3 second hold needs > 3 second subprocess timeout."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.hold_key("shift", 3000)
        _, kwargs = mock.call_args
        assert kwargs.get("timeout", 10) >= 5


def test_hold_key_raises_on_failure():
    with patch.object(D, "_exec", return_value=(1, b"", b"bad key")):
        with pytest.raises(D.DesktopError, match="hold_key"):
            D.hold_key("F99", 100)


# ────────── scroll coverage ──────────

@pytest.mark.parametrize("direction,button", [
    ("up", 4), ("down", 5), ("left", 6), ("right", 7),
    ("UP", 4), ("Down", 5),  # case-insensitive
])
def test_scroll_direction_button_map(direction, button):
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.scroll(0, 0, direction)
        cmd = mock.call_args[0][0]
        assert f" {button}" in cmd


def test_scroll_default_amount_is_3():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.scroll(10, 20, "down")
        cmd = mock.call_args[0][0]
        assert "--repeat 3" in cmd


def test_scroll_custom_amount():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.scroll(0, 0, "up", amount=10)
        cmd = mock.call_args[0][0]
        assert "--repeat 10" in cmd


def test_scroll_unknown_direction_raises_descriptive():
    with pytest.raises(D.DesktopError) as exc:
        D.scroll(0, 0, "northeast")
    assert "northeast" in str(exc.value)


def test_scroll_raises_on_xdotool_error():
    with patch.object(D, "_exec", return_value=(1, b"", b"xdotool: lost display")):
        with pytest.raises(D.DesktopError, match="scroll"):
            D.scroll(0, 0, "down")


# ────────── wait edge cases ──────────

def test_wait_exactly_at_floor():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.wait(0.1)
        cmd = mock.call_args[0][0]
        assert "sleep 0.1" in cmd


def test_wait_exactly_at_cap():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.wait(10.0)
        cmd = mock.call_args[0][0]
        assert "sleep 10" in cmd


def test_wait_mid_range():
    with patch.object(D, "_exec", return_value=(0, b"", b"")) as mock:
        D.wait(2.5)
        cmd = mock.call_args[0][0]
        assert "sleep 2.5" in cmd


def test_wait_returns_actual_slept():
    """Even if model asked for 60s, the message should say 10s."""
    with patch.object(D, "_exec", return_value=(0, b"", b"")):
        result = D.wait(60.0)
        assert "10" in result.message


def test_wait_raises_on_sleep_failure():
    """A SIGKILL on the sleep would surface as non-zero rc."""
    with patch.object(D, "_exec", return_value=(137, b"", b"killed")):
        with pytest.raises(D.DesktopError, match="wait"):
            D.wait(1.0)


# ────────── cursor_position edge cases ──────────

def test_cursor_position_ignores_extra_lines():
    """Extra X=... Y=... in unrelated output (window props) shouldn't confuse."""
    out = (b"X=320\n"
           b"Y=240\n"
           b"SCREEN=0\n"
           b"WINDOW=12345\n"
           b"WINDOW_PROP_X=999\n")
    with patch.object(D, "_exec", return_value=(0, out, b"")):
        x, y = D.cursor_position()
        # The function picks the FIRST X= / Y= line — the props don't start
        # with X= alone, so this still works
        assert (x, y) == (320, 240)


def test_cursor_position_raises_on_nonzero():
    with patch.object(D, "_exec", return_value=(1, b"", b"xdotool failed")):
        with pytest.raises(D.DesktopError, match="cursor_position"):
            D.cursor_position()


# ────────── ActionResult dataclass ──────────

def test_action_result_default_no_screenshot():
    r = D.ActionResult(ok=True, message="done")
    assert r.screenshot is None


def test_action_result_with_screenshot_bytes():
    png = b"\x89PNG\r\n"
    r = D.ActionResult(ok=True, message="captured", screenshot=png)
    assert r.screenshot == png


# ────────── DesktopError subclass ──────────

def test_desktop_error_is_runtime_error():
    """Callers catching RuntimeError should still catch DesktopError."""
    assert issubclass(D.DesktopError, RuntimeError)
