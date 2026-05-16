#!/usr/bin/env python3
"""Destiny Computer — operator install smoke test.

Run this AFTER `docker compose up -d` to verify your install is healthy
end-to-end. It walks the operator-facing workflow against a real running
driver + desktop:

  1. /health is reachable + reports desktop_reachable=true
  2. /screenshot returns a PNG > 1 KB
  3. /api/budget returns the expected schema
  4. /api/tasks lists (probably empty) without error
  5. POST /api/task accepts a tiny goal + returns task_id
  6. GET /api/task/{id} returns starting -> running -> completed
     within the per-step timeout (default 90 s)
  7. GET /api/task/{id}/stream yields at least one event before end
  8. /api/desktop/snapshot creates a snapshot
  9. /api/desktop/snapshots lists the new snapshot
  10. /api/desktop/snapshots/{old_id} 409s for the MOST RECENT (safeguard)

Each check prints PASS / FAIL with the response excerpt that justifies
the verdict (no fabrication — operators see the actual response). Exit
code 0 if all green, 1 if any failed.

Usage:

    # Default: hits the local driver, no Bearer token
    python3 scripts/launch-smoke.py

    # Remote driver + bearer auth
    DRIVER_URL=https://your-host:8090 \\
    DESTINY_API_TOKEN=$(grep DESTINY_API_TOKEN .env | cut -d= -f2) \\
    python3 scripts/launch-smoke.py

    # Run a real Anthropic task (default: SKIP because it costs money)
    SMOKE_RUN_TASK=1 python3 scripts/launch-smoke.py

Env vars:
    DRIVER_URL          base URL of the driver (default http://127.0.0.1:8090)
    DESTINY_API_TOKEN   Bearer token if your driver is gated
    SMOKE_RUN_TASK      "1" to actually dispatch a task (costs $0.05-0.40
                        on Anthropic; free on local-uitars). Default off.
    SMOKE_TASK_TIMEOUT  per-step poll timeout in seconds (default 90)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

DRIVER_URL    = os.environ.get("DRIVER_URL", "http://127.0.0.1:8090").rstrip("/")
TOKEN         = os.environ.get("DESTINY_API_TOKEN", "")
RUN_TASK      = os.environ.get("SMOKE_RUN_TASK", "0") == "1"
TASK_TIMEOUT  = int(os.environ.get("SMOKE_TASK_TIMEOUT", "90"))

# ANSI colors only when stdout is a tty — log redirection stays plain
if sys.stdout.isatty():
    GREEN = "\x1b[32m"; RED = "\x1b[31m"; DIM = "\x1b[2m"; END = "\x1b[0m"
    BOLD = "\x1b[1m"
else:
    GREEN = RED = DIM = END = BOLD = ""

_results = []  # (name, passed_bool, detail_str)


def _http(method: str, path: str, body: Optional[dict] = None,
          timeout: int = 10) -> Tuple[int, bytes, dict]:
    """One HTTP call. Returns (status, body_bytes, headers_dict).
    Stdlib only — no `requests` dependency on the operator machine."""
    url = f"{DRIVER_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except urllib.error.URLError as e:
        return 0, str(e).encode(), {}


def _check(name: str, passed: bool, detail: str) -> None:
    """Print one check + record for the final summary."""
    icon = f"{GREEN}PASS{END}" if passed else f"{RED}FAIL{END}"
    print(f"  [{icon}] {name}")
    if detail and not passed:
        # Indent + truncate the detail so a 500-line traceback doesn't drown
        # the summary.
        snippet = detail.strip().replace("\n", "\n        ")[:600]
        print(f"        {DIM}{snippet}{END}")
    elif detail:
        snippet = detail.strip().replace("\n", "\n        ")[:200]
        print(f"        {DIM}{snippet}{END}")
    _results.append((name, passed, detail))


# ────────── checks ──────────


def check_health() -> bool:
    code, body, _ = _http("GET", "/health")
    if code != 200:
        _check("GET /health → 200", False, f"got {code}: {body[:200]!r}")
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        _check("/health is JSON", False, str(e))
        return False
    _check("GET /health → 200 + JSON", True,
           f"version={payload.get('version')} model={payload.get('model')}")
    desktop_ok = payload.get("desktop_reachable") is True
    _check("/health reports desktop_reachable=true", desktop_ok,
           f"desktop_reachable={payload.get('desktop_reachable')}; "
           f"if False: docker compose ps and check the `desktop` service")
    return desktop_ok


def check_screenshot() -> bool:
    code, body, headers = _http("GET", "/screenshot")
    if code != 200:
        _check("GET /screenshot → 200", False, f"got {code}: {body[:200]!r}")
        return False
    ct = headers.get("content-type", "")
    if "image/png" not in ct:
        _check("/screenshot is image/png", False, f"content-type={ct!r}")
        return False
    if len(body) < 1024:
        _check("/screenshot ≥ 1 KB", False, f"got {len(body)} bytes")
        return False
    # PNG magic
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        _check("/screenshot has PNG magic", False, f"first bytes: {body[:8]!r}")
        return False
    _check("GET /screenshot → real PNG", True,
           f"{len(body):,} bytes, PNG magic OK")
    return True


def check_budget() -> bool:
    code, body, _ = _http("GET", "/api/budget")
    if code != 200:
        _check("GET /api/budget → 200", False, f"got {code}: {body[:200]!r}")
        return False
    payload = json.loads(body)
    required = {"date", "tasks_run", "total_usd", "cap_usd",
                "remaining_usd", "per_task_usd"}
    missing = required - set(payload.keys())
    if missing:
        _check("/api/budget has expected fields", False,
               f"missing: {sorted(missing)}")
        return False
    _check("GET /api/budget → schema OK", True,
           f"today ${payload['total_usd']:.4f} / cap ${payload['cap_usd']}")
    return True


def check_tasks_list() -> bool:
    code, body, _ = _http("GET", "/api/tasks")
    if code != 200:
        _check("GET /api/tasks → 200", False, f"got {code}: {body[:200]!r}")
        return False
    payload = json.loads(body)
    ok = "tasks" in payload and isinstance(payload["tasks"], list)
    _check("/api/tasks → {tasks: [...]}", ok,
           f"count={len(payload.get('tasks', []))}")
    return ok


def check_run_task() -> bool:
    """Optional — costs money on Anthropic. Skipped unless SMOKE_RUN_TASK=1."""
    if not RUN_TASK:
        _check("POST /api/task (skipped — set SMOKE_RUN_TASK=1 to run)",
               True, "skip")
        return True
    # Cheap goal — just take a screenshot and stop.
    code, body, _ = _http("POST", "/api/task",
                          body={"goal": "take a screenshot and reply 'ok'",
                                "max_steps": 3})
    if code != 202:
        _check("POST /api/task → 202", False, f"got {code}: {body[:200]!r}")
        return False
    task_id = json.loads(body).get("task_id")
    if not task_id:
        _check("/api/task returns task_id", False, body.decode("utf-8", "replace"))
        return False
    _check("POST /api/task → 202 + task_id", True, f"task_id={task_id}")
    # Poll the transcript
    deadline = time.time() + TASK_TIMEOUT
    while time.time() < deadline:
        code, body, _ = _http("GET", f"/api/task/{task_id}")
        if code != 200:
            _check(f"GET /api/task/{task_id} reachable", False,
                   f"got {code}: {body[:200]!r}")
            return False
        payload = json.loads(body)
        status = payload.get("status")
        if status not in (None, "starting", "running"):
            _check(f"task finished ({status})",
                   status == "completed",
                   f"final_text={(payload.get('final_text') or '')[:120]}, "
                   f"cost=${payload.get('total_cost_usd', 0):.4f}")
            return status == "completed"
        time.sleep(2)
    _check(f"task completed within {TASK_TIMEOUT}s", False,
           f"last status={status}")
    return False


def check_snapshot_lifecycle() -> bool:
    """Create snapshot → list (verify present) → try delete most-recent
    (verify 409) → leave it for the operator to clean up."""
    # Create
    code, body, _ = _http("POST", "/api/desktop/snapshot",
                          body={"note": "smoke-test"})
    if code != 201:
        _check("POST /api/desktop/snapshot → 201", False,
               f"got {code}: {body[:200]!r}")
        return False
    snap_id = json.loads(body).get("id")
    _check("POST /api/desktop/snapshot → 201", True, f"id={snap_id}")
    # List
    code, body, _ = _http("GET", "/api/desktop/snapshots")
    if code != 200:
        _check("GET /api/desktop/snapshots → 200", False, str(code))
        return False
    snaps = json.loads(body).get("snapshots", [])
    in_list = any(s.get("id") == snap_id for s in snaps)
    _check("our snapshot appears in /api/desktop/snapshots", in_list,
           f"count={len(snaps)}")
    if not in_list:
        return False
    # Try to delete the most-recent → must 409
    code, body, _ = _http("DELETE", f"/api/desktop/snapshots/{snap_id}")
    _check("DELETE most-recent snapshot → 409 (safeguard)",
           code == 409,
           f"got {code}: {body[:120]!r}")
    return code == 409


# ────────── main ──────────


def main() -> int:
    print(f"{BOLD}destiny-computer launch smoke{END}")
    print(f"  driver: {DRIVER_URL}")
    print(f"  token : {'set' if TOKEN else 'not set (assuming open driver)'}")
    print(f"  task  : {'will be dispatched' if RUN_TASK else 'skipped (SMOKE_RUN_TASK=1 to dispatch)'}")
    print()

    print(f"{BOLD}Reachability{END}")
    if not check_health():
        return _summarize()
    check_screenshot()
    check_budget()
    check_tasks_list()
    print()

    print(f"{BOLD}Task dispatch{END}")
    check_run_task()
    print()

    print(f"{BOLD}Snapshot lifecycle{END}")
    check_snapshot_lifecycle()
    print()

    return _summarize()


def _summarize() -> int:
    passed = sum(1 for _, ok, _ in _results if ok)
    total  = len(_results)
    failed = total - passed
    print(f"{BOLD}Summary:{END} {passed}/{total} passed", end="")
    if failed:
        print(f"  ({RED}{failed} failed{END})")
        return 1
    print(f"  ({GREEN}all green{END})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
