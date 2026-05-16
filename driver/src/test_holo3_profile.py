"""D5a closure tests — Holo3 vLLM sidecar compose profile (issue #7).

Mirrors atelier-os test_holo3_profile.py shape. Differences:
  - profile name is `local-vision` (matches the issue acceptance which
    specified that name); env var routing is VISION_BACKEND=local-uitars
  - holo3 sidecar lives alongside the single `desktop` + `driver`
    services (destiny-computer is single-session, not multi-)
  - driver's VISION_BACKEND=local-* path bypasses the ANTHROPIC_API_KEY
    precondition (verified in test_main_endpoints already; here we
    verify the env wiring in compose)
  - D5b (loop.py routing wiring) is a follow-up; this PR ships the
    sidecar + the precondition relaxation only. D5b filed separately.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


COMPOSE_PATH = Path("/tmp/destiny-computer/compose/docker-compose.yml")
ENV_EXAMPLE  = Path("/tmp/destiny-computer/.env.example")
README_PATH  = Path("/tmp/destiny-computer/README.md")
TRACKING_PATH = Path("/tmp/destiny-computer/TRACKING.md")


@pytest.fixture(scope="module")
def compose():
    raw = COMPOSE_PATH.read_text()
    try:
        import yaml  # type: ignore
        return yaml.safe_load(raw)
    except ImportError:
        pytest.skip("PyYAML not installed; skipping structural tests")


# ────────── compose structure ──────────

def test_compose_parses_with_holo3(compose):
    assert "holo3" in compose["services"]


def test_holo3_gated_behind_local_vision_profile(compose):
    """Default `docker compose up` must NOT spawn the GPU sidecar
    — would crash on hosts without nvidia-container-toolkit."""
    profiles = compose["services"]["holo3"].get("profiles", [])
    assert "local-vision" in profiles


def test_holo3_uses_pinned_vllm_image(compose):
    img = compose["services"]["holo3"]["image"]
    assert img.startswith("vllm/vllm-openai")


def test_holo3_reserves_nvidia_gpu(compose):
    devices = (compose["services"]["holo3"]
               .get("deploy", {})
               .get("resources", {})
               .get("reservations", {})
               .get("devices", []))
    nvidia = [d for d in devices if d.get("driver") == "nvidia"]
    assert nvidia
    assert nvidia[0].get("count", 0) >= 1


def test_holo3_command_uses_holo3_model_default(compose):
    cmd = " ".join(str(c) for c in compose["services"]["holo3"]["command"])
    assert "Hcompany/Holo3-35B-A3B" in cmd or "HOLO3_MODEL" in cmd


def test_holo3_command_safe_gpu_memory_util(compose):
    cmd = " ".join(str(c) for c in compose["services"]["holo3"]["command"])
    m = re.search(r"--gpu-memory-utilization[^0-9]*([0-9.]+)", cmd)
    assert m
    util = float(m.group(1))
    assert 0 < util <= 0.95


def test_holo3_command_caps_max_model_len(compose):
    cmd = " ".join(str(c) for c in compose["services"]["holo3"]["command"])
    m = re.search(r"--max-model-len[^0-9]*([0-9]+)", cmd)
    assert m
    assert int(m.group(1)) <= 32768


def test_holo3_command_uses_enforce_eager(compose):
    cmd = " ".join(str(c) for c in compose["services"]["holo3"]["command"])
    assert "--enforce-eager" in cmd


def test_holo3_models_volume_declared(compose):
    vols = " ".join(compose["services"]["holo3"].get("volumes", []))
    assert "holo3-models" in vols
    assert "holo3-models" in compose.get("volumes", {})


def test_holo3_healthcheck_start_period_long(compose):
    sp = compose["services"]["holo3"].get("healthcheck", {}).get("start_period", "0s")
    # parse "Ns" or "Nm"
    if sp.endswith("s"):
        seconds = int(sp[:-1])
    elif sp.endswith("m"):
        seconds = int(sp[:-1]) * 60
    else:
        seconds = int(sp)
    assert seconds >= 300, f"start_period too short for weight load: {sp}"


def test_holo3_healthcheck_uses_models_endpoint(compose):
    test = " ".join(str(t) for t in
                    compose["services"]["holo3"].get("healthcheck", {}).get("test", []))
    assert "/v1/models" in test or "/models" in test


def test_holo3_expose_internal_not_host(compose):
    """Don't bind :8000 to the host by default — operators who want
    direct access add a `ports:` line."""
    assert "8000" in " ".join(str(p) for p in
                              compose["services"]["holo3"].get("expose", []))
    assert not compose["services"]["holo3"].get("ports", [])


# ────────── driver env wiring ──────────

def test_driver_holo3_endpoint_defaults_to_sidecar(compose):
    drv_env = compose["services"]["driver"]["environment"]
    holo3_ep = drv_env.get("HOLO3_ENDPOINT", "")
    assert "holo3:8000" in holo3_ep
    assert "/v1" in holo3_ep


def test_driver_holo3_endpoint_overridable(compose):
    drv_env = compose["services"]["driver"]["environment"]
    holo3_ep = drv_env.get("HOLO3_ENDPOINT", "")
    assert "HOLO3_ENDPOINT" in holo3_ep  # ${HOLO3_ENDPOINT:-...}


def test_driver_holo3_model_defaults(compose):
    drv_env = compose["services"]["driver"]["environment"]
    assert "Hcompany/Holo3-35B-A3B" in drv_env.get("HOLO3_MODEL", "")


def test_driver_holo3_api_key_wired(compose):
    drv_env = compose["services"]["driver"]["environment"]
    assert "HOLO3_API_KEY" in drv_env


def test_driver_anthropic_api_key_optional_in_compose(compose):
    """The pre-PR ANTHROPIC_API_KEY was `:?required`. Now it defaults
    to empty string so the driver can boot in VISION_BACKEND=local-*
    mode without an Anthropic key set."""
    drv_env = compose["services"]["driver"]["environment"]
    ak = drv_env.get("ANTHROPIC_API_KEY", "")
    assert ":?" not in ak, "ANTHROPIC_API_KEY shouldn't be required at compose level"


def test_driver_does_not_depend_on_holo3(compose):
    """The driver MUST not depends_on holo3 — would break the default
    `docker compose up` (no GPU host) AND the VISION_BACKEND=anthropic
    path (no need for vLLM at all)."""
    deps = compose["services"]["driver"].get("depends_on", {})
    if isinstance(deps, list):
        assert "holo3" not in deps
    else:
        assert "holo3" not in deps


# ────────── ANTHROPIC_API_KEY precondition relaxation ──────────

@pytest.fixture
def app(monkeypatch, tmp_path):
    """Fresh main per test for the precondition checks."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DESTINY_API_TOKEN", raising=False)
    monkeypatch.delenv("DESTINY_TASK_RATE_LIMIT", raising=False)
    monkeypatch.delenv("VISION_BACKEND", raising=False)
    sys.modules.pop("main", None)
    sys.path.insert(0, "/tmp/destiny-computer/driver/src")
    import main as M  # type: ignore[import-not-found]
    monkeypatch.setattr(M, "_desktop_reachable", lambda: True)
    monkeypatch.setattr(M.L, "today_spend", lambda _: 0.0)
    monkeypatch.setattr(M, "_run_task_blocking", lambda *a, **k: None)
    return M


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app.app)


def test_submit_task_500_when_anthropic_backend_and_no_key(monkeypatch, app, client):
    """Default VISION_BACKEND=anthropic + no ANTHROPIC_API_KEY → 500.
    This is the v0.2 contract; PR preserves it for the default path."""
    monkeypatch.setattr(app, "VISION_BACKEND", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post("/api/task", json={"goal": "go"})
    assert r.status_code == 500
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_submit_task_202_when_local_backend_and_no_key(monkeypatch, app, client):
    """VISION_BACKEND=local-uitars + no ANTHROPIC_API_KEY → 202.
    The precondition only fires for the anthropic backend now."""
    monkeypatch.setattr(app, "VISION_BACKEND", "local-uitars")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post("/api/task", json={"goal": "go"})
    assert r.status_code == 202


def test_submit_task_202_when_local_backend_and_key_present(monkeypatch, app, client):
    """VISION_BACKEND=local-uitars + ANTHROPIC_API_KEY set (operator
    hedging both) → 202. No regression on the multi-backend host."""
    monkeypatch.setattr(app, "VISION_BACKEND", "local-uitars")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    r = client.post("/api/task", json={"goal": "go"})
    assert r.status_code == 202


# ────────── docs ──────────

def test_env_example_documents_local_vision_profile():
    raw = ENV_EXAMPLE.read_text()
    # The env var name + the profile name + the issue number
    assert "VISION_BACKEND" in raw
    assert "local-vision" in raw or "local-uitars" in raw
    assert "issue #7" in raw.lower() or "#7" in raw


def test_tracking_md_split_d5_into_d5a_and_d5b():
    """D5 was a single issue; we close D5a (sidecar + precondition)
    and file D5b (loop.py routing wiring) as a v0.3 follow-up."""
    raw = TRACKING_PATH.read_text()
    assert "D5a" in raw or "D5 — (CLOSED" in raw
    # Mention v0.3 follow-up in some form
    assert "D5b" in raw or "v0.3" in raw or "follow-up" in raw.lower()


def test_readme_documents_local_vision_profile():
    raw = README_PATH.read_text()
    # Mentions the profile + the env var + the GPU + the model
    assert "local-vision" in raw or "VISION_BACKEND" in raw
    assert "Holo3" in raw or "vLLM" in raw


# ────────── scan-before-push ──────────

def test_no_secrets_in_compose():
    raw = COMPOSE_PATH.read_text()
    forbidden = [
        r"sk-ant-[a-zA-Z0-9_-]{8,}",
        r"shpat_[a-zA-Z0-9]{20,}",
        r"\bsk-1234\b",
        r"10\.179\.1\.",
        r"100\.75\.",
    ]
    for pat in forbidden:
        assert not re.search(pat, raw), f"compose leaks pattern: {pat}"


def test_no_competitor_model_ids_in_holo3_command(compose):
    cmd = " ".join(str(c) for c in compose["services"]["holo3"]["command"])
    assert "Hcompany/Holo3-35B-A3B" in cmd
    assert "claude-" not in cmd.lower()
    assert "gpt-" not in cmd.lower()
    assert "deepseek" not in cmd.lower()
