# prusa-slack-bot

A self-hosted Slack bot that turns a Prusa 3D printer into a first-class citizen of your workspace. It shows live status in a pinned message, lets people opt-in to DM notifications when their print finishes, tracks which filament is loaded, and exposes pause / resume / cancel buttons. Everything runs over Slack **Socket Mode**, so the bot needs no public URL, no port forwarding, and no TLS certificate.

> Single-tenant by design: you install it into **your** workspace with **your** tokens, and run it on **your** network. No "Add to Slack" button, no shared service.

---

## What it looks like

A single pinned message in the channel you choose:

```
Printer status: 🕒 Printing
File         Calicat.gcode
Progress     ▓▓▓▓▓▓░░░░░░ 42%
ETA          1h 00m left
Elapsed      30m
🧵 Loaded: Prusament PLA Galaxy Black
[ Track this print ] [ Swap filament ] [ Manage inventory ] [ Pause ] [ Cancel ]
nozzle 215°C · bed 60°C
updated 8s ago
```

If the printer hits an error, the message degrades visibly and anyone who clicked **Track this print** gets a DM. There is no channel-wide alert: people who didn't opt in don't get pinged.

---

## Requirements

- A Prusa printer running **PrusaLink**, reachable on the local network
- A Slack workspace where you can install a custom app
- A machine on the same network as the printer (Raspberry Pi, NAS, NixOS box, anything with outbound internet)

---

## 1. Create the Slack app (two tokens)

You need two tokens:

| Token | Starts with | Where it comes from | Used for |
|---|---|---|---|
| Bot token | `xoxb-` | After installing the app to your workspace | Posting and updating the status message, opening DMs |
| App-level token | `xapp-` | "App-Level Tokens" → generate with scope `connections:write` | The Socket Mode WebSocket |

Steps:

1. Open <https://api.slack.com/apps> and click **Create New App → From an app manifest**.
2. Pick your workspace.
3. Paste the contents of [`slack-app-manifest.yaml`](slack-app-manifest.yaml). Confirm. Slack sets up scopes, Socket Mode, interactivity, and the bot user in one go.
4. In the new app's settings, go to **Basic Information → App-Level Tokens** and generate a token with scope `connections:write`. Copy it (starts with `xapp-`). That's your `SLACK_APP_TOKEN`.
5. Go to **Install App** and click **Install to Workspace**. Approve. Copy the **Bot User OAuth Token** (starts with `xoxb-`). That's your `SLACK_BOT_TOKEN`.
6. Invite the bot to the channel where you want the live status message (`/invite @Prusa printer` in that channel).

If the manifest can't enable Socket Mode for your workspace policy, you'll see it during step 3. Ask your Slack admin to allow Socket Mode apps, or run the bot in HTTP mode (out of scope for this README).

---

## 2. Get your PrusaLink credentials

On the printer's display: **Settings → Network → PrusaLink** shows the local IP and the API key. Most recent firmware uses an API key with the `X-Api-Key` header. Older firmware uses HTTP Digest with a username + password. The bot supports both: configure whichever you have.

---

## 3. Pick a run path

Three independent paths. All read the same environment-variable config (see [`.env.example`](.env.example)). Pick one.

### Path A — Docker / Docker Compose (recommended default)

No Nix, no system-level Python. Works on Raspberry Pi (arm64) and any amd64 box.

```sh
git clone https://github.com/your-org/prusa-slack-bot.git
cd prusa-slack-bot
cp .env.example .env
# Edit .env: fill in SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_STATUS_CHANNEL,
# PRUSALINK_HOST, PRUSALINK_API_KEY.
docker compose up -d
docker compose logs -f
```

The state DB lives in the `prusa-bot-state` named volume, so it survives container or host restarts.

If you'd rather build the image yourself, in `docker-compose.yml` comment out the `image:` line and uncomment `build: .`.

### Path B — Native install (no Nix, no Docker)

Standard Python install. Good for Raspberry Pi OS and Debian.

```sh
sudo useradd --system --home /var/lib/prusa-slack-bot --create-home prusabot
sudo -u prusabot bash -lc '
  cd /var/lib/prusa-slack-bot
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install git+https://github.com/your-org/prusa-slack-bot.git
'
sudo cp .env.example /etc/prusa-slack-bot.env   # then edit it
sudo chmod 600 /etc/prusa-slack-bot.env
sudo cp systemd/prusa-slack-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prusa-slack-bot.service
sudo journalctl -u prusa-slack-bot -f
```

### Path C — Nix flake + NixOS module

The most reproducible path, for NixOS users who already use `sops-nix` or `agenix`.

```nix
{
  inputs.prusa-slack-bot.url = "github:your-org/prusa-slack-bot";

  outputs = { self, nixpkgs, prusa-slack-bot, ... }: {
    nixosConfigurations.printerpi = nixpkgs.lib.nixosSystem {
      system = "aarch64-linux";
      modules = [
        prusa-slack-bot.nixosModules.default
        ({ config, ... }: {
          services.prusa-slack-bot = {
            enable = true;
            statusChannel = "#printer";
            printerHost = "192.168.1.50";
            tokenFile.slackBotToken   = config.sops.secrets."prusa/slack-bot-token".path;
            tokenFile.slackAppToken   = config.sops.secrets."prusa/slack-app-token".path;
            tokenFile.prusalinkApiKey = config.sops.secrets."prusa/prusalink-api-key".path;
          };
        })
      ];
    };
  };
}
```

Secrets are loaded via systemd's `LoadCredential`, so they never end up in `/nix/store` or `systemctl show` output. A dev shell is available via `nix develop` — it gives you `uv`, Python 3.12, and `ruff`.

Nix is **never required**: Paths A and B are first-class.

---

## Configuration

All settings come from environment variables. See [`.env.example`](.env.example) for the full list. The important ones:

| Variable | Default | Notes |
|---|---|---|
| `SLACK_BOT_TOKEN` | required | `xoxb-...` |
| `SLACK_APP_TOKEN` | required | `xapp-...` |
| `SLACK_STATUS_CHANNEL` | required | `#printer` or `C0123ABCDE` |
| `PRUSALINK_HOST` | required | IP, hostname, or full URL |
| `PRUSALINK_API_KEY` | one of these is required | Plain API key from PrusaLink settings |
| `PRUSALINK_USERNAME` / `PRUSALINK_PASSWORD` | one of these is required | HTTP Digest credentials (older firmware) |
| `POLL_INTERVAL_SECONDS` | `30` | How often to poll the printer |
| `OFFLINE_AFTER_FAILURES` | `4` | Consecutive failures before flipping to OFFLINE |
| `DB_PATH` | `./prusa-slack-bot.sqlite3` | SQLite state file |
| `CANCEL_POLICY` | `anyone` | Or `starter_only` (only the first tracker can cancel) |
| `WEBCAM_MODE` | `auto` | `auto`, `on`, `off` |
| `FILAMENT_INVENTORY_SEED` | empty | Comma-separated list of spools to create on first run |

On boot the bot validates everything and prints a human-readable error if something is missing or unreachable.

---

## What it actually does

- **Live status message**: pinned in your chosen channel, updated on real change (state, ETA delta, ~5% progress), with a 30s heartbeat. Edits are debounced so we don't flap or burn rate limit.
- **Filament tracking**: bookkeeping only. The bot remembers what *you said* is loaded; PrusaLink can't see the spool. If the printer reports a material that disagrees with what you said is loaded, the status line shows a warning.
- **Opt-in DMs**: anyone in the channel can hit **Track this print**. Trackers get a DM when the print finishes, pauses, errors, or is cancelled. No channel-wide alerts.
- **Job control**: pause / resume / cancel buttons. Cancel goes through a confirmation modal that names the file, and re-verifies the same job is still running before acting.
- **Resilience**: a single bad poll is transient. The bot only flips to OFFLINE after several consecutive failures, and recovers automatically. Tokens are scrubbed from logs.
- **Single instance**: a file lock prevents two copies from updating the same message and flapping.

## What it doesn't do

- It's **not** a hosted multi-workspace service.
- "Currently loaded filament" is honest bookkeeping, not a sensor reading.
- 30s polling can miss sub-30s state blips (a momentary pause that auto-resumes).
- v1 is single-printer per instance. The data model treats a printer as a list of one, so adding multi-printer support later is config work, not a refactor.

---

## Development

```sh
uv venv --python 3.12
uv pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

The test suite covers the pure logic exhaustively (config parsing, input sanitization, transition detection, status rendering, debounce decisions) and uses fakes for the integration-shaped tests (poller, DB).

Architecture notes are in [`prusa-slack-bot-architecture.md`](prusa-slack-bot-architecture.md). Behavior is whatever the tests say it is.

---

## Releases

Tagged releases publish a multi-arch container image to `ghcr.io/your-org/prusa-slack-bot:vX.Y.Z` (linux/amd64 + linux/arm64) and attach a Python wheel to the GitHub release. Versions are bumped automatically by [release-please](https://github.com/googleapis/release-please) — merging the release PR it opens cuts a new tag.

---

## License

[MIT](LICENSE). Contributions welcome.
