import pytest

from slack_benchy.config import ConfigError, load_config


def _base_env(**overrides):
    env = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_APP_TOKEN": "xapp-test",
        "SLACK_STATUS_CHANNEL": "#printer",
        "PRUSALINK_HOST": "192.168.1.50",
        "PRUSALINK_API_KEY": "secret",
    }
    env.update(overrides)
    return env


def test_minimal_env_loads():
    cfg = load_config(_base_env())
    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.poll_interval_seconds == 30
    assert cfg.offline_after_failures == 4
    assert cfg.cancel_policy == "anyone"
    assert cfg.prusalink_base_url == "http://192.168.1.50"


def test_missing_bot_token_raises():
    env = _base_env()
    del env["SLACK_BOT_TOKEN"]
    with pytest.raises(ConfigError, match="SLACK_BOT_TOKEN"):
        load_config(env)


def test_wrong_token_prefix_raises():
    with pytest.raises(ConfigError, match="must start with 'xoxb-'"):
        load_config(_base_env(SLACK_BOT_TOKEN="xapp-wrong-kind"))
    with pytest.raises(ConfigError, match="must start with 'xapp-'"):
        load_config(_base_env(SLACK_APP_TOKEN="xoxb-wrong-kind"))


def test_missing_prusalink_auth_raises():
    env = _base_env()
    del env["PRUSALINK_API_KEY"]
    with pytest.raises(ConfigError, match="PrusaLink auth"):
        load_config(env)


def test_digest_auth_accepted_without_api_key():
    env = _base_env()
    del env["PRUSALINK_API_KEY"]
    env["PRUSALINK_USERNAME"] = "maker"
    env["PRUSALINK_PASSWORD"] = "hunter2"
    cfg = load_config(env)
    assert cfg.prusalink_api_key is None
    assert cfg.prusalink_username == "maker"


def test_invalid_cancel_policy_raises():
    with pytest.raises(ConfigError, match="CANCEL_POLICY"):
        load_config(_base_env(CANCEL_POLICY="admin_only"))


def test_filament_seed_parsed():
    cfg = load_config(_base_env(FILAMENT_INVENTORY_SEED="PLA black, PETG clear , ,ASA white"))
    assert cfg.filament_inventory_seed == ("PLA black", "PETG clear", "ASA white")


def test_host_with_scheme_preserved():
    cfg = load_config(_base_env(PRUSALINK_HOST="https://printer.lan:8080/"))
    assert cfg.prusalink_base_url == "https://printer.lan:8080"


def test_non_integer_poll_interval_raises():
    with pytest.raises(ConfigError, match="POLL_INTERVAL_SECONDS"):
        load_config(_base_env(POLL_INTERVAL_SECONDS="soon"))


def test_poll_interval_floor():
    with pytest.raises(ConfigError, match="at least"):
        load_config(_base_env(POLL_INTERVAL_SECONDS="1"))
