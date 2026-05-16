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

### D5 — ([#7](https://github.com/karany97/destiny-computer/issues/7)) Local-vision backend (no Anthropic dependency)

- **What**: Currently every task spends real money on Anthropic
  Computer Use. Operators on a tight budget can't run the loop at
  all.
- **Where**: `loop.py` is hard-coded to the Anthropic SDK; the v0.2
  README mentions `VISION_BACKEND=local-uitars` as a future option
  but it's not wired.
- **Why deferred**: Holo3-35B-A3B exists and is open-weights but
  needs a 24 GB GPU and ~2 min to spin up — non-trivial to ship
  inside the same `docker compose up` story.
- **Acceptance**: Optional compose profile `local-vision` that runs
  vLLM with Holo3, `VISION_BACKEND=local-uitars` actually routes
  through it.

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
