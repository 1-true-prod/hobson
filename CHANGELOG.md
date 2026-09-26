# Changelog

## Unreleased

- Installing a pinned release (`CLAUDIO_REF=v0.2.0`) no longer prints git's "is not a commit!"
  warning and detached-HEAD advice

## 0.2.0 — 2026-09-26

First release for public testing.

### Install and update

- **One-liner that works when piped.** `curl … | bash` used to hand the installer the pipe as its
  stdin, so the first menu read EOF and the install stopped before writing any hook. Prompts now
  read from the terminal, and with no terminal at all (CI, ssh) it installs the defaults
- `--yes` for an unattended install (`curl … | bash -s -- --yes`): the macOS voice, nothing heavy
  downloaded. Ollama, uv and models are only installed after an explicit yes
- **Re-running is updating.** An existing config is kept and only the hooks are refreshed, which
  leaves an unchanged `settings.json` alone. New: `claudio update`, and `CLAUDIO_REF` to pin a
  branch or tag
- The installer writes only the choices you made; every other setting comes from the defaults at
  load time, so improved defaults reach existing installs (it used to freeze
  `daemon_idle_timeout` at 180)
- Python 3.9+ instead of 3.10+: the stock macOS `/usr/bin/python3` (3.9.6) is enough, and the
  suite runs on it in CI
- Hooks run the interpreter the installer checked, by absolute path, so a Claude Code launched from
  the desktop app or an IDE, with a different `PATH`, runs the same Python
- `settings.json`: follows `CLAUDE_CONFIG_DIR`, writes through a symlink instead of replacing it,
  keeps its file mode, survives `null` sections, and no longer carries a `Bash(say:*)` permission
  (hooks never needed it, and `say -o` writes files; an update removes the old grant)
- claudio's hooks are recognised by their entrypoint, not by the word "claudio" in a command,
  which claimed (and on uninstall deleted) the hooks of anyone whose home is `/Users/claudio`
- A pyenv / asdf / mise shim is resolved to the Python behind it before it is pinned; `doctor`
  reports a pinned interpreter that has since been uninstalled
- `claudio uninstall [--yes]` stops both daemons and any pending nudge, removes all state files
  (not just five of them), and can delete the installer-managed checkout
- Fixed under Homebrew's bash 5: the installer's menus exited on the first down-arrow and
  `claudio doctor` stopped at its first finding (`((x++))` from 0 fails under `set -e` there).
  Also: menus under `TERM=dumb`, `doctor` for chatterbox users with no cache yet, and
  `claudio daemon start` for pocket-tts on bash 3.2, which all exited early
- Updating refuses a `CLAUDIO_DIR` that is not a claudio checkout, and a checkout with commits
  that were never pushed, instead of resetting them away
- `claudio doctor` names the model claudio actually uses (it suggested pulling `qwen3.5:4b`), shows
  why hooks are out of date, and exits non-zero on errors
- The installer's Ollama model check no longer reports a present model missing (`grep -q` under
  `pipefail`)
- CI on macOS: the suite on system Python and 3.13, shellcheck, and the full
  install → update → doctor → uninstall cycle under bash 3.2 and 5

### Voice quality

- Phrases are checked before they are spoken: a 4–12 word budget enforced in both prompt and gate,
  a past-tense detector for commentary (repaired rather than discarded, including compound and
  irregular tenses), and a near-duplicate guard that decays so old repeats stop causing silence
- **Optional Jev decider** (`"decider": {"backend": "jev"}`, via OpenRouter) — a second opinion on
  phrases the near-duplicate guard rejected, aimed at the tense transition it can't see ("I fixed
  the invoice sync" after "I'm fixing the invoice sync"), and on commentary that rewords something just
  said, which it can hold back. It never silences a Stop, permission or notification; it sends
  only the phrase and up to six recent ones; the key lives in `~/.claude/claudio.env`
- Commentary names the symbol an edit touched in more languages (Kotlin modifiers and extension
  functions, Go, Rust, shell), and no longer invents one from prose in Markdown files
- `tts_normalize` applied to LLM-generated phrases, with four gaps closed against real logged output
- Plainer verbs, prompt rules for tense and naming, and no more speaking the category separator aloud
- Log lines carry the date and the project, the generation pipeline is traceable end to end, and
  `scripts/log_analyse.py` turns every published defect rate into a command

### Saying less

- **Batched commentary** — tool calls are queued and summarised in one utterance once the batch is
  full (`commentary.min_tool_calls`) or old enough (`commentary.min_seconds`), instead of narrating
  every call. A finished turn discards anything still queued
- **`anomaly` verbosity** — ignores the count and timer gates and speaks only what is surprising
- **Jev decides which batches are worth hearing** — with the decider on, each batch of commentary
  is scored before Ollama is asked, and one dial, `commentary.chattiness` (`claudio commentary
  chattiness`), sets the bar in place of `terse`/`normal`. On 140 real batches the default caught
  every one worth hearing while speaking about a third as often. The batch summary is sent redacted
- `claudio stats` no longer counts a flush that ended in SKIP, a guard rejection or an unusable
  reply as spoken
- Background subagents no longer narrate their tool calls
- **Timed mute and quiet hours** — `claudio off 30m`, plus a recurring `quiet_hours` window

### Speaking up

- **Dangerous commands sound different** — a permission prompt for a destructive command (`rm`,
  force-push, `reset --hard`, `DROP TABLE`, …) is announced as "Careful — this one …" rather than
  the usual line. Local rules, no network; with the Jev decider on, the long tail too, sent redacted
- **Nudge** — escalating, cancellable re-announcements (45s / 2m / 5m, then silence) when a session
  is waiting on the user; typing in that session cancels it via the new UserPromptSubmit hook.
  Only when you are the blocker: an idle input after a finished turn is neither announced nor
  nudged — the turn's own announcement already said it finished.
  On the realtime engines the "waiting on you" line comes from the personality's templates, not the
  model, which mostly lost its meaning
- A question dialog (AskUserQuestion, plan approval) is announced with the personality's question
  lines, not a permission line for a tool called AskUserQuestion or a model guess
- **Watchdog** — notices a session that has gone quiet without finishing, but not one waiting on
  you (a question, plan approval or permission prompt) or a build still inside its own timeout

### New commands

- `claudio recap [minutes]` — pull a spoken summary of recent activity
- `claudio stats` — what was spoken, queued, and suppressed, including nudge/watchdog activity
- `claudio commentary verbosity`, `claudio volume`, `claudio off [duration]`

### Engines and models

- Experimental `pocket-tts` realtime engine (config-only; not in the installer or picker)
- Default Ollama model switched to `llama3.2:3b`, validated by a new A/B harness
  (`scripts/ab_models.py`) against a labelled 60-case corpus; inference params tuned for latency
- TTS daemons stay warm across normal gaps between utterances (idle timeout 180s → 600s), which cut
  fallbacks to macOS `say` from 5.1% of barks to ~1%
- Commentary no longer stays dead after a cold Ollama load
- `per-project playback rate` added behind `project_identity`, but **off by default** — varying
  `afplay -r` resamples pitch and tempo together and sounds robotic

### Project

- Renamed from `claude-bark` to `claudio`; launch infrastructure, community files, remote installer
- pytest suite (now 689 tests, offline and silent — no writes to the developer's real `~/.claude`)
- `settings-merge.py --check` fails on a hook that is out of date, not only one that is missing

## 0.1.0 — 2026-03-16

Initial public release.

### Features

- Three TTS engines: `say` (macOS built-in), `kokoro-realtime` (neural TTS daemon), `chatterbox` (voice cloning)
- Four hook events: Stop, PermissionRequest, Notification, PreToolUse (commentary)
- Local Ollama classification for Stop events (done/broken/question)
- AI-generated contextual phrases via Ollama for realtime engine
- Personality system with four built-in voices: `alfred`, `minimal`, `pirate`, `snarky-dev`
- Configuration presets for one-command setup
- Interactive CLI with arrow-key menus
- Audio caching for instant playback
- `claudio doctor` diagnostics command
- File-based locking and cooldown to prevent audio overlap
