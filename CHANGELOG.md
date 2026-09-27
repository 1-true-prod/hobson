# Changelog

## Unreleased

### New

- **The setup wizard.** Installing opens a window that walks through every choice: the engine,
  its voice and personality (each heard before anything is downloaded), when Hobson speaks, the
  Ollama model (any model; the default is marked recommended), Jev, presence and the phone switch.
  **Express** takes a loadout surveyed for this Mac. Nothing is written or installed until COMMIT,
  which first lists exactly what will change and what it will download, and closing the window
  before then changes nothing. Only settings that differ from the defaults are written.
  `hobson setup` opens it again. Over SSH or with `--yes` the installer writes the defaults and
  installs nothing heavy, as before
- **Jev is asked, not assumed**: with an OpenRouter key (found, or pasted in), the wizard tests it
  with one real call and turns Jev on only if that call works. The key is kept in
  `~/.claude/hobson.env`, never in the config
- **Pocket TTS can be installed from the wizard** (or `hobson setup pocket-tts`), about 1 GB
- **Female voices**: the wizard offers several Kokoro and Pocket TTS voices, men and women, as
  recorded samples
- After an update, the installer names any setup sections this install has not seen, and
  re-running it at the Mac offers to open the wizard for them
- **Presence: Hobson knows whether anyone is listening.** A small Swift helper reads the
  keyboard, screen lock, display sleep and calls (a known call app capturing the microphone), and
  takes a two-second look through the camera only when he is about to speak to an idle desk. Away
  or on a call, what he would say is held; when you are back you hear one briefing, most urgent
  first, and nothing if nothing happened. Lock the screen while a session waits on you and he says
  so on your way out. With company in frame, a wait is announced without its details. A nudge
  waits for you rather than spending its three tries on an empty room; the watchdog's line is held.
  On by default; `hobson presence off` turns it off, `hobson presence mode` picks the camera use
  (`signals`, `auto`, `continuous`), and anything it cannot tell leaves Hobson as he was
- **Hobson notices you come and go**: a farewell when you leave, and on your return the briefing
  or "Welcome back. All quiet for the last 20 minutes." Guarded: no greeting after under 20
  seconds away, one farewell per five minutes, never the same line twice running.
  `hobson presence greetings off` keeps him to news only. In `continuous` mode the camera sees
  you walk away; otherwise leaving is a screen lock
- **The phone switch**: `hobson presence phone auto` makes an Android phone the camera's switch,
  read over adb. Face up the camera may be used, face down it is off, and Hobson says which. A
  phone that cannot be read counts as face down
- `hobson presence`, and presence in `hobson status`, `hobson doctor` and `hobson monitor`
- `hobson presence preview on`: a floating window with the camera feed, a box around each face,
  and the state the sensor reads from them
- **Wave at Hobson** (`continuous` mode): a raised hand swung side to side, with your face in view,
  gets an answer — the briefing if anything is waiting, otherwise a hello
- **A menu-bar icon** while presence runs: what the sensor sees, **Show Preview** to show or hide
  the camera window, and **Presence On** to pause it (camera off, nothing sensed, Hobson speaks as
  with presence off) and resume it. `hobson presence on` resumes a pause too
- Only a human face counts as a person: bodies and anything else that moves never trigger an
  arrival, a departure, company or a wave. No face and no keystroke for 30 seconds is away

### Fixes

- `hobson recap` speaks again. Since the log started carrying dates it found no activity at all;
  it now reads dated lines, and says each phrase once rather than three times
- Hooks that overlap no longer lose each other's session state. A commentary flush or a Stop saved
  the copy it loaded before calling Ollama, discarding tool calls queued, phrases spoken and turns
  classified meanwhile; with eight hooks writing at once, 79 of 200 writes went missing. Session
  state is now written in short locked transactions, atomically
- A config file that is not a JSON object falls back to the defaults instead of stopping every hook
- `hobson recap` reads phrases with an apostrophe. A phrase is logged as its `repr()`, which puts
  "I'm …" in double quotes, and recap read single quotes only: it missed 53% of everything spoken
  and summarised the batch descriptions instead
- `hobson stats` counted any line containing the word "barked" as spoken, including a trace that
  quoted it; it now counts played phrases only
- A log message with a line break (a Bash command with no description) is written on one line.
  It used to spill onto lines no reader could place; those are now read back as part of their line
- `log_analyse.py` says it cannot read a missing log instead of raising

### Changes

- **Hobson is now a synthetic butler**: a courteous machine intelligence, precise and faintly
  uncanny. The README, the CLI and the hobson personality's description say so; the phrase
  templates are unchanged
- The installer no longer walks through choices in the terminal, installs no engine or model
  itself, and leaves the camera prompt to the wizard
- **say and chatterbox read a Stop the way the realtime engines do**: the same condensed last
  message for "waiting on you" and "still working", and a "broken" whose last message names no
  failure counts as done. Stats count their "still working" Stops too
- `hobson stats` no longer lists "going in circles": that detector was removed in 0.2.0
- Internally, kokoro-realtime and pocket-tts are one daemon engine with a spec each, and a Stop is
  read once, in `stop_outcome.py`, for every engine
- `hobson monitor` shows the engine on lines written before project tags, and no longer takes
  `[nudge]` for an engine
- The prompt hook no longer loads the engine module: 39 ms to 33 ms median per prompt
- Internally, every path, the config, the project label and the quiet controls live in `home.py`,
  resolved from `$HOME` when asked for, and every log line is written and read by `log_record.py`

### Removed

- The takeover of installs from before 0.3.0: their old entrypoint, command, checkout path and
  state names are no longer recognised. If you installed before 0.3.0, re-run the one-liner

## 0.3.0 — 2026-09-26

### Hobson

A well-mannered butler, and a name of its own.

- The command is `hobson`, the checkout lives in `~/.local/share/hobson`, and state is
  `~/.claude/hobson*`. Environment variables are `HOBSON_REF`, `HOBSON_DIR`, `HOBSON_REPO`,
  `HOBSON_YES`, `HOBSON_PYTHON`
- The default personality is **hobson** (it was **alfred**, billed as Batman's butler); a config
  naming `alfred` reads as `hobson`, and chatterbox still finds `models/alfred-reference.*`

### Install

- Installing a pinned release (`HOBSON_REF=v0.3.0`) no longer prints git's "is not a commit!"
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
  leaves an unchanged `settings.json` alone. New: `hobson update`, and `HOBSON_REF` to pin a
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
- Hobson's hooks are recognised by their entrypoint, not by a word in a command, which claimed
  (and on uninstall deleted) the hooks of anyone whose home directory had that name
- A pyenv / asdf / mise shim is resolved to the Python behind it before it is pinned; `doctor`
  reports a pinned interpreter that has since been uninstalled
- `hobson uninstall [--yes]` stops both daemons and any pending nudge, removes all state files
  (not just five of them), and can delete the installer-managed checkout
- Fixed under Homebrew's bash 5: the installer's menus exited on the first down-arrow and
  `hobson doctor` stopped at its first finding (`((x++))` from 0 fails under `set -e` there).
  Also: menus under `TERM=dumb`, `doctor` for chatterbox users with no cache yet, and
  `hobson daemon start` for pocket-tts on bash 3.2, which all exited early
- Updating refuses a `HOBSON_DIR` that is not a Hobson checkout, and a checkout with commits
  that were never pushed, instead of resetting them away
- `hobson doctor` names the model Hobson actually uses (it suggested pulling `qwen3.5:4b`), shows
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
  only the phrase and up to six recent ones; the key lives in `~/.claude/hobson.env`
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
  is scored before Ollama is asked, and one dial, `commentary.chattiness` (`hobson commentary
  chattiness`), sets the bar in place of `terse`/`normal`. On 140 real batches the default caught
  every one worth hearing while speaking about a third as often. The batch summary is sent redacted
- `hobson stats` no longer counts a flush that ended in SKIP, a guard rejection or an unusable
  reply as spoken
- Background subagents no longer narrate their tool calls
- **Timed mute and quiet hours** — `hobson off 30m`, plus a recurring `quiet_hours` window

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

- `hobson recap [minutes]` — pull a spoken summary of recent activity
- `hobson stats` — what was spoken, queued, and suppressed, including nudge/watchdog activity
- `hobson commentary verbosity`, `hobson volume`, `hobson off [duration]`

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

- Renamed from `claude-bark`; launch infrastructure, community files, remote installer
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
- `hobson doctor` diagnostics command
- File-based locking and cooldown to prevent audio overlap
