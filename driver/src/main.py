"""Destiny Computer driver (v0.2 — real Anthropic Computer Use loop).

The persistent-desktop half of the Destiny stack. Pairs with the chat at
https://github.com/karany97/nandai-atelier — atelier's right-side iframe
embeds the KasmVNC desktop this driver controls.

What v0.2 ships (vs v0.1 stub):

- /api/task is no longer a placeholder. It accepts a goal, spawns the
  Anthropic Computer Use loop in a background task, and returns a task_id
  the chat can poll or stream.
- /api/task/{id} returns the live transcript (step count, status, cost,
  steps[]).
- /api/task/{id}/stream is a Server-Sent-Events stream of step records,
  letting the chat narrate "step 3: clicked 'New Tab'" in real time.
- /api/budget reads the actual cost ledger written by loop.py instead
  of a v0.1-style empty stub.

Routes:
  GET  /health               → service liveness + desktop reachability
  GET  /screenshot           → PNG of current desktop (legacy v0.1 endpoint)
  POST /api/task             → submit goal, spawn loop, return task_id
  GET  /api/task/{id}        → transcript snapshot
  GET  /api/task/{id}/stream → SSE stream of step records
  GET  /api/budget           → today's cumulative spend
  GET  /api/tasks            → recent task list (last 20)

Failure modes are explicit (status codes follow conventional REST):
  503 — desktop container not reachable (caller should retry / show error)
  400 — bad payload (missing goal, etc.)
  402 — budget cap exceeded for the day (caller surfaces to user)
  404 — task_id unknown
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

# Allow running either as a package (`python -m destiny_driver.main`) or
# as a script inside the container (`python main.py`). The container puts
# `src/` on PYTHONPATH so `from desktop import ...` works.
try:
    from . import desktop as D
    from . import loop as L
except ImportError:
    import desktop as D  # type: ignore[no-redef]
    import loop as L  # type: ignore[no-redef]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("destiny-driver")

# ─── Config from env ───────────────────────────────────────────────────────
DESKTOP_CONTAINER = os.environ.get("DESKTOP_CONTAINER", "destiny-desktop")
MAX_STEPS = int(os.environ.get("MAX_STEPS_PER_TASK", "30"))
MAX_USD = float(os.environ.get("MAX_USD_PER_DAY", "1.00"))
VISION_BACKEND = os.environ.get("VISION_BACKEND", "anthropic")

STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
LEDGER_FILE = STATE_DIR / "cost-ledger.jsonl"
TASKS_DIR = STATE_DIR / "tasks"
TASKS_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_TASKS_FILE = STATE_DIR / "tasks.jsonl"  # v0.1 compatibility — keep writing the index

app = FastAPI(
    title="destiny-computer-driver",
    version="0.2.0",
    description="Autonomous desktop driver using Anthropic Computer Use. "
                "Paired with nandai-atelier.",
)


# ─── In-process step bus (for SSE streaming) ───────────────────────────────
#
# When a background task posts a step record to its asyncio.Queue, the
# /api/task/{id}/stream endpoint pops it and forwards it to the SSE
# connection. We keep one queue per task_id and clean up when the loop
# finishes. Cap memory by closing the queue after the task ends.
_STEP_BUSES: Dict[str, "asyncio.Queue[Optional[dict]]"] = {}


def _bus_for(task_id: str) -> "asyncio.Queue[Optional[dict]]":
    if task_id not in _STEP_BUSES:
        _STEP_BUSES[task_id] = asyncio.Queue()
    return _STEP_BUSES[task_id]


# ─── Helpers ──────────────────────────────────────────────────────────────


def _docker_available() -> bool:
    import shutil
    return shutil.which("docker") is not None


def _desktop_reachable() -> bool:
    if not _docker_available():
        return False
    try:
        import subprocess
        r = subprocess.run(
            ["docker", "exec", DESKTOP_CONTAINER, "true"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _transcript_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def _index_task(task_id: str, goal: str) -> None:
    """Append to legacy tasks.jsonl so /api/tasks can list without scanning."""
    LEGACY_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "id": task_id,
        "goal": goal,
        "submitted_at": time.time(),
    }
    with LEGACY_TASKS_FILE.open("a") as f:
        f.write(json.dumps(row) + "\n")


# ─── Endpoints ─────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> Dict[str, object]:
    reachable = _desktop_reachable()
    return {
        "ok": reachable,
        "service": "destiny-computer-driver",
        "version": "0.2.0",
        "vision_backend": VISION_BACKEND,
        "model": L.MODEL,
        "desktop_container": DESKTOP_CONTAINER,
        "desktop_reachable": reachable,
        "max_steps_per_task": MAX_STEPS,
        "max_usd_per_day": MAX_USD,
        "today_spend_usd": round(L.today_spend(LEDGER_FILE), 4),
    }


@app.get("/screenshot")
def screenshot() -> Response:
    """Take a PNG of the current desktop. Mostly for the chat's preview pane."""
    if not _desktop_reachable():
        raise HTTPException(503, "desktop container unreachable")
    try:
        png = D.screenshot()
    except D.DesktopError as e:
        raise HTTPException(500, f"screenshot failed: {e}") from e
    return Response(content=png, media_type="image/png")


# ─── Task API ─────────────────────────────────────────────────────────────


class TaskRequest(BaseModel):
    goal: str = Field(..., min_length=2, max_length=2000,
                      description="Natural-language goal for the AI to achieve.")
    context: Optional[str] = Field(
        None, max_length=4000,
        description="Optional conversation context to anchor the run.",
    )
    max_steps: Optional[int] = Field(None, ge=1, le=200,
                                     description="Override the env default.")


def _run_task_blocking(task_id: str, goal: str, max_steps: int) -> None:
    """Sync entry point — invoked from a background thread by FastAPI."""
    bus = _bus_for(task_id)

    def progress(transcript: L.TaskTranscript, step: L.StepRecord) -> None:
        payload = {
            "task_id": transcript.task_id,
            "step": step.step,
            "action": step.action,
            "result": step.desktop_result,
            "text": step.text_from_model,
            "cost_usd": step.cost_usd,
            "total_cost_usd": transcript.total_cost_usd,
            "status": transcript.status,
        }
        try:
            # Cross-thread .put — use call_soon_threadsafe via the loop
            bus.put_nowait(payload)
        except Exception:
            log.exception("failed to push step to bus")

    try:
        L.run_task(
            goal=goal,
            task_id=task_id,
            transcript_file=_transcript_path(task_id),
            ledger_file=LEDGER_FILE,
            max_steps=max_steps,
            max_usd_per_day=MAX_USD,
            progress_cb=progress,
        )
    finally:
        # Sentinel: signals SSE stream end.
        try:
            bus.put_nowait(None)
        except Exception:
            pass


@app.post("/api/task")
def submit_task(req: TaskRequest, bg: BackgroundTasks) -> JSONResponse:
    """Accept a goal, kick off the loop in the background, return task_id."""
    if not _desktop_reachable():
        raise HTTPException(503, "desktop container unreachable")

    # Pre-flight budget check — refuse early if already over cap so we
    # don't dispatch a task that'll bail at step 0.
    if L.today_spend(LEDGER_FILE) >= MAX_USD:
        raise HTTPException(
            402,
            f"daily cap ${MAX_USD:.2f} already reached; try tomorrow or raise MAX_USD_PER_DAY",
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(
            500,
            "ANTHROPIC_API_KEY not set — driver can't reach the model",
        )

    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    max_steps = req.max_steps or MAX_STEPS

    _index_task(task_id, req.goal)
    bg.add_task(_run_task_blocking, task_id, req.goal, max_steps)

    return JSONResponse({
        "task_id": task_id,
        "status": "running",
        "max_steps": max_steps,
        "max_usd_per_day": MAX_USD,
        "model": L.MODEL,
        "stream_url": f"/api/task/{task_id}/stream",
        "transcript_url": f"/api/task/{task_id}",
    }, status_code=202)


@app.get("/api/task/{task_id}")
def get_task(task_id: str) -> JSONResponse:
    p = _transcript_path(task_id)
    if not p.exists():
        # Maybe still warming up — return a placeholder so the chat can show
        # "started" instead of 404.
        return JSONResponse({
            "task_id": task_id,
            "status": "starting",
            "step_count": 0,
            "total_cost_usd": 0.0,
        })
    try:
        return JSONResponse(json.loads(p.read_text()))
    except Exception as e:
        raise HTTPException(500, f"transcript read error: {e}") from e


@app.get("/api/task/{task_id}/stream")
async def stream_task(task_id: str) -> StreamingResponse:
    """Server-Sent-Events stream of step records.

    The chat's UI subscribes to this and renders "Step 3 (click 612,431):
    opened New Tab". Closes when the loop posts a sentinel (None).
    """
    bus = _bus_for(task_id)

    async def gen() -> AsyncIterator[bytes]:
        # Keep-alive comment so proxies don't kill an idle connection
        yield b": stream open\n\n"
        try:
            while True:
                # Coarse 30s timeout — emit a keep-alive ping instead of stalling
                try:
                    item = await asyncio.wait_for(bus.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    continue
                if item is None:
                    yield b"event: end\ndata: {}\n\n"
                    return
                yield f"event: step\ndata: {json.dumps(item)}\n\n".encode("utf-8")
        finally:
            # Don't leak buses after stream closes; the loop has already finished
            # by the time we hit None, so safe to forget.
            _STEP_BUSES.pop(task_id, None)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


@app.get("/api/tasks")
def list_tasks(limit: int = 20) -> JSONResponse:
    """Recent submitted tasks. Reads tasks.jsonl, then merges live transcripts."""
    rows: List[dict] = []
    if LEGACY_TASKS_FILE.exists():
        all_rows = LEGACY_TASKS_FILE.read_text().splitlines()
        for line in all_rows[-limit:]:
            try:
                r = json.loads(line)
                tp = _transcript_path(r["id"])
                if tp.exists():
                    t = json.loads(tp.read_text())
                    r.update({
                        "status": t.get("status"),
                        "step_count": t.get("step_count"),
                        "total_cost_usd": t.get("total_cost_usd"),
                    })
                else:
                    r["status"] = "submitted"
                rows.append(r)
            except Exception:
                continue
    return JSONResponse({"tasks": rows})


@app.get("/api/budget")
def budget() -> JSONResponse:
    """Today's cumulative spend, breakdown per task, remaining budget."""
    today = time.strftime("%Y-%m-%d")
    total_usd = 0.0
    per_task: defaultdict = defaultdict(float)
    if LEDGER_FILE.exists():
        for line in LEDGER_FILE.read_text().splitlines():
            try:
                r = json.loads(line)
                if r.get("date") == today:
                    total_usd += float(r.get("usd", 0))
                    per_task[r.get("task_id", "?")] += float(r.get("usd", 0))
            except Exception:
                continue
    return JSONResponse({
        "date": today,
        "tasks_run": len(per_task),
        "total_usd": round(total_usd, 4),
        "cap_usd": MAX_USD,
        "remaining_usd": round(max(0.0, MAX_USD - total_usd), 4),
        "per_task_usd": {k: round(v, 4) for k, v in per_task.items()},
    })


if __name__ == "__main__":
    import uvicorn
    # HOST defaults to 0.0.0.0 (container deploy) but operators on a host-
    # deploy can lock it to 127.0.0.1 so the driver is only reachable via
    # an upstream proxy (recommended — /api/task spends real Anthropic
    # credits, you don't want it exposed on the LAN).
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8090)),
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
