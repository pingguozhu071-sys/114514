简体中文 ([README.md](README.md)) | [日本語](README.ja-JP.md) | English

# Daedalus

**Unified Data Acquisition & Observation Engine (not a crawler)**

> Single-machine, Windows-first. Three acquisition environments (Direct Network / Browser Runtime / Artifact & Media) exist as **callable execution resources**; an evidence-driven router picks the cheapest sufficient path (L0→L5). **Capture first, understand later** — the raw layer is content-addressed (sha256), append-only, and every historical byte can be re-interpreted offline by a new parser (`reparse`).

## What it is

- **Three top-level experts**: Direct Network / Browser Runtime / Artifact & Media. Threads, processes, subprocesses and browser slots are just **execution resources** — the router decides when to use them based on evidence and cost.
- **The Task is the core object (not the URL)**: goal / scope / budget / **declared resources** / evidence chain / state. One task may traverse several environments.
- **Bounded routing**: transition caps, budget exhaustion stops escalation, failure is a routing signal (retries and throttles are two separate ledgers); every transition carries evidence and can be replayed.
- **Capture First**: raw layer is content-addressed, written atomically, never deleted. Wrong parser? Re-run later — the facts survive.
- **Security**: one outbound gate (SSRF checks with per-hop re-verification), credentials only via DPAPI, logs and exports are **sanitized before they land**.

## Quick start · Install

| Mode | How |
|---|---|
| Installer | Double-click `dist/Daedalus-Setup-<version>.exe` — full wizard, **never silent**; tri-lingual license page; desktop shortcut is opt-in |
| Portable | Copy `dist/daedalus/`, drop an empty `portable.flag` beside it; data lands in `DaedalusData/` next to it |

Self-check after install: `daedalus-cli --json doctor` — readiness, missing pieces, and **why the UI language is what it is** (the `ui` section), in one command.

## Quick start · CLI

```bash
daedalus-cli --json collect https://example.com/     # collect (per-domain limits + robots + SSRF gate)
daedalus-cli --json collect --dry-run https://example.com/   # plan only; self-attests network_calls=0
daedalus-cli --json reparse                          # re-interpret history with current parsers, offline
daedalus-cli --json export --out out.jsonl           # export (sanitized)
```

Ten subcommands: `version / config / doctor / metrics / alerts / collect / reparse / export / search / ui`.
With `--json`, stdout is **JSON only** and always **UTF-8** (regardless of console code page). Exit codes: `0` ok / `1` business failure / `2` usage / `3` blocked / `4` internal.

## GUI (`daedalus-cli ui`)

- **UI language**: settings > installer choice > system language > en-US; the title bar has `English ｜ 简体中文 ｜ 日本語` links for instant switching (full page rebuild, no mixed language).
- **Overview**: stat cards (rolling numbers) + the live resource plan + **quick collect** — paste targets one per line, or **import a TXT** (malformed lines dropped, deduped, grouped by domain; reports "imported N (dropped M invalid, deduped D)").
- **Tasks**: double-click any row to read its full decision chain (evidence); **retry / export selected / delete records** — deletion asks first and only removes task & evidence rows, **raw captures are never touched**.
- **Logs**: a live feed, **sanitized before display**, capped at 5000 lines, level-colored.
- **Looks**: wallpaper-aware accent extraction, four glass presets, three density steps, one motion switch (off = jump to final state, not "no feedback").

## Boundaries

- **Does**: session reuse, cookie import (your own sessions only), per-domain rate limits + jitter + adaptive backoff, and on blocking: slow down → stop → report with reasons.
- **Does not**: stealth injection / fingerprint spoofing / captcha solving / signature forgery / exit rotation / behavioral humanization.
- Stance: collect as completely as possible **where the user has access and the system can legally observe**.

## For developers

```bash
python tools/gates.py        # 13 gate suites · 291 checks (exit-code based)
python tools/ui_walk.py      # UI walkthrough · 37 checks
python tools/build.py        # packaging with hard artifact assertions + packaged smoke (real dry-runs)
```

Design docs in [`docs/`](docs/00-架构.md) (start at [`docs/权威源.md`](docs/权威源.md)); the bug ledger is [`docs/18-Bug与错误日志台账.md`](docs/18-Bug与错误日志台账.md); open items in [`docs/PENDING.md`](docs/PENDING.md).

## Known limits (honest list)

Never installed on a fresh machine (mechanism-level evidence only); real-monitor feel and multi-DPI untested; 24h soak not run (the 1h soak passed: 24,400 tasks / 0 lost rows); dependency CVE scan not done.

## License

[MIT](LICENSE)
