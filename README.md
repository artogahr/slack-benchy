# slack-benchy

A self-hosted Slack bot that surfaces a Prusa 3D printer in your workspace. Shows up in Slack as **Benchy** and maintains a live pinned status message in a channel of your choice. People can opt in to a DM when their print finishes, pauses, or errors. No public URL or port forwarding needed — it runs over Slack Socket Mode.

Single-tenant by design: install it into your own workspace with your own tokens, run it on a machine that can reach your printer.

## Requirements

- A Prusa printer running PrusaLink, reachable on the local network
- A Slack workspace where you can install a custom app
- A host on the same network (Raspberry Pi, NAS, NixOS box, anything with outbound internet)

## 1. Create the Slack app

You need two tokens. Both come from the same Slack app.

1. Open <https://api.slack.com/apps> → **Create New App → From an app manifest** → pick your workspace.
2. Paste [`slack-app-manifest.yaml`](slack-app-manifest.yaml). Before confirming, edit the `Owner:` line in `long_description` so it names you or your team. (Some IT teams require Purpose + Owner on every Slack app; both are pre-filled, just swap in your name.) Confirm. Scopes, Socket Mode, and the bot user are now set.
3. **Basic Information → App-Level Tokens**: generate a token with scope `connections:write`. That's your `SLACK_APP_TOKEN` (`xapp-...`).
4. **Install App → Install to Workspace**: that gives you the `SLACK_BOT_TOKEN` (`xoxb-...`).
5. In your status channel: `/invite @Benchy`.

## 2. Get PrusaLink credentials

On the printer: **Settings → Network → PrusaLink** shows the local IP and API key. Recent firmware uses the API key with the `X-Api-Key` header. Older firmware uses HTTP Digest with a username and password. Both are supported.

## 3. Run it

Three independent paths, all reading the same env (see [`.env.example`](.env.example)).

### Docker Compose (recommended)

```sh
git clone https://github.com/artogahr/slack-benchy.git
cd slack-benchy
cp .env.example .env   # fill in the five required vars
docker compose up -d
docker compose logs -f
```

Multi-arch image (`linux/amd64`, `linux/arm64`) is published to `ghcr.io/artogahr/slack-benchy`. State persists in the `benchy-state` named volume.

### Native install with systemd

```sh
sudo useradd --system --home /var/lib/slack-benchy --create-home prusabot
sudo -u prusabot bash -lc '
  cd /var/lib/slack-benchy
  python3 -m venv .venv
  .venv/bin/pip install git+https://github.com/artogahr/slack-benchy.git
'
sudo cp .env.example /etc/slack-benchy.env && sudo chmod 600 /etc/slack-benchy.env   # edit it
sudo cp systemd/slack-benchy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now slack-benchy
```

### Nix flake + NixOS module

```nix
inputs.slack-benchy.url = "github:artogahr/slack-benchy";

# in your NixOS config:
imports = [ slack-benchy.nixosModules.default ];
services.slack-benchy = {
  enable = true;
  statusChannel = "#printer";
  printerHost = "192.168.1.50";
  tokenFile.slackBotToken   = config.sops.secrets."benchy/slack-bot-token".path;
  tokenFile.slackAppToken   = config.sops.secrets."benchy/slack-app-token".path;
  tokenFile.prusalinkApiKey = config.sops.secrets."benchy/prusalink-api-key".path;
};
```

Secrets load via systemd `LoadCredential`, so they never reach `/nix/store` or `systemctl show`. Nix is optional; the Docker and native paths work without it.

## Configuration

Required env vars: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_STATUS_CHANNEL`, `PRUSALINK_HOST`, and either `PRUSALINK_API_KEY` or `PRUSALINK_USERNAME`+`PRUSALINK_PASSWORD`. See [`.env.example`](.env.example) for everything else (poll interval, offline threshold, cancel policy, filament inventory seed).

On boot the bot validates the config and prints a human-readable error if something is wrong.

## Known limitations

- "Currently loaded filament" is bookkeeping (what someone told Benchy), not a sensor reading. PrusaLink cannot identify the physical spool.
- 30s polling can miss sub-30s state blips.
- v1 is single-printer per instance.

## Contributing

See [`CLAUDE.md`](CLAUDE.md) for conventions and [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design spec.

## Attribution

This project was built by [Claude](https://claude.com/claude-code) (Anthropic's coding assistant) under manual human review.

## License

[MIT](LICENSE).
