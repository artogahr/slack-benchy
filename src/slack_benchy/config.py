"""Declarative configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """Raised for human-readable configuration problems."""


@dataclass(frozen=True)
class Config:
    slack_bot_token: str
    slack_app_token: str
    status_channel: str
    prusalink_host: str
    prusalink_api_key: str | None
    prusalink_username: str | None
    prusalink_password: str | None
    poll_interval_seconds: int
    offline_after_failures: int
    db_path: Path
    cancel_policy: str
    webcam_mode: str
    filament_inventory_seed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def prusalink_base_url(self) -> str:
        host = self.prusalink_host
        if host.startswith(("http://", "https://")):
            return host.rstrip("/")
        return f"http://{host}".rstrip("/")


def _get(env: dict[str, str], key: str, default: str | None = None) -> str | None:
    value = env.get(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _require(env: dict[str, str], key: str) -> str:
    value = _get(env, key)
    if not value:
        raise ConfigError(f"Required environment variable {key} is not set.")
    return value


def _int(env: dict[str, str], key: str, default: int, minimum: int = 1) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}.") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be at least {minimum}.")
    return value


def load_config(env: dict[str, str] | None = None) -> Config:
    """Load and validate config from a mapping (defaults to os.environ)."""

    e = dict(env if env is not None else os.environ)

    bot_token = _require(e, "SLACK_BOT_TOKEN")
    if not bot_token.startswith("xoxb-"):
        raise ConfigError("SLACK_BOT_TOKEN must start with 'xoxb-'.")
    app_token = _require(e, "SLACK_APP_TOKEN")
    if not app_token.startswith("xapp-"):
        raise ConfigError(
            "SLACK_APP_TOKEN must start with 'xapp-' (this is the app-level token used "
            "for Socket Mode, separate from the bot token)."
        )

    channel = _require(e, "SLACK_STATUS_CHANNEL")
    host = _require(e, "PRUSALINK_HOST")

    api_key = _get(e, "PRUSALINK_API_KEY")
    user = _get(e, "PRUSALINK_USERNAME")
    pw = _get(e, "PRUSALINK_PASSWORD")
    if not api_key and not (user and pw):
        raise ConfigError(
            "PrusaLink auth is missing. Set PRUSALINK_API_KEY, or both "
            "PRUSALINK_USERNAME and PRUSALINK_PASSWORD for Digest auth."
        )

    cancel = (_get(e, "CANCEL_POLICY", "anyone") or "anyone").lower()
    if cancel not in {"anyone", "starter_only"}:
        raise ConfigError("CANCEL_POLICY must be 'anyone' or 'starter_only'.")

    webcam = (_get(e, "WEBCAM_MODE", "auto") or "auto").lower()
    if webcam not in {"auto", "on", "off"}:
        raise ConfigError("WEBCAM_MODE must be 'auto', 'on', or 'off'.")

    seed_raw = _get(e, "FILAMENT_INVENTORY_SEED", "") or ""
    seed = tuple(s.strip() for s in seed_raw.split(",") if s.strip())

    return Config(
        slack_bot_token=bot_token,
        slack_app_token=app_token,
        status_channel=channel,
        prusalink_host=host,
        prusalink_api_key=api_key,
        prusalink_username=user,
        prusalink_password=pw,
        poll_interval_seconds=_int(e, "POLL_INTERVAL_SECONDS", 30, minimum=5),
        offline_after_failures=_int(e, "OFFLINE_AFTER_FAILURES", 4),
        db_path=Path(_get(e, "DB_PATH", "./slack-benchy.sqlite3") or "./slack-benchy.sqlite3"),
        cancel_policy=cancel,
        webcam_mode=webcam,
        filament_inventory_seed=seed,
    )
