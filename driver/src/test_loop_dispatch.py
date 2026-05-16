"""Coverage for loop._dispatch_action — every Anthropic computer_20251124 verb.

The dispatcher is the translator between Anthropic's tool_use input dicts
and our desktop.py primitives. Both ends are well-tested individually;
this file makes sure NO action emitted by Computer Use ever falls through
to the desktop module wrong (which would silently misbehave instead of
raising — the worst bug class for an autonomous agent).
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

import desktop as D  # type: ignore[import-not-found]
import loop as L  # type: ignore[import-not-found]


# ────────── stub helper ──────────

def _stub_action():
    """Return a successful ActionResult for desktop module calls."""
    return D.ActionResult(ok=True, message="stubbed")


# ────────── screenshot ──────────

def test_dispatch_screenshot_returns_ok_without_calling_desktop():
    """screenshot is a no-op in the dispatcher — the loop always captures
    a post-action screenshot anyway, so dispatching one would be redundant
    AND slow (each xwd|convert is ~300ms inside the container)."""
    with patch.object(D, "screenshot") as ds:
        result = L._dispatch_action({"action": "screenshot"})
        assert result.ok is True
        ds.assert_not_called()


# ────────── mouse_move ──────────

def test_dispatch_mouse_move_threads_coordinate():
    with patch.object(D, "mouse_move", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "mouse_move", "coordinate": [100, 200]})
        m.assert_called_once_with(100, 200)


def test_dispatch_mouse_move_accepts_coord_alias():
    """Some clients send `coord` instead of `coordinate` — accept both."""
    with patch.object(D, "mouse_move", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "mouse_move", "coord": [50, 75]})
        m.assert_called_once_with(50, 75)


def test_dispatch_mouse_move_tuple_coordinate():
    """Anthropic sometimes emits tuples; convert just like lists."""
    with patch.object(D, "mouse_move", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "mouse_move", "coordinate": (10, 20)})
        m.assert_called_once_with(10, 20)


# ────────── left_click ──────────

def test_dispatch_left_click_with_coords():
    with patch.object(D, "left_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "left_click", "coordinate": [50, 60]})
        m.assert_called_once_with(50, 60)


def test_dispatch_left_click_without_coords():
    """No coordinate → click at current cursor."""
    with patch.object(D, "left_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "left_click"})
        m.assert_called_once_with(None, None)


# ────────── right/middle/double/triple click ──────────

def test_dispatch_right_click_with_coords():
    with patch.object(D, "right_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "right_click", "coordinate": [5, 5]})
        m.assert_called_once_with(5, 5)


def test_dispatch_middle_click_with_coords():
    with patch.object(D, "middle_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "middle_click", "coordinate": [10, 10]})
        m.assert_called_once_with(10, 10)


def test_dispatch_double_click_with_coords():
    with patch.object(D, "double_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "double_click", "coordinate": [15, 15]})
        m.assert_called_once_with(15, 15)


def test_dispatch_triple_click_with_coords():
    with patch.object(D, "triple_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "triple_click", "coordinate": [20, 20]})
        m.assert_called_once_with(20, 20)


def test_dispatch_right_click_no_coords():
    with patch.object(D, "right_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "right_click"})
        m.assert_called_once_with(None, None)


def test_dispatch_middle_click_no_coords():
    with patch.object(D, "middle_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "middle_click"})
        m.assert_called_once_with(None, None)


def test_dispatch_double_click_no_coords():
    with patch.object(D, "double_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "double_click"})
        m.assert_called_once_with(None, None)


def test_dispatch_triple_click_no_coords():
    with patch.object(D, "triple_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "triple_click"})
        m.assert_called_once_with(None, None)


# ────────── mouse down/up ──────────

def test_dispatch_left_mouse_down_with_coords():
    with patch.object(D, "left_mouse_down", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "left_mouse_down", "coordinate": [1, 2]})
        m.assert_called_once_with(1, 2)


def test_dispatch_left_mouse_down_no_coords():
    with patch.object(D, "left_mouse_down", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "left_mouse_down"})
        m.assert_called_once_with(None, None)


def test_dispatch_left_mouse_up_with_coords():
    with patch.object(D, "left_mouse_up", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "left_mouse_up", "coordinate": [3, 4]})
        m.assert_called_once_with(3, 4)


def test_dispatch_left_mouse_up_no_coords():
    with patch.object(D, "left_mouse_up", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "left_mouse_up"})
        m.assert_called_once_with(None, None)


# ────────── left_click_drag ──────────

def test_dispatch_drag_threads_start_and_end():
    with patch.object(D, "left_click_drag", return_value=_stub_action()) as m:
        L._dispatch_action({
            "action": "left_click_drag",
            "start_coordinate": [10, 20],
            "coordinate": [100, 200],
        })
        m.assert_called_once_with(10, 20, 100, 200)


def test_dispatch_drag_missing_start_raises():
    with pytest.raises(D.DesktopError, match="start_coordinate"):
        L._dispatch_action({
            "action": "left_click_drag",
            "coordinate": [100, 200],
        })


def test_dispatch_drag_missing_end_raises():
    with pytest.raises(D.DesktopError, match="end point"):
        L._dispatch_action({
            "action": "left_click_drag",
            "start_coordinate": [10, 20],
        })


def test_dispatch_drag_bad_start_length_raises():
    """start_coordinate=[1] should be rejected, not silently used."""
    with pytest.raises(D.DesktopError, match="start_coordinate"):
        L._dispatch_action({
            "action": "left_click_drag",
            "start_coordinate": [1],
            "coordinate": [100, 200],
        })


# ────────── type ──────────

def test_dispatch_type_passes_text():
    with patch.object(D, "type_text", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "type", "text": "hello world"})
        m.assert_called_once_with("hello world")


def test_dispatch_type_empty_text():
    """Model can emit empty string — still call type_text (not skip)."""
    with patch.object(D, "type_text", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "type", "text": ""})
        m.assert_called_once_with("")


def test_dispatch_type_missing_text_defaults_empty():
    """Defensive — missing text shouldn't blow up the dispatcher."""
    with patch.object(D, "type_text", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "type"})
        m.assert_called_once_with("")


# ────────── key ──────────

def test_dispatch_key_passes_text():
    with patch.object(D, "key_press", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "key", "text": "Return"})
        m.assert_called_once_with("Return")


def test_dispatch_key_chord():
    with patch.object(D, "key_press", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "key", "text": "ctrl+t"})
        m.assert_called_once_with("ctrl+t")


def test_dispatch_key_missing_text_defaults_empty():
    """Empty key won't do anything useful but the dispatcher shouldn't crash."""
    with patch.object(D, "key_press", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "key"})
        m.assert_called_once_with("")


# ────────── hold_key ──────────

def test_dispatch_hold_key_converts_seconds_to_ms():
    """Anthropic schema uses `duration` in SECONDS; desktop API takes ms."""
    with patch.object(D, "hold_key", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "hold_key", "text": "space", "duration": 2})
        m.assert_called_once_with("space", 2000)


def test_dispatch_hold_key_default_duration():
    """Missing duration → 1 second → 1000 ms."""
    with patch.object(D, "hold_key", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "hold_key", "text": "shift"})
        m.assert_called_once_with("shift", 1000)


def test_dispatch_hold_key_empty_text():
    """No key → empty string passed; desktop.py will reject with DesktopError."""
    with patch.object(D, "hold_key", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "hold_key", "duration": 1})
        m.assert_called_once_with("", 1000)


# ────────── scroll ──────────

def test_dispatch_scroll_with_coords():
    with patch.object(D, "scroll", return_value=_stub_action()) as m:
        L._dispatch_action({
            "action": "scroll",
            "coordinate": [400, 300],
            "scroll_direction": "down",
            "scroll_amount": 5,
        })
        m.assert_called_once_with(400, 300, "down", 5)


def test_dispatch_scroll_default_direction_is_down():
    with patch.object(D, "scroll", return_value=_stub_action()) as m:
        L._dispatch_action({
            "action": "scroll",
            "coordinate": [10, 10],
        })
        m.assert_called_once_with(10, 10, "down", 3)


def test_dispatch_scroll_default_amount_is_3():
    with patch.object(D, "scroll", return_value=_stub_action()) as m:
        L._dispatch_action({
            "action": "scroll",
            "coordinate": [10, 10],
            "scroll_direction": "up",
        })
        args, _ = m.call_args
        assert args[3] == 3


def test_dispatch_scroll_without_coords_uses_cursor_position():
    """Anthropic sometimes drops the coord when 'scroll where you are'."""
    with patch.object(D, "cursor_position", return_value=(50, 60)) as cp, \
         patch.object(D, "scroll", return_value=_stub_action()) as m:
        L._dispatch_action({
            "action": "scroll",
            "scroll_direction": "down",
            "scroll_amount": 2,
        })
        cp.assert_called_once()
        m.assert_called_once_with(50, 60, "down", 2)


# ────────── wait ──────────

def test_dispatch_wait_passes_duration():
    with patch.object(D, "wait", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "wait", "duration": 3.5})
        m.assert_called_once_with(3.5)


def test_dispatch_wait_default_duration_is_1():
    with patch.object(D, "wait", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "wait"})
        m.assert_called_once_with(1.0)


# ────────── cursor_position ──────────

def test_dispatch_cursor_position_returns_ok_with_message():
    with patch.object(D, "cursor_position", return_value=(123, 456)):
        result = L._dispatch_action({"action": "cursor_position"})
        assert result.ok is True
        assert "123" in result.message
        assert "456" in result.message


# ────────── error paths ──────────

def test_dispatch_missing_action_raises():
    with pytest.raises(D.DesktopError, match="missing 'action'"):
        L._dispatch_action({})


def test_dispatch_none_action_raises():
    with pytest.raises(D.DesktopError, match="missing 'action'"):
        L._dispatch_action({"action": None})


def test_dispatch_unknown_action_raises():
    with pytest.raises(D.DesktopError, match="unknown action"):
        L._dispatch_action({"action": "warpdrive"})


def test_dispatch_unknown_action_includes_name_in_error():
    """Operator reading the error needs to know WHICH action was unknown."""
    with pytest.raises(D.DesktopError) as exc:
        L._dispatch_action({"action": "fly_to_moon"})
    assert "fly_to_moon" in str(exc.value)


# ────────── coord parsing edge cases ──────────

def test_dispatch_bad_coordinate_length_falls_back_to_none():
    """coordinate=[5] is invalid; dispatch should pass None to desktop."""
    with patch.object(D, "left_click", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "left_click", "coordinate": [5]})
        m.assert_called_once_with(None, None)


def test_dispatch_coord_with_floats_converted_to_int():
    """Anthropic occasionally emits floats — dispatcher must int() them."""
    with patch.object(D, "mouse_move", return_value=_stub_action()) as m:
        L._dispatch_action({"action": "mouse_move", "coordinate": [10.7, 20.3]})
        m.assert_called_once_with(10, 20)
