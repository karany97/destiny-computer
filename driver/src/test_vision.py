"""D5b closure tests — vision-backend abstraction (issue #11).

Coverage:
  - StepResult dataclass shape + defaults
  - VisionBackend ABC enforcement
  - get_backend factory (anthropic / local-uitars / holo3 / unknown)
  - AnthropicVisionBackend: system prompt + tool config + initial
    history shape + step response parsing + append_action_result
    (regression-locks the behaviour the existing run_task tests assume)
  - Holo3VisionBackend: HTTP POST shape + cost=0.0 +
    _parse_holo3_response across every action type (click, type, key,
    scroll, wait, done, malformed → completion fallback)
  - Holo3 backend endpoint-unreachable → RuntimeError (caught by
    run_task as api_error)
"""
from __future__ import annotations

import json
import sys
import urllib.error
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def V(monkeypatch):
    """Fresh vision module per test (env defaults reset)."""
    monkeypatch.delenv("VISION_BACKEND", raising=False)
    monkeypatch.delenv("HOLO3_ENDPOINT", raising=False)
    monkeypatch.delenv("HOLO3_MODEL", raising=False)
    monkeypatch.delenv("HOLO3_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    sys.modules.pop("vision", None)
    sys.path.insert(0, "/tmp/destiny-computer/driver/src")
    import vision as M  # type: ignore[import-not-found]
    return M


# ────────── StepResult ──────────

def test_step_result_default_finish_false(V):
    s = V.StepResult()
    assert s.finish is False
    assert s.action is None
    assert s.text is None
    assert s.cost_usd == 0.0
    assert s.input_tokens == 0
    assert s.output_tokens == 0
    assert s.backend_marker is None


def test_step_result_finish_path(V):
    s = V.StepResult(text="done", finish=True)
    assert s.finish is True
    assert s.action is None


def test_step_result_action_path(V):
    s = V.StepResult(action={"action": "left_click"}, cost_usd=0.01)
    assert s.action == {"action": "left_click"}
    assert s.cost_usd == 0.01


# ────────── get_backend factory ──────────

def test_get_backend_default_anthropic(V, monkeypatch):
    """Unset VISION_BACKEND → anthropic (back-compat with v0.2)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.get_backend()
    assert isinstance(b, V.AnthropicVisionBackend)


def test_get_backend_explicit_anthropic(V, monkeypatch):
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.get_backend("anthropic")
    assert isinstance(b, V.AnthropicVisionBackend)


def test_get_backend_local_uitars(V):
    b = V.get_backend("local-uitars")
    assert isinstance(b, V.Holo3VisionBackend)


def test_get_backend_holo3_alias(V):
    """Operators sometimes type `holo3` instead of `local-uitars`. Accept."""
    b = V.get_backend("holo3")
    assert isinstance(b, V.Holo3VisionBackend)


def test_get_backend_case_insensitive(V):
    b = V.get_backend("Local-UITARS")
    assert isinstance(b, V.Holo3VisionBackend)


def test_get_backend_env_var_picked_up(V, monkeypatch):
    monkeypatch.setenv("VISION_BACKEND", "local-uitars")
    b = V.get_backend()
    assert isinstance(b, V.Holo3VisionBackend)


def test_get_backend_unknown_raises(V):
    with pytest.raises(ValueError, match="unknown VISION_BACKEND"):
        V.get_backend("magic-claude-7")


def test_get_backend_unknown_error_includes_supported_list(V):
    with pytest.raises(ValueError) as exc:
        V.get_backend("xyz")
    assert "anthropic" in str(exc.value)
    assert "local-uitars" in str(exc.value) or "holo3" in str(exc.value)


# ────────── ABC enforcement ──────────

def test_vision_backend_cannot_be_instantiated_directly(V):
    """VisionBackend is abstract — instantiating should raise."""
    with pytest.raises(TypeError):
        V.VisionBackend()


# ────────── AnthropicVisionBackend ──────────

def test_anthropic_backend_system_prompt_includes_screen_size(V, monkeypatch):
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.AnthropicVisionBackend()
    prompt = b._system_prompt(1920, 1080)
    assert "1920" in prompt
    assert "1080" in prompt


def test_anthropic_backend_tool_config_has_dimensions(V, monkeypatch):
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.AnthropicVisionBackend()
    cfg = b._tool_config(1280, 720)
    assert cfg[0]["display_width_px"] == 1280
    assert cfg[0]["display_height_px"] == 720
    assert cfg[0]["name"] == "computer"


def test_anthropic_backend_initial_history_shape(V, monkeypatch):
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.AnthropicVisionBackend()
    hist = b.initial_history("click the button", b"\x89PNG\x00\x01", (1280, 720))
    assert len(hist) == 1
    assert hist[0]["role"] == "user"
    # text + image block
    assert any(c.get("type") == "text" for c in hist[0]["content"])
    assert any(c.get("type") == "image" for c in hist[0]["content"])


def test_anthropic_backend_step_returns_finish_on_text_only_response(V, monkeypatch):
    """No tool_use block → finish=True."""
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.AnthropicVisionBackend()
    # Build a fake response with text-only content
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "done — opened the app"
    text_block.model_dump = lambda: {"type": "text", "text": "done — opened the app"}
    resp = MagicMock()
    resp.content = [text_block]
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 5
    b.client.beta.messages.create = lambda **kw: resp
    result = b.step([{"role": "user", "content": []}], (1280, 720))
    assert result.finish is True
    assert result.text == "done — opened the app"
    assert result.action is None
    assert result.cost_usd > 0  # 100 input tokens cost something


def test_anthropic_backend_step_extracts_tool_use(V, monkeypatch):
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.AnthropicVisionBackend()
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    tool_use.id = "tu_abc"
    tool_use.input = {"action": "left_click", "coordinate": [612, 431]}
    tool_use.model_dump = lambda: {"type": "tool_use", "id": "tu_abc",
                                    "input": tool_use.input}
    resp = MagicMock()
    resp.content = [tool_use]
    resp.usage.input_tokens = 50
    resp.usage.output_tokens = 10
    b.client.beta.messages.create = lambda **kw: resp
    result = b.step([], (1280, 720))
    assert result.finish is False
    assert result.action == {"action": "left_click", "coordinate": [612, 431]}
    assert result.backend_marker["tool_use_id"] == "tu_abc"


def test_anthropic_backend_append_action_result_pairs_tool_use_id(V, monkeypatch):
    """The append must include a tool_result with the same tool_use_id —
    Anthropic 400s otherwise."""
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.AnthropicVisionBackend()
    last = V.StepResult(
        action={"action": "left_click"},
        backend_marker={
            "assistant_blocks": [{"type": "tool_use", "id": "tu_xyz"}],
            "tool_use_id": "tu_xyz",
        },
    )
    history = b.append_action_result([], last, "clicked", True, b"\x89PNG")
    # Two new turns appended: assistant + user
    assert len(history) == 2
    assert history[0]["role"] == "assistant"
    assert history[1]["role"] == "user"
    # tool_result references the right id
    tool_result_block = history[1]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["tool_use_id"] == "tu_xyz"


def test_anthropic_backend_append_action_result_marks_is_error(V, monkeypatch):
    monkeypatch.setattr(V, "Anthropic", lambda **_: MagicMock())
    b = V.AnthropicVisionBackend()
    last = V.StepResult(
        action={"action": "left_click"},
        backend_marker={
            "assistant_blocks": [{"type": "tool_use", "id": "tu_x"}],
            "tool_use_id": "tu_x",
        },
    )
    history = b.append_action_result([], last, "out of bounds", False, b"\x89PNG")
    assert history[1]["content"][0].get("is_error") is True


# ────────── Holo3 response parser ──────────

@pytest.mark.parametrize("raw,expected_action,expected_finish", [
    (
        "Action: click\nCoordinate: [123, 456]\nReasoning: clicked the button",
        {"action": "left_click", "coordinate": [123, 456]},
        False,
    ),
    (
        'Action: type\nText: "hello world"\nReasoning: typing',
        {"action": "type", "text": "hello world"},
        False,
    ),
    (
        'Action: key\nText: "Return"\nReasoning: press enter',
        {"action": "key", "text": "Return"},
        False,
    ),
    (
        "Action: scroll\nCoordinate: [400, 300]\nDirection: down\nReasoning: scroll",
        {"action": "scroll", "coordinate": [400, 300], "scroll_direction": "down"},
        False,
    ),
    (
        "Action: wait\nReasoning: let the page load",
        {"action": "wait", "duration": 1},
        False,
    ),
    (
        "Action: done\nReasoning: task complete, opened the application",
        None,
        True,
    ),
])
def test_parse_holo3_action_shapes(V, raw, expected_action, expected_finish):
    action, text, finish = V._parse_holo3_response(raw)
    assert finish is expected_finish
    if expected_action is None:
        assert action is None
    else:
        # All expected_action keys present (action may have extras)
        for k, v in expected_action.items():
            assert action.get(k) == v


def test_parse_holo3_no_action_treated_as_completion(V):
    """If Holo3 emits text only (off-script), treat as completion."""
    raw = "I have completed the task by opening Firefox and searching."
    action, text, finish = V._parse_holo3_response(raw)
    assert action is None
    assert finish is True
    assert text == raw


def test_parse_holo3_empty_string_is_completion(V):
    action, text, finish = V._parse_holo3_response("")
    assert action is None
    assert finish is True
    assert text is None


def test_parse_holo3_extracts_reasoning(V):
    raw = ("Action: click\n"
           "Coordinate: [100, 200]\n"
           "Reasoning: This is the New Tab button in the top-left")
    action, text, finish = V._parse_holo3_response(raw)
    assert text is not None
    assert "New Tab" in text


def test_parse_holo3_right_click(V):
    raw = "Action: right_click\nCoordinate: [50, 50]"
    action, _, _ = V._parse_holo3_response(raw)
    assert action == {"action": "right_click", "coordinate": [50, 50]}


def test_parse_holo3_double_click(V):
    raw = "Action: double_click\nCoordinate: [50, 50]"
    action, _, _ = V._parse_holo3_response(raw)
    assert action == {"action": "double_click", "coordinate": [50, 50]}


def test_parse_holo3_mouse_move(V):
    raw = "Action: mouse_move\nCoordinate: [10, 20]"
    action, _, _ = V._parse_holo3_response(raw)
    assert action == {"action": "mouse_move", "coordinate": [10, 20]}


def test_parse_holo3_text_with_escaped_quotes(V):
    """Text args with escaped quotes inside the JSON-style literal."""
    raw = 'Action: type\nText: "say \\"hello\\""'
    action, _, _ = V._parse_holo3_response(raw)
    assert action["action"] == "type"
    # The escaped backslash sequences come through as-is (Holo3 doesn't
    # actually need to escape — we accept the raw string).
    assert "hello" in action["text"]


# ────────── Holo3VisionBackend HTTP path ──────────

def test_holo3_backend_default_endpoint(V):
    b = V.Holo3VisionBackend()
    assert b.endpoint == "http://holo3:8000/v1"
    assert b.model == "Hcompany/Holo3-35B-A3B"
    assert b.api_key == "EMPTY"


def test_holo3_backend_env_override(V, monkeypatch):
    monkeypatch.setenv("HOLO3_ENDPOINT", "http://10.0.0.5:9000/v1/")
    monkeypatch.setenv("HOLO3_MODEL", "Hcompany/Holo3-9B-A1B")
    monkeypatch.setenv("HOLO3_API_KEY", "my-secret")
    b = V.Holo3VisionBackend()
    # Trailing slash stripped
    assert b.endpoint == "http://10.0.0.5:9000/v1"
    assert b.model == "Hcompany/Holo3-9B-A1B"
    assert b.api_key == "my-secret"


def test_holo3_backend_initial_history_has_system_prompt(V):
    b = V.Holo3VisionBackend()
    hist = b.initial_history("click the button", b"\x89PNG", (1024, 768))
    assert len(hist) == 2
    assert hist[0]["role"] == "system"
    assert "Holo3" in hist[0]["content"]
    assert "1024" in hist[0]["content"]  # screen size in prompt
    assert hist[1]["role"] == "user"


def test_holo3_backend_step_posts_chat_completions(V, monkeypatch):
    """Verify the HTTP request shape — endpoint + body + headers."""
    b = V.Holo3VisionBackend()
    captured = {}

    class _FakeResp:
        def __init__(self, body): self._body = body
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = dict(req.headers)
        body = json.dumps({
            "choices": [{"message": {"content": "Action: done\nReasoning: ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode("utf-8")
        return _FakeResp(body)

    monkeypatch.setattr(V.urllib.request, "urlopen", fake_urlopen)
    b.step([{"role": "user", "content": "x"}], (1280, 720))
    # URL hits the chat completions endpoint
    assert "/chat/completions" in captured["url"]
    # Authorization header carries the API key as a Bearer
    auth = captured["headers"].get("Authorization", "")
    assert auth.startswith("Bearer ")
    # Body has the model + messages
    payload = json.loads(captured["data"])
    assert payload["model"] == "Hcompany/Holo3-35B-A3B"
    assert "messages" in payload


def test_holo3_backend_step_zero_cost(V, monkeypatch):
    """Local model — cost_usd MUST be 0 (don't accidentally bill the
    operator for a self-hosted call)."""
    b = V.Holo3VisionBackend()

    class _FakeResp:
        def read(self): return json.dumps({
            "choices": [{"message": {"content": "Action: done"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        }).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(V.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    result = b.step([], (1280, 720))
    assert result.cost_usd == 0.0
    # But tokens still captured for the ledger
    assert result.input_tokens == 1000
    assert result.output_tokens == 100


def test_holo3_backend_step_parses_action(V, monkeypatch):
    b = V.Holo3VisionBackend()

    class _FakeResp:
        def read(self): return json.dumps({
            "choices": [{"message": {"content":
                "Action: click\nCoordinate: [100, 200]\nReasoning: button"
            }}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5},
        }).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(V.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    result = b.step([], (1280, 720))
    assert result.finish is False
    assert result.action == {"action": "left_click", "coordinate": [100, 200]}


def test_holo3_backend_step_endpoint_unreachable_raises_runtime_error(V, monkeypatch):
    """When vLLM is down, raise RuntimeError so run_task catches it
    and sets api_error (instead of leaking a urllib stacktrace to the
    operator)."""
    b = V.Holo3VisionBackend()
    monkeypatch.setattr(V.urllib.request, "urlopen",
                        MagicMock(side_effect=urllib.error.URLError("ECONNREFUSED")))
    with pytest.raises(RuntimeError, match="Holo3 endpoint unreachable"):
        b.step([], (1280, 720))


def test_holo3_backend_step_non_json_response_raises(V, monkeypatch):
    """Misconfigured proxy returns HTML 502 page → raise RuntimeError."""
    b = V.Holo3VisionBackend()

    class _FakeResp:
        def read(self): return b"<html>502 Bad Gateway</html>"
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(V.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    with pytest.raises(RuntimeError, match="non-JSON"):
        b.step([], (1280, 720))


def test_holo3_backend_step_missing_choices_raises(V, monkeypatch):
    """vLLM crash mid-stream → choices=[] in the response. Raise."""
    b = V.Holo3VisionBackend()

    class _FakeResp:
        def read(self): return json.dumps({"choices": []}).encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(V.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    with pytest.raises(RuntimeError, match="missing choices"):
        b.step([], (1280, 720))


def test_holo3_backend_append_action_result_adds_text_and_screenshot(V):
    b = V.Holo3VisionBackend()
    last = V.StepResult(
        action={"action": "left_click"},
        backend_marker={"assistant_content": "Action: click\nCoordinate: [1,2]"},
    )
    hist = b.append_action_result([], last, "clicked", True, b"\x89PNG")
    assert len(hist) == 2
    assert hist[0]["role"] == "assistant"
    assert hist[1]["role"] == "user"
    # User turn carries the action result text AND the screenshot
    assert any(c.get("type") == "text" for c in hist[1]["content"])
    assert any(c.get("type") == "image_url" for c in hist[1]["content"])


def test_holo3_backend_append_action_result_signals_failure(V):
    b = V.Holo3VisionBackend()
    last = V.StepResult(
        action={"action": "left_click"},
        backend_marker={"assistant_content": "Action: click"},
    )
    hist = b.append_action_result([], last, "out of bounds", False, b"\x89PNG")
    text_content = hist[1]["content"][0]["text"]
    assert "ok=False" in text_content


# ────────── source regression guards ──────────

def test_vision_module_exports_required_names():
    """If a future refactor renames these, downstream imports break."""
    import vision as V  # type: ignore[import-not-found]
    assert hasattr(V, "VisionBackend")
    assert hasattr(V, "AnthropicVisionBackend")
    assert hasattr(V, "Holo3VisionBackend")
    assert hasattr(V, "StepResult")
    assert hasattr(V, "get_backend")


def test_holo3_image_block_uses_data_uri():
    """OpenAI vision spec: image_url with data: URI for base64."""
    import vision as V
    block = V._holo3_image_block(b"\x89PNG")
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_anthropic_image_block_uses_anthropic_shape():
    """Anthropic spec: image source with type=base64."""
    import vision as V
    block = V._anthropic_image_block(b"\x89PNG")
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
