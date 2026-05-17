# Contributing to destiny-computer

Sibling repo to [atelier-os](https://github.com/karany97/atelier-os)
(multi-session fleet); destiny-computer is the single-desktop case.
This CONTRIBUTING.md follows the same shape as atelier-os's so
operators who run both don't have to context-switch between two
processes.

Short on purpose. The project is ~1,500 LOC of Python (driver) plus
a small KasmVNC image config — the README + the code itself are
canonical for "how does this work". Use this file for the *process*
questions: how do I file an issue, how do I send a patch, how do I
know my change is good enough to merge.

## Filing an issue

1. **Reproduce in a minimal `docker compose up`.** If the issue isn't
   reproducible against a fresh clone, it's almost always operator-
   environment specific. Tell us what's different about yours.
2. **Check [`TRACKING.md`](./TRACKING.md) and the issues list first.**
   The 6 v0.2 deliverables (D1–D5b) all shipped + closed in the
   v0.2.1 launch sprint — TRACKING.md shows them as ✅ CLOSED. D6
   is the explicit sign-post pointing at atelier-os for the
   multi-user case. If your bug isn't covered there, the
   [WILL NOT do](./TRACKING.md#items-we-will-not-do-explicitly-out-of-scope)
   list is also worth a scan.
3. **Include**:
   - The exact command you ran
   - The exact output you saw
   - What you expected
   - Output of `docker compose -f compose/docker-compose.yml version`,
     plus `docker version`, plus the host OS
   - Which vision backend was active (`VISION_BACKEND=anthropic` vs
     `local-uitars`) — half of all bug reports trace back to a
     backend-specific edge case

## Sending a PR

**The 200-test gate is non-negotiable.** (Current floor is well above
that — see numbers below.)

Before you submit a PR:

```bash
# Unit tests must pass (no docker required, runs in ~2.4s)
STATE_DIR=/tmp/destiny-state \
  PYTHONPATH=driver/src python3 -m pytest driver/src/ -q
#  Expect: 424 passed (or more, if you added tests for your change)

# Optional smoke test against a live driver
DRIVER_URL=http://localhost:8090 \
  DESTINY_API_TOKEN=$(grep DESTINY_API_TOKEN .env | cut -d= -f2) \
  python3 scripts/launch-smoke.py
#  Expect: exit 0, "all green"
#  SMOKE_RUN_TASK=1 if you want to verify the Anthropic round-trip
#  (costs ~$0.10).
```

If either suite fails, the PR can't merge.

If your change is a *new feature*, add **at least one** test for the
happy path AND one for the most plausible failure mode. The
destiny-computer pattern is to test through the FastAPI TestClient
(no docker required) for fast unit coverage. Stdlib-only tests
preferred — no `requests`, no `httpx` in the test deps.

## What we're looking for

**Yes**:
- New `VisionBackend` implementations (the `driver/src/vision.py` ABC
  is the seam — adding GPT-4o or OmniParser is ~80 LOC + a prompt
  template; see `Holo3VisionBackend` as the model)
- Closing TRACKING.md items (with test coverage)
- Performance improvements with `before` / `after` benchmarks
- Documentation / README fixes (especially anything that reads as a
  *promise* the code doesn't fulfill)
- Better cost-ledger features (per-task tags, weekly rollups, alerts)
- Better snapshot UX (e.g., labelled snapshots, scheduled snapshots)

**Not yet** (open work for v0.3 and beyond):
- WebRTC streaming pipeline (vs the current KasmVNC HTTP stream)
- Borg-style incremental snapshot backend
- `network-agent` companion for `@hostname` routing across LAN hosts
- OmniParser + GPT-4o adapters slotting into the `VisionBackend` ABC

**Out of scope** (won't be accepted as PRs):
- Multi-tenant fleet management — that's what atelier-os is for
- Windows host support — KasmVNC + xdotool is Linux-only (Docker
  Desktop on macOS / WSL2 on Windows works fine)
- Browser-side AI (Chrome extension that drives your real browser)
  — the whole point of the per-AI desktop is isolation from your
  real browsing
- A hosted SaaS control plane — self-hosting IS the product

## Style

- Python 3.12. We don't pin python version below that — `tarfile.
  extractall(filter="data")` for path-traversal safety is 3.12+.
- `black` formatting is fine but not enforced; readability wins ties.
- `ruff` lint is welcome but not enforced.
- No mypy strict (yet).
- Comments answer "why", not "what". The code already says what.
- Commit messages: imperative ("add foo") not past tense
  ("added foo"). Multi-paragraph when the change has a non-obvious
  "why".

## Code-review philosophy

The repo's principal author reviews every PR. Reviews focus on:

1. **Does the test suite still pass?** (CI will catch this; the human
   review confirms.)
2. **Is there a test for the change?** (See gate above.)
3. **Does the README still tell the truth?** (i.e., are there claims
   the code no longer fulfills?)
4. **Is the change reversible?** (One-line revert if we discover it
   was wrong tomorrow.)
5. **Does it match the design intent?** (If it doesn't, we discuss
   in the PR rather than rewriting in review.)
6. **Cross-repo consistency.** destiny-computer's `DESTINY_API_TOKEN`
   pattern intentionally mirrors atelier-os's `ATELIER_API_TOKEN` —
   if you change one, the sibling should match (or have an explicit
   reason it diverges).

We're slower on big PRs than small ones. If you're considering a
500-line patch, please open an issue first to discuss the design.

## The no-cheating rule

This repo follows the same discipline as the rest of the Atelier
ecosystem (see [PERMANENT_RULES.md](https://github.com/karany97/nandai-atelier/blob/main/docs/ECOSYSTEM.md)):

- **Every number in any PR description / commit message / README claim
  traces to either a real measurement or an explicit FIXTURE label.**
  Memory-derived figures presented as live evidence are banned.
- **Every "shipped" claim has code on disk.** If the README says a
  feature exists, there must be a test that exercises it.
- **Hotfix discipline**: a regression test gets written FIRST, before
  the fix. The test file references the bug-report issue in a comment.

If you spot a violation of the rule (in the code, the README, or any
PR), filing an issue tagged `no-cheating-rule` is welcomed.

## License

By contributing, you agree your code is released under the same
[MIT license](./LICENSE) the project uses.

No CLA. No copyright assignment. Your name stays in `git log`.
