# Changelog

All notable changes to **destiny-computer**. Format: keep-a-changelog.
Each release lists what shipped + what got tested. Sibling project
to [atelier-os](https://github.com/karany97/atelier-os) (multi-session
fleet); destiny-computer is the single-desktop case.

## [v0.2.1] — 2026-05-16  (launch-sprint: 7 PRs, all TRACKING D-items closed)

The pre-launch hardening release. 7 PRs merged in 4 days. Tests went
from 20 → 424 (+2,020%). Every D-item in `TRACKING.md` (D1-D5b)
closed. README has zero "partial ship" caveats. New
`scripts/launch-smoke.py` walks the full operator workflow end-to-end.

### Added — feature shipments

- **D1 (PR #3) — Apply the 200-test gate.** v0.2 shipped with 20 tests
  covering only `desktop.py` xdotool calls. PR added 214 tests across
  `desktop.py` edges (80), `loop.py` dispatcher (44), cost ledger
  (29), `run_task` end-to-end with mocked Anthropic SDK (21), and
  FastAPI endpoint coverage with TestClient (40). All ≤0.5 s total.
  No new dependencies. Every test traces to a concrete bug class
  (drag missing endpoints, malformed ledger lines, model emitting
  float coords, budget remaining negative, transcript JSON schema).

- **D2 (PR #4) — Optional Bearer-token auth + first TRACKING.md.**
  `DESTINY_API_TOKEN` env var. Unset → no auth (full v0.2 back-compat).
  Set → every `/api/*` and `/screenshot` requires
  `Authorization: Bearer <token>`. `hmac.compare_digest`, header-
  only (anti-leak via access logs + Referer). `/health` stays open
  for Docker healthcheck. **Different env name from atelier-os
  (`ATELIER_API_TOKEN`)** so operators running both fleets on the
  same host can rotate them independently — regression-guarded.
  +26 tests in `test_api_auth.py`. Plus the repo's first TRACKING.md
  (paralleling atelier-os discipline — every known-broken behaviour
  has reproducer + acceptance + issue link).

- **D3 (PR #9) — Per-task rate limit on /api/task.** In-memory
  leaky-bucket keyed by Bearer token (when `DESTINY_API_TOKEN` set)
  or by client IP. Default `10/min` per client, configurable via
  `DESTINY_TASK_RATE_LIMIT=N/{sec,min,hour}`. Returns 429 + RFC 6585
  `Retry-After` header on overflow. **Rate-limit dep runs AFTER auth
  dep** so a 401 doesn't consume a slot (anti-DoS — otherwise an
  attacker could spam bad tokens to exhaust a legit user's bucket).
  Uses `threading.Lock` (not `asyncio.Lock`) because FastAPI
  BackgroundTasks runs sync handlers in a thread pool — regression-
  guarded. +25 tests in `test_rate_limit.py`.

- **D4 (PR #10) — Snapshot/restore API for the desktop container.**
  Four new REST endpoints all gated by `require_token`:
  `POST /api/desktop/snapshot` (201 + Snapshot record),
  `GET /api/desktop/snapshots` (most-recent first),
  `DELETE /api/desktop/snapshots/{id}` (409 for most-recent —
  safeguard), `POST /api/desktop/restore` (202, destructive swap).
  New `driver/src/snapshot.py` (230 LOC) wraps `docker commit` +
  `docker run` from snapshot tag. Restore preserves port bindings +
  volume mounts + operator envs via `docker inspect` replay; filters
  docker-auto envs (`PATH`, `HOSTNAME`, `HOME`) so the new image's
  defaults apply. `/home/operator` bind survives across restore
  (snapshot is about OS state, not user data). +49 tests in
  `test_snapshot.py`.

- **D5a (PR #12) — Optional Holo3 vLLM sidecar compose profile.**
  New `local-vision` profile spawns `vllm/vllm-openai:latest` serving
  Hcompany/Holo3-35B-A3B with safe 24 GB GPU defaults (`--gpu-memory-
  utilization 0.85`, `--max-model-len 16384`, `--enforce-eager` for
  RTX 3090 CUDA-graph stability). Weights cached in `holo3-models`
  named volume (~70 GB; survives `docker compose down`). Healthcheck
  `start_period: 600s` for first-boot weight load. NVIDIA GPU
  reservation refuses to start on non-GPU host (loud failure > silent
  CPU OOM). `ANTHROPIC_API_KEY` no longer hard-required at compose
  level (was `:?required`); the driver's `submit_task` precondition
  only fires when `VISION_BACKEND=anthropic`. Driver does NOT
  depends_on holo3 — preserves default `up` on non-GPU hosts.
  +26 tests in `test_holo3_profile.py`.

- **D5b (PR #13) — Vision-backend abstraction + Holo3 routing in
  loop.py.** New `driver/src/vision.py` (430 LOC) ships a
  `VisionBackend` ABC with two implementations:
  - `AnthropicVisionBackend` wraps the v0.2 Anthropic Computer Use
    logic verbatim. The 21 pre-existing `test_loop_run_task` tests
    survive with only a 2-line fixture change (patch target moves
    from `loop.Anthropic` to `vision.Anthropic`).
  - `Holo3VisionBackend` POSTs to `${HOLO3_ENDPOINT}/chat/completions`
    in OpenAI Chat Completions shape. Parses Holo3's structured
    response (`Action:` / `Coordinate:` / `Text:` / `Direction:` /
    `Reasoning:`) into the same action dict shape `_dispatch_action`
    already understands. `cost_usd = 0.0` for every local call
    (regression-guarded — no accidentally billing operators for
    self-hosted inference). Tokens still recorded in the ledger so
    operators see "5 Holo3 calls today, $0.00" instead of nothing.
  `get_backend()` factory keys on `$VISION_BACKEND`; accepts
  `anthropic` / `local-uitars` / `holo3` (alias) / case-insensitive.
  Unknown → `ValueError` with the supported list. `loop.py::run_task`
  refactored (~150 LOC delta) to be backend-agnostic. +46 tests in
  `test_vision.py`.

- **`scripts/launch-smoke.py` (PR #14)** — operator install verifier
  (sibling of atelier-os PR #17). stdlib-only (no `pip install`).
  Walks `/health` → `/screenshot` → `/api/budget` → `/api/tasks` →
  snapshot lifecycle against a running driver. Optional `POST /api/
  task` behind `SMOKE_RUN_TASK=1` (defaults off because it costs
  $0.05-0.40 on Anthropic). Reports PASS/FAIL with the actual
  response excerpt. Exit code 0/1. Bearer-token aware via
  `DESTINY_API_TOKEN` env. +18 tests in `test_launch_smoke.py`.

### Fixed (subtle correctness work caught in the refactors)

- **`run_task` post-action screenshot failure is now non-fatal** —
  pre-refactor, a transient `xwd` hiccup on the post-action
  screenshot would abort the loop with `desktop_error`. Now reuses
  the bootstrap shot and continues. Operators reported sporadic
  1-in-50 aborts on long tasks; this fixes them. (PR #13)
- **`run_task` bootstrap screenshot is now explicit** — was implicit
  via `_take_screenshot_block` inside the initial messages list.
  Now `D.screenshot()` is called explicitly with a `desktop_error`
  path. Cleaner failure mode if the desktop isn't reachable at task
  start. (PR #13)
- **`get_task` short-circuits auth BEFORE the starting-placeholder**
  when token is set — pre-fix, an unauth'd caller could enumerate
  task ids by watching for starting-vs-401 response shape. Regression-
  guarded by `test_token_required_task_get_401`. (PR #4)
- **`delete_snapshot` of the most-recent now 409 in module logic
  (not just the endpoint)** — future v0.3 CLI or queue surfaces
  can't bypass the safeguard accidentally. Regression-guarded by
  `test_delete_snapshot_refuses_most_recent`. (PR #10)
- **`enforce_rate_limit` runs AFTER `require_token`** so a 401
  doesn't consume a slot. Without this, an attacker who knew a
  legitimate user's username pattern could spam `Bearer wrong-
  ${user}` to exhaust their bucket. Regression-guarded by
  `test_rate_limit_check_runs_after_auth`. (PR #9)

### Discipline

- **TRACKING.md established** + every entry D1-D5b closed with full
  resolution writeup. D6 (multi-user) stays open as a sign-post that
  points readers to the sibling atelier-os repo (multi-user is
  explicitly out of scope for destiny-computer).
- README badge: 20/20 → 424/424. New sections: "Verify your install"
  (smoke harness), "Local vision" (`local-vision` compose profile,
  end-to-end routing).
- Zero open actionable issues at release. 7 PRs merged with zero
  broken builds.

### Image

- `destiny-driver:0.2.1` — pure-Python deltas; no Dockerfile change.
- `vllm/vllm-openai:latest` pulled by the `local-vision` profile
  on operator opt-in (not bundled into the default `up`).

## [v0.2.0] — 2026-05-16  (initial public release — real Anthropic Computer Use loop)

First public release on GitHub. The v0.1 stub `/api/task` was a
placeholder ("not yet implemented"); v0.2 ships the actual autonomous
screenshot → think → act → repeat loop against Anthropic Computer Use.

### Added

- **Anthropic Computer Use loop** (`driver/src/loop.py`, ~320 LOC).
  Uses `computer_20251124` tool schema, `claude-sonnet-4-5` default
  (Opus override per env), beta header `computer-use-2025-01-24`.
  Per-step token tracking from the API response. Cost ledger
  (`cost-ledger.jsonl`) records every step. Status codes are explicit:
  `completed` / `budget_exceeded_steps` / `budget_exceeded_usd` /
  `desktop_error` / `api_error`.
- **Desktop action executor** (`driver/src/desktop.py`, ~255 LOC).
  Every primitive maps to a `docker exec destiny-desktop bash -c
  "xdotool ..."` call. All 11 verbs: mouse_move / left_click /
  right_click / middle_click / double_click / triple_click /
  left_mouse_down / left_mouse_up / left_click_drag / type_text /
  key_press / hold_key / scroll / wait / cursor_position / screenshot.
  Failure mode: `DesktopError` caught by `loop.py` and reported back
  to the model as `tool_result` with `is_error=true`.
- **FastAPI driver** (`driver/src/main.py`, rewritten ~280 LOC).
  Endpoints: `POST /api/task` → spawn loop in background, returns
  202 + `task_id`. `GET /api/task/{id}` → transcript snapshot.
  `GET /api/task/{id}/stream` → Server-Sent-Events stream of step
  records. `GET /api/tasks` → recent task list. `GET /api/budget` →
  today's spend + per-task breakdown. `GET /screenshot` (legacy v0.1).
  Pre-flight: refuses 402 if daily cap reached before spawning loop.
- **20 unit tests** (`test_desktop.py`) covering xdotool command
  assembly, output parsing, error mapping, key aliases, wait clamping,
  cursor position parsing. CI-friendly (no docker / X server needed).
- **Live-driver compatibility** — `loop.py` import fallback for both
  packaged (`from . import desktop`) and script-style (`import
  desktop`) modes, so the driver runs equally as the docker image's
  `uvicorn main:app` AND as a systemd service against an existing
  KasmVNC container on a host with `PYTHONPATH=src`.
- **README**: real screenshot from the driver's own `/screenshot`
  endpoint (not a stock photo). Companion-repo links to
  [Destiny Atelier](https://github.com/karany97/nandai-atelier)
  (chat surface) and [atelier-os](https://github.com/karany97/atelier-os)
  (multi-session sibling).
- **Compose**: KasmVNC desktop + Anthropic-Computer-Use driver in
  one `docker compose up -d`. `--cap-drop=ALL --cap-add=SYS_ADMIN`
  (Chrome inside container needs SYS_ADMIN; threat model: AI can
  break the desktop but can't escape the container).

### Pricing reality (per the May 5 "Computer Use is 45× more expensive" HN post)

- KasmVNC self-hosted (your spare PC) = $0 hardware
- Sonnet 4.5 default, 30 steps avg = $0.05–$0.40 per task
- Opus 4.5 override (per task) = $0.25–$2.00
- Per-task ledger + daily cap (`MAX_USD_PER_DAY`) refuses to start a
  new task if today's spend is already over. No surprise bills.

### Image

- `destiny-driver:0.2.0` — Python 3.12 slim + anthropic SDK.

## [v0.1] — 2026-05-16  (initial scaffold)

- `compose/docker-compose.yml` — KasmVNC desktop + placeholder driver
- `driver/src/main.py` — stub `/api/task` ("not yet implemented")
- `driver/src/desktop.py` — placeholder
- README scaffold
- MIT license

[v0.2.1]: https://github.com/karany97/destiny-computer/releases/tag/v0.2.1
[v0.2.0]: https://github.com/karany97/destiny-computer/releases/tag/v0.2.0
[v0.1]: https://github.com/karany97/destiny-computer/releases/tag/v0.1
