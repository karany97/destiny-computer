"""Destiny Computer driver (v0.1 stub).

The full manus_computer-style autonomous loop lands in v0.2 — for v0.1
this driver exposes:

- GET  /health      → service liveness + desktop reachability check
- GET  /screenshot  → PNG of the current desktop (proves the docker exec
                      path to the desktop container works)
- POST /api/task    → accept a natural-language goal; v0.1 just records
                      it + returns a "not yet implemented" message. v0.2
                      will wire the screenshot→Anthropic-Computer-Use loop.
- GET  /api/budget  → daily cost ledger

The loop itself is a one-day-build per the destiny-computer architecture
scout — see docs/architecture.md for the spec. Shipping the scaffold
first so the Atelier integration + Docker compose + container security
can be tested in production while v0.2 is in flight.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("destiny-driver")

DESKTOP_CONTAINER = os.environ.get("DESKTOP_CONTAINER", "destiny-desktop")
MAX_STEPS = int(os.environ.get("MAX_STEPS_PER_TASK", "30"))
MAX_USD = float(os.environ.get("MAX_USD_PER_DAY", "1.00"))
VISION_BACKEND = os.environ.get("VISION_BACKEND", "anthropic")
STATE_DIR = Path("/state")
STATE_DIR.mkdir(parents=True, exist_ok=True)
TASKS_FILE = STATE_DIR / "tasks.jsonl"
LEDGER_FILE = STATE_DIR / "cost-ledger.jsonl"

app = FastAPI(title="destiny-computer-driver", version="0.1.0")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _desktop_reachable() -> bool:
    if not _docker_available():
        return False
    try:
        r = subprocess.run(
            ["docker", "exec", DESKTOP_CONTAINER, "true"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


@app.get("/health")
def health():
    return {
        "ok": _desktop_reachable(),
        "service": "destiny-computer-driver",
        "version": "0.1.0",
        "vision_backend": VISION_BACKEND,
        "desktop_container": DESKTOP_CONTAINER,
        "desktop_reachable": _desktop_reachable(),
        "max_steps_per_task": MAX_STEPS,
        "max_usd_per_day": MAX_USD,
    }


@app.get("/screenshot")
def screenshot():
    """Take a PNG of the current desktop via xwd|convert inside the container."""
    if not _desktop_reachable():
        raise HTTPException(503, "desktop container unreachable")
    try:
        # xwd → ImageMagick `convert` → stdout PNG. xwd requires DISPLAY.
        cmd = [
            "docker", "exec", DESKTOP_CONTAINER, "bash", "-c",
            "DISPLAY=:1 xwd -root -silent | convert xwd:- png:-",
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        if r.returncode != 0:
            log.error("xwd failed: %s", r.stderr.decode("utf-8", errors="replace")[:200])
            raise HTTPException(500, "screenshot failed")
        return Response(content=r.stdout, media_type="image/png")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "screenshot timed out")


class TaskRequest(BaseModel):
    goal: str
    """Natural-language description of what the AI should do on the desktop."""
    context: Optional[str] = None
    """Optional conversation context to feed the vision-action loop."""
    max_steps: Optional[int] = None


@app.post("/api/task")
def submit_task(req: TaskRequest):
    """v0.1 stub — records the task and returns a placeholder.
    v0.2 will run the actual screenshot→think→act loop."""
    task = {
        "id": f"task_{int(time.time() * 1000)}",
        "goal": req.goal,
        "context": req.context,
        "max_steps": req.max_steps or MAX_STEPS,
        "submitted_at": time.time(),
        "status": "queued",
        "result": None,
    }
    with open(TASKS_FILE, "a") as f:
        f.write(json.dumps(task) + "\n")
    return JSONResponse({
        "task_id": task["id"],
        "status": "queued",
        "message": (
            "Destiny Computer driver v0.1 received your task. The autonomous "
            "loop is targeted for v0.2 — for now, drive the desktop yourself "
            f"via the KasmVNC pane (your goal has been logged to {TASKS_FILE} "
            "for future replay)."
        ),
    })


@app.get("/api/budget")
def budget():
    """Today's cumulative Anthropic spend per task."""
    today = time.strftime("%Y-%m-%d")
    total_usd = 0.0
    task_count = 0
    if LEDGER_FILE.exists():
        for line in LEDGER_FILE.read_text().splitlines():
            try:
                row = json.loads(line)
                if row.get("date") == today:
                    total_usd += float(row.get("usd", 0))
                    task_count += 1
            except Exception:
                continue
    return {
        "date": today,
        "tasks_run": task_count,
        "total_usd": round(total_usd, 4),
        "cap_usd": MAX_USD,
        "remaining_usd": round(max(0.0, MAX_USD - total_usd), 4),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8090)))
