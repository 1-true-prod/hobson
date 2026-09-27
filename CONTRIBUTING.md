# Contributing to Hobson

Thanks for your interest in contributing! Here's how to get started.

## Getting Started

1. Fork and clone the repo
2. Run `./install.sh` to set up hooks and pick your engine
3. Make your changes
4. Run the test suite, and test manually (see below)
5. Open a pull request

## Testing

There's a pytest suite — offline and silent (Ollama HTTP and `afplay`/`say` are mocked, and
`~/.claude` is redirected to a tmp dir by the `claude_home` fixture, so running it never touches
your real config or makes noise):

```bash
python3 -m venv venvs/dev && ./venvs/dev/bin/pip install -r requirements-dev.txt
./venvs/dev/bin/python -m pytest tests/
./venvs/dev/bin/python -m pytest tests/ --cov=scripts --cov-report=term-missing
```

Coverage is report-only — there's no failing threshold. Don't pipe pytest into `tail` or `head` to
check it passed: that masks the exit code.

Then test manually:

```bash
# Test each event type
echo '{"hook_event_name":"Notification","message":"test"}' | python3 scripts/hobson.py
echo '{"hook_event_name":"PermissionRequest","tool_name":"Bash"}' | python3 scripts/hobson.py
echo '{"hook_event_name":"Stop"}' | python3 scripts/hobson.py

# Diagnostics
./hobson doctor
./hobson status
./hobson test
```

The installer, the setup wizard and uninstall change your real setup, so try them in the sandbox
instead: a scratch home, installed from a snapshot of your working tree through the same
`curl | bash` path a new user takes.

```bash
scripts/sandbox.sh new       # install into the sandbox; the setup wizard opens
scripts/sandbox.sh shell     # a shell there: hobson setup, hobson status, hobson uninstall, ...
scripts/sandbox.sh check     # is your own install as it was?
scripts/sandbox.sh destroy   # remove the sandbox and everything it started
```

A scratch `HOME` alone is not enough: the camera permission belongs to your macOS account, not to
a home, and Homebrew, Ollama's models and the voice daemons' ports are shared. The script's header
says what it guards and what it only warns about.

## Adding a Personality

1. Create `scripts/personalities/<name>/personality.json`
2. Follow the structure of an existing personality (start with `minimal` for templates-only, or `pirate` for templates + Ollama prompts)
3. Required sections: `templates` with `categories`, `permission`, `notification`, `generic`
4. Optional section: `prompts` with `system_prompt` and `examples` for AI-generated phrases
5. Test with `hobson personality <name> && hobson test`

## Adding an Engine

For a **static** (cached-template) engine:

1. Create `scripts/engines/<name>.py`
2. Subclass `BaseEngine` from `engines/base.py`
3. Set `templates_module`, `cache_dir`, `cache_ext`
4. Implement `backfill(text)` for audio generation
5. Add its `engine_name` to `ENGINE_TAGS` in `scripts/log_record.py`, or every reader of the log
   takes its tag for part of the message (a test fails until it is there)
6. Add the engine to the CLI picker in `hobson` and to `install.sh`

For a **realtime** engine, override `speak_dynamic(phrase, allow_cold_start)` instead — that single
seam is what routes speech through your daemon, and it means commentary, batching, nudges and the
quiet controls all work without you reimplementing any of the gating. `kokoro_realtime.py` is the
reference; `pocket_tts_realtime.py` is the same shape. Its `engine_name` goes in `ENGINE_TAGS` too.

## Code Style

- Bash: follow existing patterns in `hobson` and `install.sh`
- Python: no strict formatter, just keep it readable and consistent with existing code
- Keep dependencies minimal — this runs on every Claude Code hook event

## Pull Request Guidelines

- Keep PRs focused on a single change
- Test all affected event types before submitting
- Update `CLAUDE.md` if you change architecture or add commands
- Update `README.md` if you add user-facing features
