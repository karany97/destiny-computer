## What this changes

A sentence or two. If it closes an issue, write "Closes #N".

## Test gate (required before merge)

- [ ] `STATE_DIR=/tmp/destiny-state PYTHONPATH=driver/src python3 -m pytest driver/src/ -q` passes
- [ ] (Optional, costs money) `SMOKE_RUN_TASK=1 python3 scripts/launch-smoke.py`
      against a live driver passes
- [ ] New behaviour has a new test (unit OR live smoke)
- [ ] If you removed/changed any README claim, the docs no longer
      lie about what the code does
- [ ] If you added a new `VisionBackend` implementation, the
      `cost_usd = 0.0` regression guard is preserved for local
      backends

## Reviewer focus

What should the reviewer pay closest attention to? (Lines they should
read twice, design choices that deserve a sanity check, etc.)

## Cross-repo impact (if any)

Does this change anything that the sibling
[atelier-os](https://github.com/karany97/atelier-os) needs to mirror?
The two repos share the `VisionBackend` ABC pattern and the
`Bearer-token-env-name-distinct-per-repo` pattern by design — if
you change those, the sibling should match or have an explicit
reason for diverging.

## Reverse path

How would we revert this if we found it broke something tomorrow?

- [ ] One-line revert via `git revert <sha>` is safe
- [ ] Reverting needs additional cleanup (describe below)
