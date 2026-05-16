# Destiny Computer — known limitations + open work

> Same discipline as the sibling atelier-os repo (TRACKING.md there):
> nothing ships to a karany97 public repo unless every known-broken
> behaviour has an entry here with a reproducer + acceptance criteria
> + GitHub issue link. Adding a "TODO" anywhere else without also
> landing it here is a process bug.

## How this file is laid out

Each entry has:
- **What** — the symptom in one sentence
- **Where** — file + line if applicable
- **Why deferred** — why we shipped without fixing
- **Acceptance** — what "fixed" looks like
- **Tracker** — GitHub issue link

## Closed in v0.2.x

### D1 — Apply the 200-test gate ✅

- **What**: v0.2 shipped with 20 unit tests (just `desktop.py` xdotool
  calls). Sibling atelier-os runs 356/356 — viewers comparing the
  two side-by-side were going to read 20/20 as "this driver is
  unverified".
- **Resolution**: PR #3 added 214 tests across `desktop.py` edges (80),
  `loop.py` dispatcher (44), cost ledger (29), `run_task` end-to-end
  with mocked Anthropic SDK (21), and FastAPI endpoint coverage with
  TestClient (40). Total now **260/260** (with the auth tests below).
- **Tracker**: closed via [#3](https://github.com/karany97/destiny-computer/pull/3).

### D2 — Optional Bearer-token auth on the driver API ✅

- **What**: Anyone who could reach `DRIVER_PORT` could `POST /api/task`
  and burn Anthropic credits, or `GET /screenshot` to spy on what the
  operator was doing. The README's existing recommendation (bind
  `HOST=127.0.0.1` and proxy externally) is a network-level kludge —
  code-level gating is the right defense.
- **Where**: `driver/src/main.py` now has `require_token()` FastAPI
  dependency mirroring the atelier-os PR #13 pattern.
- **Resolution**: Optional `DESTINY_API_TOKEN` env. Unset → no auth
  (full back-compat with the v0.2 default workflow). Set → every
  `/api/*` and `/screenshot` requires `Authorization: Bearer <token>`,
  returns 401 + `WWW-Authenticate: Bearer` on missing/wrong. Constant-
  time comparison via `hmac.compare_digest` defeats timing attacks.
  `/health` stays open (Docker healthcheck). Query-param tokens
  rejected (anti-leak via access logs + Referer). Different env name
  from atelier-os so operators running both fleets can rotate
  independently. +26 tests in `test_api_auth.py`.

## Currently-tracked items (v0.2.x)

### D3 — (CLOSED via PR #9, [#5](https://github.com/karany97/destiny-computer/issues/5)) ✅ Per-task rate limit on `/api/task`

- **What**: A misconfigured or compromised client could submit 100 tasks/second and burn the entire `MAX_USD_PER_DAY` before the budget check on task 2 has even completed.
- **Where**: `driver/src/main.py::submit_task` — `enforce_rate_limit` dependency
- **Resolution**: In-memory leaky-bucket keyed by Bearer token (when `DESTINY_API_TOKEN` is set) or by client IP. Default 10/min, configurable via `DESTINY_TASK_RATE_LIMIT` env in format `N/PERIOD` where PERIOD ∈ {sec, min, hour}. Invalid spec → safe default. Returns 429 + `Retry-After` header on overflow (RFC 6585). Bucket state lives in driver memory only — restart clears it (the `MAX_USD_PER_DAY` cap on disk is the cumulative-spend safety net). Rate-limit dep runs AFTER auth so a 401 doesn't consume a slot (regression-guarded — otherwise an attacker could spam bad tokens to exhaust the bucket).
- **Tests**: +25 (`test_rate_limit.py`) — `_parse_rate_limit` (6 incl. invalid/zero/case-insensitive fallbacks), `_client_id` (4 incl. token-preferred + IP fallback + missing client), `_rate_limit_check` (5 incl. under-cap, at-cap, refill-after-period, per-client isolation, Retry-After floor), endpoint (8 incl. 202/429 paths, Retry-After header, configured-spec in error message, default 10/min, per-token isolation, 401 doesn't consume slot, restart evaporates state), regression guards (2). Total now **285/285 in 1.88s**.

### D4 — (CLOSED via PR #10, [#6](https://github.com/karany97/destiny-computer/issues/6)) ✅ Snapshot/restore for the desktop container

- **What**: There was no API to freeze a "trained" desktop state (logged-in browser sessions, installed apps, customized configs) and roll back or branch from it. Operators were stuck with manual `docker commit` + `docker run`.
- **Where**: `driver/src/snapshot.py` (NEW, 230 LOC) + 4 endpoints in `driver/src/main.py`
- **Resolution**: 4 new endpoints, all gated by `require_token`:
  - `POST /api/desktop/snapshot {note}` → 201 + Snapshot record (id, tag, created_at, size_bytes, note). Calls `docker inspect` to verify the container is running, `docker commit destiny-desktop destiny-desktop-snapshot:<id>` to capture the image, `docker image inspect` for size. Refuses 503 if container unreachable, 500 if commit fails.
  - `GET /api/desktop/snapshots` → list most-recent first. Skips malformed metadata rows.
  - `DELETE /api/desktop/snapshots/{id}` → removes `docker image rm` + drops the metadata row. 404 if unknown id, **409 if it's the most-recent** (safeguard so operators can't accidentally orphan their only restore point — must create a fresher snapshot first to delete it).
  - `POST /api/desktop/restore {snapshot_id}` → 202. Captures current container's port bindings + volume mounts + env via `docker inspect`, stops + removes it, runs new from `snapshot.tag` with the same config. Bind-mounted `/home/operator` survives across restore (the snapshot captures the writable layer, not the volume). Filters out docker-auto envs (PATH/HOSTNAME/HOME) so the new image's defaults apply. 404 if snapshot unknown, 500 on docker run failure, 503 if current container unreachable. Caller polls `/health` to know when the new desktop is ready (~3-10s).
- **Tests**: +49 (`test_snapshot.py`) — `_docker` wrapper (4 incl. timeout/missing-CLI), `create_snapshot` (7 incl. happy path, container-not-running 503, container-missing, commit-fails, size-lookup-degrades-gracefully, metadata append, unique-id), `list_snapshots` (4 incl. sort + malformed skip + empty-line skip), `get_snapshot` (2), `delete_snapshot` (6 incl. most-recent guard, unknown-id, empty-store, image-rm invoked), `restore_snapshot` (6 incl. unknown-id, preserves ports + binds, filters docker-auto envs, missing current container, garbled inspect JSON, docker run failure), endpoint behaviour (15 incl. all 4 routes × all status codes), auth gating (4 — all routes require token when set). Total now **334/334 in 2.37s**.

### D5a — (CLOSED via PR #12, [#7](https://github.com/karany97/destiny-computer/issues/7)) ✅ Local-vision sidecar + ANTHROPIC_API_KEY precondition relaxation

- **What**: Pre-PR, the v0.2 README mentioned `VISION_BACKEND=local-uitars` as a future option but the operator path was non-existent: no compose sidecar, AND `ANTHROPIC_API_KEY` was a hard requirement at compose level (`:?required`) regardless of which backend the driver was meant to use.
- **Where**: `compose/docker-compose.yml` — new `local-vision` profile + `holo3-models` named volume + driver env wiring; `driver/src/main.py::submit_task` precondition check.
- **Resolution**: Ships the sidecar half of D5 (mirroring atelier-os S6 / PR #16). Opt-in `local-vision` compose profile spins up `vllm/vllm-openai:latest` serving Holo3-35B-A3B with safe defaults (24 GB GPU, `--max-model-len 16384`, `--gpu-memory-utilization 0.85`, `--enforce-eager`). Weights cached in `holo3-models` named volume (~70 GB; survives `docker compose down`). Healthcheck `start_period: 600s` accommodates first-boot weight load. NVIDIA GPU reservation refuses to start on non-GPU host (loud failure > silent CPU OOM). Driver auto-resolves `HOLO3_ENDPOINT=http://holo3:8000/v1` via compose-network DNS. `ANTHROPIC_API_KEY` now optional in compose (defaults to empty); main.py's submit_task precondition only fires when `VISION_BACKEND=anthropic`. Driver does NOT depends_on holo3 (preserves default `up` on non-GPU hosts + the anthropic path).
- **Tests**: +26 (`test_holo3_profile.py`) — compose structure (12 incl. profile gating, GPU reservation, --enforce-eager regression guard, named volume, healthcheck start_period), driver env wiring (6 incl. auto-discover sidecar + ANTHROPIC_API_KEY no-longer-required-in-compose + driver doesn't depends_on holo3), precondition relaxation (3 — local backend works without ANTHROPIC_API_KEY; anthropic backend still 500s without it), docs (3 incl. TRACKING split into D5a/D5b), scan-before-push (2). Total now **360 unit = 360**.

### D5b — (CLOSED via PR #13, [#11](https://github.com/karany97/destiny-computer/issues/11)) ✅ Wire VISION_BACKEND=local-uitars routing in loop.py

- **What**: The sidecar was up + reachable (D5a) but `loop.py` always called Anthropic Computer Use; `VISION_BACKEND=local-uitars` was effective for the precondition check only. The README D5a partial-ship caveat made this gap visible to operators.
- **Where**: NEW `driver/src/vision.py` (430 LOC) + `driver/src/loop.py::run_task` refactored to be backend-agnostic.
- **Resolution**: New `vision.py` ships a `VisionBackend` ABC with two implementations:
  - **`AnthropicVisionBackend`** wraps the v0.2 Anthropic Computer Use logic verbatim. The 21 pre-existing `test_loop_run_task.py` tests survive with only a 2-line fixture change (patch target moves from `loop.Anthropic` to `vision.Anthropic` + an explicit `VISION_BACKEND=anthropic` setenv so a stray env doesn't flip them onto Holo3).
  - **`Holo3VisionBackend`** POSTs to `${HOLO3_ENDPOINT}/chat/completions` in OpenAI Chat Completions shape with Holo3's recommended system prompt. Parses Holo3's structured response (`Action:`, `Coordinate:`, `Text:`, `Direction:`, `Reasoning:` lines) into the same action dict shape `_dispatch_action` already understands. Cost reported as $0.00 (local model, no per-token spend) but tokens still recorded for transparency. Endpoint-unreachable / non-JSON-response / missing-choices → `RuntimeError` caught by `run_task` as `api_error` (instead of leaking a urllib stacktrace).
  - **`get_backend()` factory** keys on `$VISION_BACKEND`; accepts `anthropic` / `local-uitars` / `holo3` (alias) / case-insensitive. Unknown values raise `ValueError` with the supported list — operator misconfigs fail fast.
- **`loop.py::run_task`**: ~150 LOC refactored. Now backend-agnostic: `get_backend()` → `initial_history(goal, png, screen_size)` → loop calling `backend.step(history, screen_size)` returning a normalized `StepResult` → `backend.append_action_result(...)` for the next turn. Backends own their conversation history shape entirely. Bootstrap screenshot moved here from the Anthropic-specific code path (was: implicit; now: explicit `D.screenshot()` with desktop_error handling). Post-action screenshot failure is non-fatal — reuses the bootstrap shot rather than aborting the loop (operators saw spurious aborts on transient xwd hiccups).
- **Tests**: +46 (`test_vision.py`) — StepResult shape (3), `get_backend` factory (8 incl. unset/explicit/alias/case-insensitive/env-var/unknown/error-includes-list/ABC-not-directly-instantiable), AnthropicVisionBackend (7 incl. system prompt has screen size, tool config dims, initial history shape, step finish/tool_use, append_action_result pairs tool_use_id correctly + marks is_error on failure), Holo3 response parser (13 incl. every action verb via parametrize + completion fallback + empty string + reasoning extract + escaped quotes + right_click/double_click/mouse_move/scroll), Holo3 backend HTTP (10 incl. defaults + env override + request shape + zero-cost guard + endpoint-unreachable → RuntimeError + non-JSON → RuntimeError + missing-choices → RuntimeError + append shape), regression guards (3 incl. module exports). Total now **406 unit = 406**.
- **README**: D5a "partial ship" caveat REMOVED. The Local Vision section now reads as a complete shipped feature.

### D6 — ([#8](https://github.com/karany97/destiny-computer/issues/8)) Multi-user `/api/task` (different operator personas) — OUT OF SCOPE

- **What**: All tasks run as the same operator persona. No notion of
  "this is Janvi's task, that one is Devika's".
- **Where**: there's no user model in `main.py`
- **Why deferred**: v0.2 ships single-user. Multi-user is what
  atelier-os exists to solve (one container per teammate). If you
  need that, use atelier-os, not destiny-computer.
- **Acceptance**: explicitly out of scope for this repo — destiny-
  computer stays single-desktop. This entry exists only to point
  newcomers at the sibling repo. Issue stays open as a sign-post.

## Items we WILL NOT do (explicitly out of scope)

- **Multi-tenant fleet management.** That's what atelier-os is for.
  destiny-computer stays single-desktop.
- **Windows host support.** The KasmVNC + xdotool stack is Linux-only.
  Linux VMs work fine (Docker Desktop on macOS / WSL2 on Windows).
- **Browser-side AI**, e.g. extension that drives Chrome directly. The
  whole point of the per-AI desktop is isolation from your real
  browser.

## How to add a new entry

When you (or future-me) finds a bug you can't fix in the current pass:

1. Open an issue on the GitHub repo with reproducer + acceptance criteria
2. Add an entry here (D<next-number>) with link to the issue
3. Reference both in your commit message
4. If the entry blocks a public release, list it in the v0.X release notes
