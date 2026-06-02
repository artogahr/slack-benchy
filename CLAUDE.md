# CLAUDE.md

Agent handbook for working on this repo. Read this before touching code.

## Quick start

```sh
uv venv --python 3.12
uv pip install -e '.[dev]'
.venv/bin/pytest -q          # 91 tests, runs in <1s
.venv/bin/ruff check src tests
.venv/bin/python -m slack_benchy   # needs env vars from .env
```

`uv` is the package manager. Don't use bare `pip`. The venv is `.venv/`.

## Repo layout

```
src/slack_benchy/
├── config.py           pure: env → typed config, human-readable errors
├── sanitize.py         pure: user-text hardening for free-text fields
├── db.py               SQLite (WAL) behind an asyncio lock
├── prusalink.py        httpx async client, v1 + legacy fallback
├── transitions.py      pure: edge-triggered state diff → events
├── status_message.py   pure: Block Kit renderer + debounce decision
├── slack_app.py        Bolt handlers, StatusMessenger, DM emitter
├── poller.py           the unkillable 30s loop
├── logging_setup.py    secret-scrubbing log filter
├── single_instance.py  POSIX file lock guarding against duplicate runs
└── __main__.py         wires everything, validates config, runs forever
```

`ARCHITECTURE.md` is the spec the bot was built from. Treat it as load-bearing for behavior decisions; if a change would contradict it, flag the contradiction before implementing.

## Conventions that aren't obvious

**Pure-function modules.** `config`, `sanitize`, `transitions`, `status_message` do no I/O. The whole test strategy depends on this. If you find yourself wanting to add a network call or DB access in one of these files, the abstraction is wrong and you should hoist the I/O into the caller.

**Stale-action guard.** Every Slack handler that mutates state (`toggle_track`, `job_pause`, `job_resume`, `job_cancel_confirm`, cancel-modal submit) re-reads the current snapshot via `self._snapshot_fn()` and compares `job_key` before acting. The race is real: a button payload can arrive seconds after the world moved on. Look at `_on_job_cancel_confirm` for the canonical pattern.

**Edge-triggered notifications.** Transition detection takes a `(previous, current)` snapshot pair and emits events on transitions only. A persistent ERROR state never re-fires. If you add a new event type, write the test that exercises both "transition fires" and "stable state stays silent".

**No mocking the DB.** Tests get a real SQLite via the `db` fixture in `tests/test_db.py` / `tests/test_poller.py`. Mocked DBs hide migration bugs. The same applies if the test suite ever grows to cover the StatusMessenger: use a fake Slack client, not a fake DB.

**Secret scrubbing.** `logging_setup.RedactionFilter` strips `xoxb-`, `xapp-`, `X-Api-Key`, and `Authorization:` patterns from log lines. If you add a new kind of secret, extend the patterns there. Never `logger.debug(token)`.

## Conventional commits, owned by release-please

Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat: add foo` → minor bump
- `fix: bar` → patch bump
- `feat!: drop legacy auth` (or footer `BREAKING CHANGE: ...`) → major bump
- `chore:`, `docs:`, `ci:`, `test:`, `refactor:` → no bump

**Do not hand-edit** the version in `pyproject.toml` or `src/slack_benchy/__init__.py`. `release-please` owns them. It opens a release PR after every push to main; merging that PR cuts a tag, which triggers the multi-arch Docker build and wheel publish.

If you need to release ad-hoc, push the release PR through. Don't tag manually.

## Style

- Line length 120, enforced by ruff (see `[tool.ruff]` in `pyproject.toml`)
- Type hints on public functions; tolerated to be missing on private helpers
- Prefer dataclasses over plain dicts for state with structure
- Tests use real fixtures (real SQLite, fake collaborators for network)

User prose preferences when writing code comments, commit messages, PR descriptions, and README updates:

- No em dashes
- No "rule of three" lists if two examples make the point
- Short and direct, no AI tells

## Known intentional gaps

- **Webcam capture is in `PrusaLinkClient` but not wired into the status message.** Slack `chat.update` does not cleanly handle changing image attachments, and we'd need to upload via `files.upload_v2` and reference a Slack-hosted URL. Marked as future work in ARCHITECTURE.md §3.6.
- **Multi-printer.** v1 is single-printer hardcoded. The data model uses `job_key` as a primary key, so adding a `printer_id` column and a list of printers in config would be moderate, not a rewrite.
- **Cancel policy `starter_only`.** PrusaLink has no concept of who started a print, so we interpret "starter" as the first user who clicked **Track this print** on that job. If nobody is tracking, anyone can cancel.

## Running against a real printer

You need both Slack tokens and a reachable PrusaLink. See the README. If you don't have a printer handy, write a fake at the `PrusaLinkClient` interface seam (see `tests/test_poller.py` for the shape) rather than mocking httpx.

## When in doubt

Read the test that exercises the behavior you're about to change. If there isn't one, write one first.
