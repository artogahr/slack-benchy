# slack-benchy — Architecture & Build Specification

> This document is a build spec. It describes a Slack bot that surfaces a Prusa
> 3D printer's status in a Slack workspace and lets people interact with it. It
> is written to be handed to a developer (human or AI) to implement.

---

## 1. Goal & deployment model

Managing a 3D printer in a shared office is a coordination problem: who's printing,
when it'll be done, what filament is loaded, whether it's stuck in an error state.
This bot makes the printer a first-class citizen in Slack — visible, interactive,
and able to reach out when it needs attention.

**Deployment model: single-tenant, self-hosted.**

- Each user installs this in **their own Slack workspace** with **their own Slack
  app** and **their own tokens**. Every install is fully independent.
- The bot runs on a machine on the **same local network as the printer** (a
  Raspberry Pi, a spare server, a NixOS box — anything that can reach the printer's
  LAN IP and reach the internet outbound).
- This is **NOT** a multi-tenant hosted service. We do not run infrastructure for
  other people, we do not handle other workspaces' OAuth, and we never need to reach
  printers on networks we don't control. The project is distributed as **code**
  (repo + container image + Nix flake), not as a hosted "Add to Slack" app.

This single-tenant model drives the most important architecture decision below.

---

## 2. Critical architecture decision: Socket Mode

**Use Slack Socket Mode. Do NOT build around public HTTP endpoints.**

Rationale:

- The host machine sits behind an office router with **no inbound ingress**. Socket
  Mode opens an **outbound WebSocket** to Slack, so the bot needs no public URL, no
  port forwarding, no reverse proxy, and no TLS certificate.
- Because each user self-installs into a single workspace manually, we do **not**
  need the OAuth "Add to Slack" distribution flow — which is the main reason one
  would otherwise need a public endpoint.
- Forcing every installer to expose a public HTTPS endpoint from their office
  network would be a massive adoption barrier and a security liability. Socket Mode
  keeps self-hosting accessible to non-experts.

**Socket Mode covers ~100% of this bot's feature set.** The only notable Slack
features it does NOT support are **link unfurling** and the **OAuth distribution
flow** — neither of which this project needs.

**Token implication:** Socket Mode requires an **app-level token** (`xapp-...`, scope
`connections:write`) **in addition to** the bot token (`xoxb-...`). That means **two
secrets**, which matters for config and the NixOS secrets module.

> Implementation note: use Slack **Bolt** (Python or JS). Bolt abstracts the
> transport, so if anyone ever wants link unfurling later, switching to HTTP changes
> only app initialization, not the handlers.

---

## 3. Features

### 3.1 Live printer status (pinned message)
A single message in a designated channel shows current printer state, what's
printing, and estimated finish time. It updates automatically; no one posts manually.

- On startup, **find-or-create** the status message and persist its `ts` (timestamp),
  so restarts reuse the same message instead of spawning duplicates.
- Update via `chat.update`. **Diff against last known state and only call
  `chat.update` when something meaningful changed** (state transition, ETA crossing a
  threshold, or progress moving by a meaningful delta — e.g. every 5–10% or every few
  minutes — NOT every poll). This avoids edit spam and rate-limit pressure.
- Include a freshness footer: "updated Xs ago." If polls start failing, the message
  must **visibly degrade** (e.g. ⚠️ "haven't heard from the printer in 5 min")
  rather than silently looking like a healthy idle state.

### 3.2 Filament tracking
The bot tracks which filaments are available (an inventory) and what is currently
loaded. When someone swaps a spool, they hit a button and pick from a list (modal).
The change is reflected in the **status message** — no separate notification is sent.
Anyone interested can look at the pinned message to see what's loaded; there's no need
to ping the channel.

- **Honesty about what this is:** PrusaLink does not reliably know which physical
  spool is loaded (material type at best). "Currently loaded" is **bot-side
  bookkeeping** — it reflects what someone last *told* the bot, not ground truth.
  Document it as such; do not oversell it as authoritative.
- **Reconciliation:** when a print starts, if PrusaLink reports a material type that
  disagrees with the recorded loaded spool, surface a gentle ⚠️ flag rather than
  silently trusting either source.

### 3.3 Opt-in print notifications ("Track this print")
While a print is running, anyone in the channel can click "Track this print" and get
a **DM** when it finishes, is paused, or errors.

- This is **edge-triggered transition detection**, not a snapshot check. Persist the
  last observed printer state and fire notifications on the **transition** (e.g.
  `PRINTING → FINISHED`, `PRINTING → PAUSED`, `* → ERROR`), not on "is it errored
  right now."
- Accept that with a 30s poll, **sub-30s blips may be invisible** (brief pause that
  auto-resumes, transient error that self-clears). Document this limitation.
- Multiple users may track the same print; DM each tracker once per relevant event.
  Clear/expire subscriptions when the job completes.

### 3.4 Error handling (no channel broadcast)
Errors and unexpected stops are surfaced in two low-noise ways only:
- The **status message** reflects the error state visually (⚠️) — silent, notifies
  no one.
- **Trackers** of the current print receive a **DM** about the error (see 3.3).

There is **no automatic channel-wide alert** for errors. This is deliberate: don't
distract people who didn't opt in to the current print.

### 3.5 Job controls (recommended addition)
PrusaLink supports job commands. Expose buttons for **pause / resume / cancel**.

- **Cancel is destructive** — gate it behind a confirmation modal.
- Consider restricting who may cancel a job someone else started (config option).

### 3.6 Webcam snapshot (recommended addition, if camera present)
If the printer/PrusaLink exposes a snapshot, attach a current image to the status
message. This is the single highest-value optional feature — it answers "is my print
okay?" without walking over to the printer. Make it optional/auto-detected.

---

## 4. How it works (runtime)

- The bot polls the printer's **PrusaLink local API every 30 seconds** for status and
  job info, and keeps the Slack status message up to date.
- All interaction (filament changes, tracking a print, managing inventory, job
  controls) happens through **Slack buttons and modals** over Socket Mode. No separate
  web UI.

### PrusaLink API notes
- Endpoints: prefer the v1 endpoints (`/api/v1/status`, `/api/v1/job`); also support
  legacy (`/api/version`, `/api/printer`, `/api/job`) for broader firmware coverage.
- Job commands: `POST /api/v1/job/{id}/pause`, `.../resume`, plus stop/cancel per
  firmware.
- **Auth:** PrusaLink uses an API key (`X-Api-Key` header) or HTTP Digest depending on
  version/config. **Support both.** If auth is misconfigured, **fail loudly with a
  clear message**, not a silent "offline."
- **Resilience:** the printer's onboard API can be slow and may drop requests under
  load. Treat a **single failed poll as transient** (retry/backoff); only declare
  "offline" after several consecutive failures. Never crash the poller on a single
  request error.

### Polling vs transitions (summary)
- Polling drives the status message (debounced updates).
- A separate state-comparison step drives notifications (edge-triggered).
- Both read from the same poll result each cycle.

---

## 5. Persistence

A Raspberry Pi loses power; restarts must not lose state. **Do not keep state only in
memory.** Use **SQLite** (low friction, single file).

Persist at least:
- The status message `ts` (and channel ID) — so restart reuses the pinned message.
- Filament inventory + currently-loaded spool.
- Active "track this print" subscriptions (user ID, job identifier, which events).
- Last observed printer state (for edge-triggered transition detection).
- Optionally: a job history log (file name, duration, who tracked it) for "who left
  the bed covered in spaghetti" diagnostics.

On NixOS, the DB path lives under `/var/lib/<service-name>/`.

---

## 6. Configuration (must be fully declarative)

Nothing user/site-specific may be hardcoded. A single config file (env vars + a file,
or just env vars) holds:

- `SLACK_BOT_TOKEN` (`xoxb-...`)
- `SLACK_APP_TOKEN` (`xapp-...`, for Socket Mode)
- Slack channel (the status channel) — by ID or name
- Printer host/IP
- PrusaLink API key (and/or Digest credentials)
- Poll interval (default 30s)
- Optional: who-can-cancel policy, webcam on/off, filament inventory seed

**Startup validation:** on boot, validate config and connectivity, and fail with
**human-readable errors**, e.g. "Can't reach PrusaLink at 192.168.1.50 — check the IP
and that the printer is on," and "Slack token missing scope `chat:write`." Never dump
a raw traceback as the primary failure mode.

**First-run behavior:** find-or-create the status message; post a short "I'm alive,
here's how to use me" message; handle empty states gracefully (e.g. no filaments
configured yet).

### Multi-printer forward-compatibility
v1 ships **single-printer** (matches the core use case). But model a printer as **an
item in a list of one**, not a global singleton, so adding multi-printer support later
is config work rather than a painful refactor. Offices with two printers are common
enough that this will be requested.

---

## 7. Packaging & distribution (this is where shareability lives)

Most adoption friction is in **onboarding**, not transport. Prioritize:

### 7.1 Slack app manifest (highest-value artifact)
Ship a Slack **app manifest** (YAML/JSON) in the repo so a user creates their app —
with correct scopes, Socket Mode enabled, interactivity configured — in one paste
instead of clicking through many settings pages.

Required Slack scopes (bot token), at minimum:
- `chat:write` — post/update the status message
- `chat:write.public` (or be invited to the channel) — post to the channel
- `im:write` — open DMs for tracked-print notifications
- `commands` — if slash commands are used
- `reactions:write` / `files:write` — only if used (e.g. webcam image upload via
  `files:write`)

App-level token scope: `connections:write` (Socket Mode).

Enable: Socket Mode; Interactivity; Event Subscriptions as needed (over Socket Mode);
and the bot user.

### 7.2 Two-token setup, clearly documented
Explain where the `xoxb-` bot token and the `xapp-` app-level token come from and what
scopes each needs. A short first-run check should validate both tokens and report
exactly which scope/permission is missing.

### 7.3 Run paths (Nix must NOT be required)

Provide **three** independent ways to run the bot. A user must be able to get fully
running **without Nix installed**. All paths read the **same config** (Section 6) and
produce the same behavior. List them in the README in this order:

**Path A — Docker / Docker Compose (recommended default, no Nix).**
Ship a published container image and a `docker-compose.yml`. This is the lingua
franca of "run this on my Pi" and should be the headline instructions. The user
fills in a `.env` (or compose env block) with the tokens, channel, printer host, and
PrusaLink key, then `docker compose up -d`. Build multi-arch images (at least
`linux/arm64` for Raspberry Pi and `linux/amd64`) so Pi users aren't stuck. Persist
the SQLite DB via a mounted volume so state survives container/host restarts.

**Path B — Plain native install (no Nix, no Docker).**
For users who want to run it directly on Raspberry Pi OS / any Linux: a standard
language-native setup. For Python, that's `pip`/`uv` (or `pipx`) into a virtualenv;
for Node, `npm install` + `node`. Document running it under a process supervisor so
it restarts on boot and on crash — provide a sample **systemd unit** (the common case
on Raspberry Pi OS / Debian). Secrets come from an env file referenced by the unit,
never hardcoded.

**Path C — Nix flake + NixOS module (best experience for the Nix crowd).**
The **flake** gives a reproducible dev shell and package; the **NixOS module**
declares the bot as a system service with secrets via `sops-nix` or `agenix`. This
is a genuine selling point for NixOS users — give the module clean, documented
options. The dev shell must use env vars, never hardcoded secrets. **Nix remains
strictly optional**: nothing in Paths A or B may depend on it.

> Keep one canonical config schema and one entrypoint; the three paths are only
> different ways of installing dependencies and supervising the same process.

---

## 8. Requirements (end-user)

- A Prusa printer running **PrusaLink**, reachable on the local network.
- A Slack workspace where the user can install a custom app.
- A machine on the same network to host the bot (Raspberry Pi, spare server, NixOS
  box — anything with outbound internet and LAN access to the printer).

---

## 9. Robustness, abuse resistance & input validation

This bot lives in a shared channel and will be installed by non-experts. Assume
fat-fingers, bored coworkers, flaky networks, and a printer that reboots mid-job.
Harden these surfaces explicitly — none of this is optional polish.

### 9.1 User text inputs (free-text modal fields)
Applies to "add new filament" and any other free-text field.
- **Length cap:** reject/truncate to a sane limit (e.g. filament name ≤ 64 chars).
  Enforce server-side, not just via the modal's `max_length` hint (a malicious client
  can bypass UI hints).
- **Sanitize:** trim whitespace; collapse internal newlines/tabs (they break the
  status-message layout); reject empty/whitespace-only.
- **Neutralize Slack markup injection:** strip or escape mention syntax
  (`<!channel>`, `<!here>`, `<@U…>`) and link/format markup so a filament named
  `<!channel> lol` can't ping the room or forge formatting when echoed into the
  status message. Render user strings as plain text.
- **Unicode hygiene:** cap or strip excessive combining characters / zalgo and
  control characters; normalize. A name shouldn't be able to visually corrupt the
  message.

### 9.2 Inventory limits
- **Max inventory size** (e.g. ≤ 50 spools). Slack `static_select` tolerates only
  ~100 options; an unbounded "add filament" eventually breaks the swap modal
  silently. Cap well below that, with a clear "inventory full" message.
- **Dedupe** on add (case-insensitive, post-sanitization) so the list doesn't fill
  with near-duplicates.
- Provide **remove** for inventory entries so a full/messy list is recoverable
  without DB surgery.

### 9.3 Button / action abuse
- **Idempotency:** "Track this print" is **set membership**, not append — clicking
  it five times tracks once. Offer a clear "Untrack" toggle.
- **Debounce / disable-after-click:** rapid double-clicks must not double-fire
  handlers or send duplicate DMs. Debounce job-control buttons (pause/resume) so
  mashing them doesn't thrash the PrusaLink API.
- **Stale-action guard:** an interaction payload may arrive for a job/state that has
  since changed. Re-check current state in the handler before acting; if the world
  moved (e.g. the job already ended), respond with a friendly "that's no longer the
  current print" instead of acting on stale assumptions.

### 9.4 Destructive actions (cancel)
- Confirmation modal (already specified) **plus** the stale-action guard above: the
  confirmation must name the specific job, and the handler must verify that same job
  is still running before cancelling — never cancel "whatever is running now."
- Optional permission gate (config): who may cancel a job they didn't start.

### 9.5 The poll loop must be unkillable
The poller is the bot's spine; a single bad response must never kill it.
- Wrap each poll in try/except. Handle: timeouts, connection refused (printer
  rebooting), HTTP 4xx/5xx, malformed/partial JSON, and unexpected schema. Log and
  continue — never let the loop exit.
- Treat a single failure as transient; only flip to "offline" after **N consecutive**
  failures, and recover automatically when polls succeed again.
- Bound work: a slow PrusaLink response must not stall the loop forever — use request
  timeouts.

### 9.6 ETA / numeric sanity
- Sanity-bound values from the printer before display: clamp negative time-remaining,
  ignore absurd ETAs (e.g. > 30 days), guard against divide-by-zero on progress.
- Don't trust the host clock blindly for "finish at HH:MM"; if showing wall-clock
  ETA, derive from time-remaining rather than assuming clocks agree.

### 9.7 SQLite concurrency
- The poll loop and interaction handlers both write. Enable **WAL mode** and
  **serialize writes** (single connection/queue or short transactions) to avoid
  `database is locked` errors and corruption.

### 9.8 Slack rate limits & reconnect
- Respect 429 responses with backoff; the debounced `chat.update` (Section 3.1) is
  the main defense. Don't update on unchanged state.
- Socket Mode reconnect must use backoff, not a tight reconnect loop, to avoid
  hammering Slack if the token/network is bad.

### 9.9 Secret hygiene
- **Never log secrets:** scrub the PrusaLink API key and Slack tokens from logs and
  error messages, including any logged request URLs/headers.
- Startup validation errors should say *what* is wrong (missing scope, bad host)
  without echoing the secret value.

### 9.10 Single-instance guard
- Two copies running (e.g. a stray Docker container plus a systemd service) will both
  poll and both edit the status message, causing flapping. Document this; optionally
  use a lock file / single-instance check and warn loudly on conflict.

---

## 10. Build checklist (for the implementer)

1. **Transport:** Bolt app in Socket Mode; two tokens from config; reconnect logic
   tested.
2. **Poller:** 30s loop hitting PrusaLink (v1 + legacy fallback; API-key + Digest
   auth); resilient to single failures; backoff; "offline" only after N consecutive
   failures.
3. **State store:** SQLite with message `ts`, inventory, loaded spool, subscriptions,
   last-observed state, optional job history.
4. **Status message:** find-or-create on boot; debounced `chat.update`; freshness
   footer; visible degradation on poll failure.
5. **Transition engine:** compare current vs last state; emit FINISHED / PAUSED /
   ERROR events; DM trackers only (no channel broadcast); reflect error in status
   message.
6. **Interactions:** "Track this print" button; filament-swap button → modal →
   inventory pick → update status message (no notification); inventory management;
   pause/resume/cancel
   (cancel behind confirm modal, optional permission gate).
7. **Filament reconciliation:** warn on material mismatch at print start.
8. **Webcam (optional):** attach snapshot if available.
9. **Config + validation:** declarative; human-readable startup errors; friendly
   first-run.
10. **Packaging:** Slack app manifest; three run paths reading one config — (A)
    multi-arch Docker image + compose with a volume for SQLite, (B) native install
    (pip/uv or npm) + sample systemd unit, (C) Nix flake + NixOS module
    (sops/agenix); README with two-token walkthrough and screenshots. Nix must not
    be required for A or B.
11. **Forward-compat:** printer modeled as a list of one.
12. **Hardening (Section 9):** input length caps + sanitization + mention-injection
    stripping; inventory cap + dedupe + remove; idempotent tracking + debounced
    buttons + stale-action guard; unkillable poll loop; ETA sanity bounds; SQLite WAL
    + serialized writes; secret scrubbing in logs; backoff on Slack 429 / Socket
    reconnect; single-instance guard.

---

## 11. Known limitations (document these honestly)

- "Currently loaded filament" is bot-maintained bookkeeping, not printer ground truth.
- 30s polling can miss sub-30s state blips.
- Single-workspace, single-tenant by design; not a hosted multi-workspace app.
- v1 is single-printer.
