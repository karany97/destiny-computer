---
name: Bug report
about: Something that should work, doesn't.
title: ''
labels: bug
assignees: ''
---

## What I did

```bash
# the exact command that fails
```

## What I expected

A description.

## What I got

```
# paste the actual output here, including stack traces, status codes,
# log lines from `docker compose logs driver | tail -30`
# or `docker compose logs desktop | tail -30`
```

## My setup

- destiny-computer version (commit SHA or release tag):
- Vision backend (`VISION_BACKEND=` value): anthropic | local-uitars | holo3
- Docker version (`docker version`):
- OS + kernel (`uname -a`):
- Architecture (`uname -m`):
- Any non-default env vars in `.env`:

## What I tried

- [ ] Re-ran `docker compose up -d` from a fresh clone
- [ ] Checked [TRACKING.md](../TRACKING.md) — bug isn't covered
- [ ] Searched existing issues — no duplicate
- [ ] If the bug involves the AI doing something unexpected: pasted
      the full per-step transcript from `/api/task/{id}` (the audit log)
