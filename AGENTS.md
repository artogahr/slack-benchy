# AGENTS.md

See [`CLAUDE.md`](CLAUDE.md) — same handbook, agent-agnostic name.

Quick reminders if you're not reading the full thing:

- `uv venv && uv pip install -e '.[dev]'` to bootstrap
- `.venv/bin/pytest -q` to run tests, `.venv/bin/ruff check src tests` to lint
- Conventional commits required (`feat:`, `fix:`, `chore:`, etc.) — `release-please` reads them
- Don't hand-edit `version` in `pyproject.toml`; `release-please` owns it
- `config.py`, `sanitize.py`, `transitions.py`, `status_message.py` are pure-function modules; no I/O
- Every Slack action handler re-checks state before mutating (stale-action guard)
- Read `ARCHITECTURE.md` for the spec, `CLAUDE.md` for the conventions
