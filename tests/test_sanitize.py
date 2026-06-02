import pytest

from prusa_slack_bot.sanitize import (
    MAX_FILAMENT_NAME,
    SanitizeError,
    normalized_key,
    sanitize_filament_name,
)


def test_plain_name_passes_through():
    assert sanitize_filament_name("Prusament PLA Galaxy Black") == "Prusament PLA Galaxy Black"


def test_whitespace_collapsed_and_trimmed():
    assert sanitize_filament_name("  PLA\t\t  black\n\nspool  ") == "PLA black spool"


def test_empty_raises():
    with pytest.raises(SanitizeError):
        sanitize_filament_name("")
    with pytest.raises(SanitizeError):
        sanitize_filament_name("    ")


def test_strips_slack_channel_mention():
    out = sanitize_filament_name("<!channel> lol")
    assert "<" not in out
    assert "channel" not in out.lower() or out.lower().startswith("lol")


def test_strips_user_mention():
    out = sanitize_filament_name("PLA <@U12345> orange")
    assert "<@" not in out
    assert "U12345" not in out


def test_angle_brackets_outside_mention_are_neutered():
    out = sanitize_filament_name("PLA <weird> blue")
    assert "<" not in out
    assert ">" not in out


def test_control_characters_stripped():
    out = sanitize_filament_name("PLA\x00\x07black")
    assert out == "PLAblack"


def test_length_cap_applied():
    long = "x" * (MAX_FILAMENT_NAME + 50)
    out = sanitize_filament_name(long)
    assert len(out) <= MAX_FILAMENT_NAME


def test_zalgo_combining_marks_capped():
    zalgo = "a" + "́" * 30
    out = sanitize_filament_name(zalgo)
    combining = sum(1 for ch in out if ch == "́")
    assert combining <= 2


def test_normalized_key_case_insensitive():
    assert normalized_key("PLA Black") == normalized_key("pla  black")  # noqa: simple eq


def test_rejects_non_string():
    with pytest.raises(SanitizeError):
        sanitize_filament_name(123)  # type: ignore[arg-type]
