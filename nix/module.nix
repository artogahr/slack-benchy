# NixOS module for slack-benchy.
#
# Usage in a flake:
#
#   inputs.slack-benchy.url = "github:artogahr/slack-benchy";
#
#   nixosConfigurations.printerpi = nixpkgs.lib.nixosSystem {
#     modules = [
#       slack-benchy.nixosModules.default
#       ({ ... }: {
#         services.slack-benchy = {
#           enable = true;
#           statusChannel = "#printer";
#           printerHost = "192.168.1.50";
#           # Tokens come from sops-nix / agenix — never put them in plain text.
#           tokenFile.slackBotToken = config.sops.secrets."prusa/slack-bot-token".path;
#           tokenFile.slackAppToken = config.sops.secrets."prusa/slack-app-token".path;
#           tokenFile.prusalinkApiKey = config.sops.secrets."prusa/prusalink-api-key".path;
#         };
#       })
#     ];
#   };

{ config, lib, pkgs, ... }:

let
  cfg = config.services.slack-benchy;
  pkg = cfg.package;
in
{
  options.services.slack-benchy = {
    enable = lib.mkEnableOption "slack-benchy service";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The slack-benchy package to run.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "prusabot";
      description = "Unix user the service runs as.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "prusabot";
      description = "Unix group the service runs as.";
    };

    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/slack-benchy";
      description = "Directory holding the SQLite database and lock file.";
    };

    statusChannel = lib.mkOption {
      type = lib.types.str;
      example = "#printer";
      description = "Slack channel name (with #) or channel ID where the status message lives.";
    };

    printerHost = lib.mkOption {
      type = lib.types.str;
      example = "192.168.1.50";
      description = "Hostname or IP of the printer running PrusaLink. Plain host, or http(s) URL.";
    };

    pollIntervalSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 30;
      description = "How often to poll PrusaLink.";
    };

    offlineAfterFailures = lib.mkOption {
      type = lib.types.ints.positive;
      default = 4;
      description = "Consecutive poll failures before the bot flips to OFFLINE.";
    };

    cancelPolicy = lib.mkOption {
      type = lib.types.enum [ "anyone" "starter_only" ];
      default = "anyone";
      description = "Who may cancel a print: anyone in the channel, or only the first tracker.";
    };

    webcamMode = lib.mkOption {
      type = lib.types.enum [ "auto" "on" "off" ];
      default = "auto";
      description = "Webcam snapshot: auto-detect, force on, or force off.";
    };

    filamentInventorySeed = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "Prusament PLA Galaxy Black" "Prusament PETG Anthracite Grey" ];
      description = "Initial filament inventory entries created on first run.";
    };

    tokenFile = {
      slackBotToken = lib.mkOption {
        type = lib.types.path;
        description = "Path to a file containing the Slack bot token (xoxb-...). Use sops-nix or agenix.";
      };
      slackAppToken = lib.mkOption {
        type = lib.types.path;
        description = "Path to a file containing the Slack app-level token (xapp-...).";
      };
      prusalinkApiKey = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Path to a file containing the PrusaLink API key. Either this or username/password is required.";
      };
      prusalinkUsername = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Optional PrusaLink HTTP Digest username (older firmware).";
      };
      prusalinkPasswordFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = "Path to a file containing the PrusaLink HTTP Digest password.";
      };
    };

    logLevel = lib.mkOption {
      type = lib.types.enum [ "DEBUG" "INFO" "WARNING" "ERROR" ];
      default = "INFO";
      description = "Log level for the service.";
    };
  };

  config = lib.mkIf cfg.enable {
    services.slack-benchy.package = lib.mkDefault (pkgs.callPackage ../. { } or pkg);

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
      createHome = true;
    };
    users.groups.${cfg.group} = { };

    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0750 ${cfg.user} ${cfg.group} -"
    ];

    systemd.services.slack-benchy = {
      description = "slack-benchy: Slack bot for Prusa 3D printers";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];

      environment =
        let
          base = {
            DB_PATH = "${cfg.stateDir}/slack-benchy.sqlite3";
            SLACK_STATUS_CHANNEL = cfg.statusChannel;
            PRUSALINK_HOST = cfg.printerHost;
            POLL_INTERVAL_SECONDS = toString cfg.pollIntervalSeconds;
            OFFLINE_AFTER_FAILURES = toString cfg.offlineAfterFailures;
            CANCEL_POLICY = cfg.cancelPolicy;
            WEBCAM_MODE = cfg.webcamMode;
            FILAMENT_INVENTORY_SEED = lib.concatStringsSep "," cfg.filamentInventorySeed;
            LOG_LEVEL = cfg.logLevel;
          };
          maybeUser = lib.optionalAttrs (cfg.tokenFile.prusalinkUsername != null) {
            PRUSALINK_USERNAME = cfg.tokenFile.prusalinkUsername;
          };
        in
        base // maybeUser;

      # Pull secrets at runtime so they never end up in /nix/store or in
      # `systemctl show`. LoadCredential makes the files available under
      # CREDENTIALS_DIRECTORY; the wrapper script reads each one into an env
      # var so the Python entrypoint sees a normal environment.
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.stateDir;
        Restart = "on-failure";
        RestartSec = "10s";
        KillSignal = "SIGTERM";
        TimeoutStopSec = "15s";

        LoadCredential =
          [ "slack-bot-token:${cfg.tokenFile.slackBotToken}"
            "slack-app-token:${cfg.tokenFile.slackAppToken}"
          ]
          ++ lib.optional (cfg.tokenFile.prusalinkApiKey != null)
            "prusalink-api-key:${cfg.tokenFile.prusalinkApiKey}"
          ++ lib.optional (cfg.tokenFile.prusalinkPasswordFile != null)
            "prusalink-password:${cfg.tokenFile.prusalinkPasswordFile}";

        ExecStart = pkgs.writeShellScript "slack-benchy-start" ''
          set -eu
          export SLACK_BOT_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/slack-bot-token")"
          export SLACK_APP_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/slack-app-token")"
          if [ -f "$CREDENTIALS_DIRECTORY/prusalink-api-key" ]; then
            export PRUSALINK_API_KEY="$(cat "$CREDENTIALS_DIRECTORY/prusalink-api-key")"
          fi
          if [ -f "$CREDENTIALS_DIRECTORY/prusalink-password" ]; then
            export PRUSALINK_PASSWORD="$(cat "$CREDENTIALS_DIRECTORY/prusalink-password")"
          fi
          exec ${cfg.package}/bin/slack-benchy
        '';

        # Hardening
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.stateDir ];
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
      };
    };
  };
}
