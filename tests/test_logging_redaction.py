import logging

from slack_benchy.logging_setup import RedactionFilter


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, "x", 0, msg, (), None)


def test_redacts_xoxb_token():
    rec = _record("Using token xoxb-1234-abc-XYZ for chat.write")
    RedactionFilter().filter(rec)
    assert "xoxb-1234-abc-XYZ" not in rec.getMessage()
    assert "***" in rec.getMessage()


def test_redacts_xapp_token():
    rec = _record("ws connecting with xapp-1-AAA-BBB-CCC")
    RedactionFilter().filter(rec)
    assert "xapp-1-AAA-BBB-CCC" not in rec.getMessage()


def test_redacts_api_key_query_param():
    rec = _record("GET http://printer/api?apikey=topsecretvalue")
    RedactionFilter().filter(rec)
    assert "topsecretvalue" not in rec.getMessage()


def test_redacts_authorization_header():
    rec = _record("Authorization: Digest username=foo realm=bar")
    RedactionFilter().filter(rec)
    assert "username=foo" not in rec.getMessage() or "***" in rec.getMessage()


def test_extra_secret_redacted():
    rec = _record("printer key was hunter2-very-secret in the logs")
    RedactionFilter(extra_secrets=["hunter2-very-secret"]).filter(rec)
    assert "hunter2-very-secret" not in rec.getMessage()
