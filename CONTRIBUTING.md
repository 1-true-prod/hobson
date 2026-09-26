# Contributing to claudio

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
echo '{"hook_event_name":"Notification","message":"test"}' | python3 scripts/claudio.py
echo '{"hook_event_name":"PermissionRequest","tool_name":"Bash"}' | python3 scripts/claudio.py
echo '{"hook_event_name":"Stop"}' | python3 scripts/claudio.py

# Diagnostics
./claudio doctor
./claudio status
./claudio test
```

## Adding a Personality

1. Create `scripts/personalities/<name>/personality.json`
2. Follow the structure of an existing personality (start with `minimal` for templates-only, or `pirate` for templates + Ollama prompts)
3. Required sections: `templates` with `categories`, `permission`, `notification`, `generic`
4. Optional section: `prompts` with `system_prompt` and `examples` for AI-generated phrases
5. Test with `claudio personality <name> && claudio test`

## Adding an Engine

For a **static** (cached-template) engine:

1. Create `scripts/engines/<name>.py`
2. Subclass `BaseEngine` from `engines/base.py`
3. Set `templates_module`, `cache_dir`, `cache_ext`
4. Implement `backfill(text)` for audio generation
5. Add the engine to the CLI picker in `claudio` and to `install.sh`

For a **realtime** engine, override `speak_dynamic(phrase, allow_cold_start)` instead — that single
seam is what routes speech through your daemon, and it means commentary, batching, nudges and the
quiet controls all work without you reimplementing any of the gating. `kokoro_realtime.py` is the
reference; `pocket_tts_realtime.py` is the same shape.

## Code Style

- Bash: follow existing patterns in `claudio` and `install.sh`
- Python: no strict formatter, just keep it readable and consistent with existing code
- Keep dependencies minimal — this runs on every Claude Code hook event

## Pull Request Guidelines

- Keep PRs focused on a single change
- Test all affected event types before submitting
- Update `CLAUDE.md` if you change architecture or add commands
- Update `README.md` if you add user-facing features
