# Hobson

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/macOS-only-black?logo=apple)](https://github.com/1-true-prod/hobson)
[![CI](https://github.com/1-true-prod/hobson/actions/workflows/ci.yml/badge.svg)](https://github.com/1-true-prod/hobson/actions/workflows/ci.yml)

A well-mannered butler for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Hobson tells you, out loud, when Claude has
finished, needs your permission, or is waiting on you — and otherwise knows to keep quiet.

<!-- TODO: Add demo video here (with audio!) -->
<!-- https://github.com/user-attachments/assets/XXXX -->

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/1-true-prod/hobson/main/install-remote.sh | bash
```

The installer asks which voice, personality and events you want. To take the defaults with no
questions (the macOS voice, nothing extra downloaded), add `-s -- --yes`:

```bash
curl -fsSL https://raw.githubusercontent.com/1-true-prod/hobson/main/install-remote.sh | bash -s -- --yes
```

Then start a new Claude Code session. Sessions already running keep the hooks they started with.

### What it changes

- Clones Hobson into `~/.local/share/hobson` and links the `hobson` command into `~/.local/bin`.
- Adds five hooks to `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`). Your other
  settings and hooks stay as they are, the file is backed up before it changes, and a symlinked
  settings file (stow, chezmoi, a dotfiles repo) stays a symlink. The hooks run async, so they never
  hold Claude Code up.
- Keeps its own config and state in `~/.claude/hobson*`.
- Builds the presence sensor (`build/HobsonPresence.app`, in the checkout) and asks once whether
  it may use the camera. Say no and it uses the keyboard, screen lock and calls only.
- Nothing else: no permissions granted, no shell profile edited (the optional uv install, for the
  neural engines, adds itself to your PATH), nothing sent off your machine
  unless you turn on [Jev](#jev-optional).

### Update, pin, remove

```bash
hobson update                 # or re-run the one-liner; your settings are kept
curl -fsSL https://raw.githubusercontent.com/1-true-prod/hobson/main/install-remote.sh \
  | HOBSON_REF=v0.3.0 bash    # pin a release tag (any branch or tag works)
hobson uninstall              # removes hooks, config and state; asks about caches and the checkout
hobson uninstall --yes        # removes all of it
```

### Requirements

- macOS (uses `afplay` and `say`)
- Python 3.9+ and git. The Xcode Command Line Tools provide both: `xcode-select --install`
- Optional: [Ollama](https://ollama.com) with `llama3.2:3b`, for phrases that fit what just
  happened. Without it Hobson speaks from its templates. The installer offers to set it up, or:

```bash
brew install ollama && brew services start ollama && ollama pull llama3.2:3b
```

### From a clone

```bash
git clone https://github.com/1-true-prod/hobson.git
cd hobson
./install.sh          # --yes for no prompts
```

The hooks run straight from that checkout, so edits are live in every new session.

## What it does

Hobson hooks into five Claude Code events — four that can speak, and one that only listens:

- **Stop** — Claude finished working. Output is classified (done/broken/question) via a local Ollama model, and an appropriate phrase is spoken.
- **PermissionRequest** — Claude needs approval to use a tool. A context-aware permission phrase is spoken.
- **Notification** — Claude wants your attention. A notification phrase is spoken, and if the session is waiting on *you*, Hobson will nudge again until you come back.
- **PreToolUse** (commentary) — Claude is working. Tool calls are batched and summarised in one sentence rather than narrated one by one.
- **UserPromptSubmit** — silent. It just tells Hobson you're at the keyboard, which cancels any pending nudge.

## TTS Engines

| Engine | Quality | Setup | Notes |
|--------|---------|-------|-------|
| **say** | Basic | None | macOS built-in, instant, zero deps |
| **kokoro-realtime** | Good | ~120MB models | Local neural TTS via daemon, AI-generated phrases, CPU-friendly |
| **chatterbox** | Excellent | ~2GB models | Voice cloning from reference audio, pre-generated cache, GPU recommended |
| **pocket-tts** | Excellent | manual venv | Experimental. Not in the installer or picker — set `"engine": "pocket-tts"` by hand |

### say (default)

Zero-dependency macOS text-to-speech. No setup, no cache. Good for trying Hobson before committing to a heavier engine.

### kokoro-realtime

[Kokoro ONNX](https://github.com/hexgrad/kokoro-onnx) neural TTS running as a local daemon. Generates phrases on the fly — supports both template-based and AI-generated speech via Ollama. 7 voice options, fast inference. Uses `am_puck` (American male) by default.

### chatterbox

[Chatterbox TTS](https://github.com/resemble-ai/chatterbox) by Resemble AI. Voice cloning from a 5-10 second reference audio clip. Uses a pre-generated cache of ~507 phrases for instant playback. Requires a GPU (MPS on Apple Silicon, CUDA on Linux).

### pocket-tts (experimental)

A second daemon-backed neural engine, shaped like kokoro-realtime. Deliberately left out of the
installer and the `hobson use` picker: enable it by editing `"engine": "pocket-tts"` in
`~/.claude/hobson.json`, and set it up by hand with `venvs/pocket-tts` +
`pip install -r requirements-pocket-tts.txt`. Without that venv every phrase falls back to macOS
`say`, silently.

## Staying out of the way

Voice that talks constantly gets turned off, so most of Hobson's recent work is about saying less:

- **Batched commentary.** Tool calls are queued and summarised in a single sentence once there are
  enough of them (5) or enough time has passed (60s) — not one quip per call. On a real log this
  took the share of tool calls producing speech from 74% down to under 5%.
- **Timed mute and quiet hours.** `hobson off 30m` mutes until it expires; set
  `"quiet_hours": [22, 8]` in config for a recurring window.
- **Subagents stay quiet.** Background agents don't narrate their own tool calls.
- **It notices a stalled session.** If a session goes quiet without finishing, a watchdog says so —
  but not while Claude is waiting on you, and not during a build that is still inside its timeout.
- **Dangerous commands sound dangerous.** A permission prompt for `rm -rf`, a force-push,
  `git reset --hard` or `DROP TABLE` is announced as "Careful — this one deletes files…" instead of
  the usual line. Built-in rules cover the common cases with nothing leaving your machine; with Jev
  on, it also catches the long tail (`prisma migrate reset`, `redis-cli FLUSHALL`, `helm uninstall`).
- **It gets your attention when you're the blocker.** When Claude is waiting on you, Hobson
  re-announces on escalating delays (45s, 2m, 5m) and then stops for good. Only when you really are
  the blocker: a finished task sitting idle isn't nagged about. Typing in that session
  cancels it instantly; the others keep waiting for you.
- **It knows whether anyone is listening.** Away from the desk or on a call, what Hobson would
  have said is held, and when you sit back down you hear one short briefing, most urgent first:
  *"Welcome back. On bank app: careful, this one deletes files. It needs your approval."* Nothing
  happened, nothing said. Lock the screen while a session is waiting on you and he mentions it
  on your way out. See [Presence](#presence).
- **`hobson recap`** — pull a spoken summary of the last 10 minutes when you come back to the desk.
- **`hobson stats`** — see what was spoken, queued, and suppressed, and why.

## Presence

A small helper (`presence/`, built at install with the Command Line Tools) tells Hobson whether
anyone is at the desk. Cheapest signals first:

| Signal | Means |
|---|---|
| Screen locked, or display asleep | away |
| Zoom, Meet in a browser, Slack, Teams, FaceTime… capturing the microphone | on a call |
| Keyboard or mouse touched in the last minute | present |
| The camera, only when he is about to speak to an idle desk (a ~2s look) | present, away, or company |

He notices you come and go: a word on your way out ("I'll hold anything that comes in"), and on
your way back either the briefing or "Welcome back. All quiet for the last 20 minutes." — not after
a lean out of frame, at most one farewell every five minutes, never the same line twice running
(`hobson presence greetings off` for news only). Away or on a call, he holds what he would say;
with someone else in frame, a wait is announced
without its details ("Something needs your attention"). A nudge waits for you instead of talking to
an empty room. Any doubt — no helper, a dark frame, an app he does not know — and he speaks exactly
as he always did.

The camera: frames stay in memory for one detection and are never saved or sent. macOS asks for
permission once, at install (or `hobson presence setup`), never mid-session. Modes:
`hobson presence mode signals` (never the camera), `auto` (the default: a look only before
speaking), `continuous` (a frame a second: notices you leaving within seconds, and company).

**The phone switch.** `hobson presence phone auto` makes an Android phone the camera's switch,
read over adb (USB or Wireless debugging): face up, the camera may be used; face down, it is off,
and Hobson says so. A phone he cannot read counts as face down.

`hobson presence` shows what he knows; `hobson monitor` shows it change; `hobson presence preview
on` floats a small window with the camera feed, a box around each face, and the state he reads
from it (on your screen only; nothing is saved).

Only a human face counts as someone: not a body, not a shape, not anything else that moves. In
`continuous` mode, no face and no keystroke for 30 seconds is away (`presence.away_after`); looking
down at a phone that long counts as leaving. Wave at the camera, with your face in view, and he
answers: the briefing if anything is waiting, otherwise a hello.

## Personalities

| Personality | Style | Templates | AI phrases |
|-------------|-------|-----------|------------|
| **hobson** | The house butler: dry, impeccably mannered | ~507 | Yes |
| **minimal** | Terse, functional | ~50 | No |
| **pirate** | Yarr, matey | ~48 | Yes |
| **snarky-dev** | Sarcastic developer | ~48 | Yes |

Switch with `hobson personality [name]` or create your own by adding a JSON file to `scripts/personalities/<name>/personality.json`.

## Presets

One-shot config appliers that set engine + personality + events in one command:

| Preset | Description |
|--------|-------------|
| **silent-butler** | Hobson speaks only when tasks finish |
| **chatty-butler** | Hobson comments on everything |
| **quick-beep** | Minimal voice via macOS say |
| **pirate-ship** | AI-generated pirate commentary |

Apply with `hobson preset [name]`.

## CLI Reference

```
hobson status              Show engine, personality, events, config
hobson on                  Enable voice notifications
hobson off [duration]      Disable voice, optionally timed: 45, 90s, 30m, 2h
hobson use [engine]        Switch engine (say/kokoro-realtime/chatterbox)
hobson personality [name]  Switch voice personality (interactive picker)
hobson preset [name]       Apply a configuration preset (interactive picker)
hobson events              Configure which events trigger voice (multi-select)
hobson commentary on|off   Toggle running commentary
hobson commentary verbosity [terse|normal|chatty|anomaly]
                            How much commentary to speak
hobson commentary chattiness [0-1]
                            With Jev on: how much commentary it lets through
hobson volume [0-10]       Get or set playback volume
hobson voice [name]        Switch Kokoro voice (interactive picker)
hobson test                Play a test bark
hobson recap [minutes]     Speak a summary of recent activity (default 10m)
hobson presence            Whether anyone is listening, and what is held for you
hobson presence on|off     Hold speech while you're away / always speak
hobson presence mode [signals|auto|continuous]
                            How Hobson knows you're there (camera use)
hobson presence greetings on|off
                            A word when you leave, a welcome when you're back
hobson presence preview on|off
                            A floating window: the feed and what Hobson sees in it
hobson presence phone [auto|off|SERIAL]
                            Android phone face up = camera on, face down = off
hobson presence look|setup Take one look / build the sensor and ask for the camera
hobson lines [category]    Show voice lines from active personality
hobson monitor             Watch bark activity in real time
hobson stats               Show what was spoken, queued, and suppressed
hobson doctor              Run diagnostics
hobson config show|reset   Show or reset configuration
hobson cache-gen [--force] Generate voice cache for current engine
hobson setup kokoro|chatterbox  Install engine venv + download models
hobson daemon start|stop   Manage the TTS daemon
hobson version             Print the version
hobson update              Pull the latest hobson and refresh its hooks
hobson uninstall [--yes]   Remove hobson (--yes: everything, no prompts)
```

## Configuration

Config lives at `~/.claude/hobson.json`:

```json
{
  "engine": "kokoro-realtime",
  "personality": "hobson",
  "events": ["stop", "permission", "notification"],
  "cooldown": 2.0,
  "volume": 3,
  "muted": false,
  "quiet_hours": null,
  "ollama": {
    "model": "llama3.2:3b",
    "url": "http://localhost:11434"
  },
  "kokoro": { "voice": "am_puck", "speed": 1.1, "daemon_port": 19849, "daemon_idle_timeout": 600 },
  "chatterbox": { "device": "mps", "exaggeration": 1.0 },
  "commentary": {
    "cooldown": 8.0,
    "tools": ["Agent", "Edit", "Write", "Bash"],
    "verbosity": "normal",
    "min_tool_calls": 5,
    "min_seconds": 60.0,
    "chattiness": 0.6
  },
  "nudge": { "enabled": true, "delays": [45, 120, 300] },
  "watchdog": { "enabled": true, "minutes": 10 },
  "decider": { "backend": "local" }
}
```

Key options:
- **engine** — `say`, `kokoro-realtime`, `chatterbox`, or `pocket-tts`
- **personality** — which voice personality to use
- **events** — which hook events trigger voice (`stop`, `permission`, `notification`, `commentary`)
- **cooldown** — minimum seconds between barks (prevents rapid-fire)
- **volume** — afplay volume for cached audio (1-10)
- **muted** — disable all barks
- **quiet_hours** — `[start_hour, end_hour]` window of silence; wraps midnight
- **commentary.verbosity** — `terse`, `normal`, `chatty`, or `anomaly` (only the surprising)
- **commentary.min_tool_calls / min_seconds** — how full or how old a batch must be before it speaks
- **commentary.chattiness** — with Jev on, how much of that it lets through: 0 next to nothing, 1 all
- **nudge** — escalating re-announcements when a session is waiting on you; `delays` caps how many
- **watchdog** — speak up if a session has gone quiet for this many minutes without finishing
- **daemon_idle_timeout** — seconds a TTS daemon stays warm; too low and utterances fall back to macOS `say`
- **decider.backend** — `local` (default, nothing leaves your machine) or `jev`; see below

## Ollama

Hobson uses [Ollama](https://ollama.ai) for two things:
1. **Stop classification** — categorizing Claude's output as done/broken/question
2. **Phrase generation** — writing contextual phrases for the realtime engines, summarising a batch
   of tool calls, and building the `hobson recap` summary

Default model: `llama3.2:3b` (~4 GB RAM). In local A/B testing it gave the most reliable
first-person phrasing and Stop classification at this footprint. For a smaller footprint use
`llama3.2:1b`; if you prefer an Apache-2.0-licensed model, `qwen3.5:4b` is a solid alternative
(slightly weaker at classifying "question" Stops). Set whichever you like via `ollama.model`.

Ollama is optional. If it isn't running, Stop events default to "done" category and AI phrase generation falls back to templates.

## Jev (optional)

Hobson stays quiet when a phrase is a near-repeat of something it just said. That check compares
words, so it can't tell "I'm fixing the invoice sync" from "I fixed the invoice sync" — and silences the
second, which is the one you wanted to hear. With `"decider": {"backend": "jev"}`, those rejected
phrases get a second opinion from Jev, TypeSafe AI's fast classifier, through
[OpenRouter](https://openrouter.ai).

- **Deciding what's worth saying.** Once a batch of commentary is ready, Jev judges whether it's
  worth hearing, and anything below `commentary.chattiness` is held back before Ollama is asked to
  phrase it. On 140 real batches it caught every one worth hearing (commits, pushes, a merge, a
  revert) while speaking about a third as often as Ollama alone. `terse` and `normal` stop
  mattering with Jev on; the dial replaces them.
- **Both directions, carefully.** It can let a wrongly rejected phrase through, and it can hold
  back running commentary that just rewords something said in the last two minutes. It never
  silences a "finished", a permission prompt, or a "waiting on you". No key, a timeout, or any
  other failure changes nothing.
- **What is sent:** the phrase in question and up to six phrases Hobson recently spoke. A one-line
  summary of each commentary batch — tool names, file names, and each command's description (or
  its first 40 characters) — such as `3 Bash (Run the test suite); Edit: Invoice.kt (+4/-2 lines)`. And, for a permission prompt on a shell command Hobson's own rules can't place (see
  below), that command. The summary and the command go out with paths, hosts, URLs, emails,
  secrets, tokens, scripts and long quoted text stripped out. No code, file contents, or
  conversation.
- **Key:** put `OPENROUTER_API_KEY=...` in `~/.claude/hobson.env` (`chmod 600`), or export it. It
  is never read from or written to `hobson.json`.
- **Cost:** about $0.00002 per call: one per commentary batch, plus the odd rejection or
  permission prompt. `hobson monitor` shows spend to date.

## Uninstalling

```bash
hobson uninstall
```

Removes Hobson's hooks from `settings.json` (leaving every other hook alone), its config and state,
and the `hobson` command; then asks about voice caches, the log, and the checkout itself.
`hobson uninstall --yes` removes all of it without asking.

## Troubleshooting

- **Silent?** Run `hobson doctor`. It checks the hooks, config, engine, Ollama and the CLI, and
  prints a fix for anything wrong. `hobson test` plays a phrase; `hobson monitor` follows what
  Hobson hears and says, live.
- **Installed, but a session says nothing.** Hooks load when a session starts: open a new one.
- **Reporting a bug:** include the output of `hobson doctor` and `hobson version`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on adding personalities, engines, and more.

## Credits

- [Kokoro ONNX](https://github.com/hexgrad/kokoro-onnx) — Neural TTS engine
- [Chatterbox](https://github.com/resemble-ai/chatterbox) by Resemble AI — Voice cloning TTS
- [Ollama](https://ollama.ai) — Local LLM inference

## License

[MIT](LICENSE)
