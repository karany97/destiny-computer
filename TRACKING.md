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

### D3 — Per-task rate limit on `/api/task`

- **What**: A misconfigured or compromised client could submit 100
  tasks/second and burn the entire `MAX_USD_PER_DAY` before the
  budget check on task 2 has even completed.
- **Where**: `driver/src/main.py::submit_task` — no per-IP / per-token
  rate limit
- **Why deferred**: v0.2.x ships for single-operator deployments. The
  per-day budget cap is the safety net.
- **Acceptance**: simple in-memory leaky-bucket keyed by client
  (token if set, else IP); default 10 tasks/minute; configurable
  via `DESTINY_TASK_RATE_LIMIT`.

### D4 — Snapshot/restore for the desktop container

- **What**: There's no equivalent of `POST /api/desktop/snapshot` to
  freeze a "trained" desktop state (logged-in browser sessions,
  installed apps) and clone it into a new container.
- **Where**: doesn't exist
- **Why deferred**: `docker commit destiny-desktop <tag>` works
  manually; this is sugar on top.
- **Acceptance**: `POST /api/desktop/snapshot` returns a snapshot id;
  `POST /api/desktop/restore {snapshot_id}` swaps the running
  container for one based on the snapshot.

### D5 — Local-vision backend (no Anthropic dependency)

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

### D6 — Multi-user `/api/task` (different operator personas)

- **What**: All tasks run as the same operator persona. No notion of
  "this is Janvi's task, that one is Devika's".
- **Where**: there's no user model in `main.py`
- **Why deferred**: v0.2 ships single-user. Multi-user is what
  atelier-os exists to solve (one container per teammate). If you
  need that, use atelier-os, not destiny-computer.
- **Acceptance**: explicitly out of scope for this repo — destiny-
  computer stays single-desktop. This entry exists only to point
  newcomers at the sibling repo.

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
