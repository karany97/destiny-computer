<div align="center">

# Destiny Computer

### A long-lived Linux desktop that an AI owns.<br/>You watch it work. You take over the keyboard whenever you want.<br/>Your files persist between sessions.

[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e)](./LICENSE)
[![Composes with: Destiny Atelier](https://img.shields.io/badge/composes%20with-Destiny%20Atelier-c2410c)](https://github.com/karany97/nandai-atelier)
[![Stack: KasmVNC + Anthropic Computer Use](https://img.shields.io/badge/stack-KasmVNC%20%2B%20Anthropic%20Computer%20Use-3b82f6)](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo)

</div>

---

## What it is

A `docker compose up` that gives you a **persistent Linux desktop**
(Ubuntu + Firefox + a real shell + Python + whatever else you install)
reachable in your browser via [KasmVNC](https://kasmweb.com/kasmvnc),
paired with a **driver** that takes natural-language goals from any
chat surface, screenshots the desktop, decides the next click, and
narrates the result back to the chat.

It's the *body* for a chat. Designed to pair with [Destiny Atelier](https://github.com/karany97/nandai-atelier) — the single-file
chat ships a right-pane iframe that embeds this desktop.

```
   ┌──────────────────┐         ┌────────────────────────────┐
   │ Destiny Atelier  │  ───▶   │  Destiny Computer          │
   │  (the chat)      │         │  ┌──────────────────────┐  │
   │                  │  iframe │  │  KasmVNC Linux       │  │
   │  left pane:      │  embeds │  │  (the desktop you    │  │
   │  conversation    │   the   │  │   watch + can drive) │  │
   │                  │  right  │  └──────────────────────┘  │
   │  right pane:     │   pane  │  ┌──────────────────────┐  │
   │  live desktop    │         │  │  driver (the AI loop)│  │
   │  (this)          │  ◀───   │  │  screenshot→think→act│  │
   └──────────────────┘ replies │  │  narrates back       │  │
                                │  └──────────────────────┘  │
                                └────────────────────────────┘
```

## Why this isn't OpenHands or Claude Cowork or E2B

**OpenHands** — open source, has a browser+terminal loop, but the UX is
"agent decides everything, you watch a log spool by." No conversational
narration. No "the desktop stays running between sessions with your
browser tabs still open." Workspace is ephemeral.

**Claude Cowork** — closed source, $20-200/mo, **macOS/Windows only**,
runs on YOUR host (so it takes over your real keyboard while it works).
Polished UX but the wrong residency model — you can't put it on a VPS
or share it across teammates.

**E2B / Daytona / Scrapybara** — battle-tested sandboxes for code
execution, but their "desktop" tier has a 24-hour session cap or costs
$50-300/mo per always-on instance.

**Anthropic Computer Use API** — the model-side capability is real, but
you still have to *supply* the VM. There's no managed product that pairs
"persistent desktop + watchable VNC + conversational handoff + your
hardware or a $5 VPS" — until now.

## Install

### Quickstart (Docker compose)

```bash
git clone https://github.com/karany97/destiny-computer.git
cd destiny-computer
cp .env.example .env
# Edit .env: set ANTHROPIC_API_KEY, DESKTOP_PASSWORD, optional ATELIER_URL

docker compose up -d
# → KasmVNC desktop at  http://localhost:6901  (password from .env)
# → Driver health at    http://localhost:8090/health
```

### Pair with Destiny Atelier

If you already run [Destiny Atelier](https://github.com/karany97/nandai-atelier),
just open Settings → Computer and paste:

```
KasmVNC / noVNC URL: http://localhost:6901/
```

Then `⌘\` (Cmd+Backslash) toggles the right-pane Computer view. Done.

### Pair with anything else

The KasmVNC URL works in any iframe (sandboxed) or in a fresh browser
tab. Your existing chat is unaffected.

## What's in the box

```
destiny-computer/
├── compose/
│   ├── docker-compose.yml       — desktop + driver + reverse proxy
│   └── caddy/                   — optional TLS + auth in front
├── driver/
│   ├── Dockerfile
│   └── src/
│       └── main.py              — the manus_computer loop:
│                                  screenshot → vision model → action →
│                                  narrate → repeat
├── docs/
│   ├── architecture.md          — the chat ↔ driver ↔ desktop dance
│   ├── security.md              — what the AI can and cannot do
│   └── operations.md            — runbook: backup, snapshot, reset
├── .env.example
├── LICENSE                       — MIT
└── README.md
```

## What's supported

| | v0.2 (this) | v0.3 | v0.4 |
|---|---|---|---|
| Persistent desktop (KasmVNC, files survive restart) | ✅ | ✅ | ✅ |
| Conversational narration ("opened X, want me to Y?") | ✅ (SSE stream) | ✅ | ✅ |
| Anthropic Computer Use API path | ✅ (computer_20251124, Sonnet 4.5) | ✅ | ✅ |
| Per-task cost ledger + daily cap enforcement | ✅ | ✅ | ✅ |
| Live transcript stream (Server-Sent-Events) | ✅ (`/api/task/{id}/stream`) | ✅ | ✅ |
| Local-only vision (UI-TARS / OmniParser / Moondream) | ❌ | ✅ | ✅ |
| Multi-user (one desktop per user) | ❌ | 🚧 | ✅ |
| Snapshot + restore desktop state | ❌ | ✅ | ✅ |
| Real-time keystrokes from chat to desktop | ❌ | ❌ | ✅ |
| File-drop from chat to desktop home | ❌ | ✅ | ✅ |
| Browser cookies persist across "restart driver" | ✅ (via volume) | ✅ | ✅ |

## Cost reality

| Backend | Cost per active hour |
|---|---|
| Self-hosted on your spare PC | $0 |
| Self-hosted on a $5/mo VPS (Hetzner CX21) | $0.007 |
| Anthropic Computer Use API (model side, Sonnet 4.5 default) | $0.05–$0.40 per task (avg 30 steps); Opus 4.5 override 5× that |
| Local vision model (UI-TARS-1.5-7B) | $0 + your GPU |

Default `.env.example` uses Anthropic Computer Use because it's the
SOTA today; switch to local vision when v0.2 lands (or earlier if
you're brave).

## Security

| Threat | What this does about it |
|---|---|
| Arbitrary AI clicking inside your desktop | Container isolation — desktop runs in `--no-new-privileges --cap-drop=ALL --read-only` where possible. AI can break the desktop; it can't escape the container. |
| AI exfiltrating your secrets | Browser inside the desktop has its OWN cookie jar (Docker volume), separate from your real browser. AI can see what you logged it into; it can't read your real Chrome. |
| Strangers driving your desktop | KasmVNC password gate by default. Optional Caddy + Authelia for SSO. Optional [pingate](https://github.com/karany97/pingate) for the simple PIN-cookie pattern. |
| AI typing into your real keyboard | Impossible — the desktop is INSIDE a container; the AI's clicks/keystrokes never reach your host. |
| Long-running task spending unbounded $ on Anthropic | `MAX_STEPS_PER_TASK` env var (default 30); `MAX_USD_PER_DAY` env var (default 1.00). Hits the cap → driver stops + tells the chat. |

Full threat model in [docs/security.md](./docs/security.md).

## Configure

| Env var | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required for Anthropic Computer Use mode)* | Your API key |
| `DESKTOP_PASSWORD` | *(required)* | Password for KasmVNC web access |
| `ATELIER_URL` | *(empty)* | If set, the driver reports back to this Atelier instance via webhook |
| `MAX_STEPS_PER_TASK` | `30` | Hard cap on autonomous action steps per goal |
| `MAX_USD_PER_DAY` | `1.00` | Anthropic budget cap |
| `VISION_BACKEND` | `anthropic` | `anthropic` \| `local-uitars` \| `local-moondream` (v0.2+) |
| `DESKTOP_RESOLUTION` | `1280x720` | Desktop screen size |

## Roadmap

- **v0.2 (now)** — KasmVNC desktop + Anthropic Computer Use loop wired end-to-end (Sonnet 4.5 default, `computer_20251124` schema). Per-task cost ledger, daily cap enforcement, Server-Sent-Events stream of step records for live narration in the chat. Action surface: mouse_move, left/right/middle/double/triple_click, click+drag, type, key, hold_key, scroll, wait, cursor_position — every action runs via `docker exec destiny-desktop xdotool ...` (no host X11 dependency).
- **v0.3 (target Jul 2026)** — Local vision backend (UI-TARS-1.5-7B), snapshot/restore, file-drop from chat to desktop home
- **v0.4 (target Sep 2026)** — Multi-user (one desktop per user), real-time keystrokes from chat to desktop
- **v0.4** — Recipe library ("scrape this site", "fill this form", "do this every morning at 7am"), with operator-shareable templates

## License

[MIT](./LICENSE). Fork it, sell it, run it on your hardware or a VPS, share it with your team.

## Acknowledgements

- **[Anthropic computer-use-demo](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo)** — the reference implementation we wrap
- **[KasmVNC](https://github.com/kasmtech/KasmVNC)** — the web-native VNC server that makes the desktop iframe-able
- **The Destiny Atelier sprint** that defined what the chat-side integration looks like
