# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Hobson is a synthetic butler for Claude Code: he says, out loud, when Claude has finished, needs
permission, or is waiting on you, and otherwise keeps a discreet silence. It hooks into five events (PermissionRequest, Stop, Notification, PreToolUse, UserPromptSubmit), classifies the output via a local Ollama model, selects or generates a personality-driven phrase, and plays it via one of four TTS engines.

**The persona** is a courteous machine intelligence with a butler's manners: precise, unflappable,
formal (no contractions), faintly uncanny, dry rather than jokey. The reference is the AI concierge
archetype, Delamain in Cyberpunk 2077 above all. That is a brief for the tone, not material: his
name and his lines stay out of anything shipped. Descriptions and the wizard's lines carry it; the
507 phrase templates predate it, and rewriting them would invalidate every chatterbox cache.

## Architecture

Four engines, two modes:

**Static engines** (say, chatterbox) use pre-written templates:
```
hobson.py (entrypoint, reads stdin JSON from Claude Code hooks)
    -> load_config() from ~/.claude/hobson.json
    -> _is_event_enabled() check against config "events" list
    -> load_engine() via dynamic import
    -> engine.run(hook_input)
        -> PermissionRequest: pick_permission(tool_name)
        -> Notification: pick_notification()
        -> Stop: BaseEngine._handle_stop() -- stop_outcome.read(), then classify() via
           Ollama (configurable model) -> StopReading.settle() -> pick(done/broken/question)
    -> engine.try_bark(phrase)
        -> cached audio? afplay : macOS say + background backfill
```

**Daemon engines** (kokoro-realtime, pocket-tts) generate phrases live via Ollama + daemon TTS:
```
hobson.py -> DaemonEngine.run(hook_input)
    -> _is_event_enabled() check against config "events" list
    -> Stop: BaseEngine._handle_stop() -- stop_outcome.read(), then
       phrase_gen.generate_or_skip("Stop", reading.context, stop=reading)
    -> PermissionRequest / Notification: phrase_gen.generate_or_skip(event, detail)
       via Ollama (configurable model) -- one call decides and phrases; detail from
       _describe_event(). Except fixed-meaning events, spoken from templates
       and never the model (_fixed_announcement): a waiting-on-you Notification
       (idle_prompt / agent_needs_input), a question dialog (AskUserQuestion /
       ExitPlanMode arrive as PermissionRequest), and a destructive permission request
    -> PreToolUse: BaseEngine._handle_commentary() (batched; see below)
    -> _speak_live(phrase)
        -> daemon generate -> afplay : macOS say fallback
```

The two are one engine, `DaemonEngine` in `engines/daemon.py`. Each backend is a `DaemonSpec`
(`kokoro_realtime.py`, `pocket_tts_realtime.py`): its config section, venv, daemon script, playback
file, startup polls, generate timeout, and which options go to the daemon's CLI and which into every
request. They were two copies until a fix to one kept missing the other.

**Experimental realtime engine** (`pocket-tts`) follows the same live-only shape as
`kokoro-realtime` — a persistent HTTP daemon (`scripts/pocket-tts-daemon.py`, default port
19850) keeps a Pocket TTS model resident and returns WAV bytes per phrase; no pre-cached audio.
It differs from kokoro in three ways worth knowing:

- **Voice states are cached per voice** (`get_state_for_audio_prompt`), warmed at startup for
  the default voice (`charles`).
- **Output is peak-normalized to 0.95** in the daemon — Pocket TTS emits float audio well below
  full scale and is otherwise near-inaudible at normal system volume.
- **The daemon self-terminates** after `pocket_tts.daemon_idle_timeout` seconds (default **600**).
  A cold model load is heavier than Kokoro's, so engine-side startup allows up to 20s. The 600s
  figure is measured, not guessed: commentary passes `allow_cold_start=False` (it will not block a
  hook for a 20s model load), so every utterance arriving after the daemon has idled out goes out
  via macOS `say` instead. At the old 180s default, 5.1% of real barks fell back that way; the
  gap distribution puts 600s at ~1.1%, which is where the curve flattens. The daemon engine takes
  every default from its `DEFAULT_CONFIG` section, so none can drift.

The setup wizard offers it as a fourth engine card and installs it (`setup_wizard.install_pocket`:
`venvs/pocket-tts` with Python 3.13 via uv, `requirements-pocket-tts.txt`, then a warm load that
fetches the ~240 MB of weights — about 1 GB in all); `hobson setup pocket-tts` runs the same
install without the window. Express never recommends it: it is heavier and slower to wake than
Kokoro. `hobson use` still does not offer it. Without that venv the engine loads but every phrase
falls back to macOS `say`, silently and permanently — which is why the wizard marks an engine
NOT INSTALLED (`engine_ready`: for pocket-tts, the venv *and* the weights in the Hugging Face
cache) and installs it on COMMIT.

**What the model is given** (`stop_outcome.py`, `session_state.build_session_context`): the event line and
a session block. A **Stop's** event line is `Earlier: … | …` (up to three prior turns, 200 characters
each) then `Last message: …` (500), from `last_transcript_context`: shown as four equal turns, the
model reported an earlier turn's work as this one's. Only what the user *typed* is a user turn: skill
bodies, subagent hand-backs, `<task-notification>`s, command echoes and compaction summaries are not
(`_user_prompt_text`; recent transcripts mark typed prompts `origin.kind == "human"`). Agent text is
read as prose (`_prose`: no code blocks, URLs or trailing source lists), and `condense_turn` keeps a
turn's ending — the verdict is there. For a Stop only, the session block leads with `Task:`, the
user's latest request of three words or more (`request_for_prompt`, read from the tail of this
hook's own `transcript_path`, never `find_transcript`'s any-project fallback). `phrase_gen.NUM_CTX`
is 2048: at 1024 a long Stop already overflowed, and Ollama drops the earliest message first — the
rules. `_chat` logs `prompt at the context limit` when a prompt reaches the edge.

**Do not give commentary the Task line.** Replayed on 80 real commentary batches and blind-judged, the
old prompt beat it 36–25: the model spoke the request as the action ("I'm opening a report for you").
Commentary, permission and notification prompts are byte-identical to before the Stop work.

**A Stop's category is not the model's alone** — the model called 45 of 46 question-ending Stops
"done", and 26 "broken" against 2 real ones, and a non-"done" Stop starts a nudge. `stop_outcome.read()`
reads the transcript once into a `StopReading`, and its `settle()` holds every rule below, for both
phrase sources (`classify` on the static engines, `generate_or_skip(stop=…)` on the daemon engines):

- `awaits_developer` reads the last four sentences for a wait: a question, or a marker such as
  "waiting for your", "say go", "annotate what", "you must decide". If it finds one the category is
  `question` (`awaiting_answer=True`), and the model is told. An offer after finished work ("Want me
  to draft the Slack reply?") is **not** a wait: counted as one, it started nudges for work nobody
  asked for.
- A "broken" whose last message names no failure (`failure_reported`) is retried with that said
  (`doubts()`; daemon engines only, which have a second attempt), and demoted to "done" if it comes
  back broken.
- A SKIP after a clear check (`awaiting_answer=False`) is "done"; a failed call stays unknown.
- Stops run at `STOP_TEMPERATURE` 0.3 (commentary stays at 0.7, for variety past the dedup guard).
  Because a low temperature repeats itself, a Stop's second attempt is shown its first reply and why
  it was rejected (`_retry_turn`) — including a reply over `MAX_WORDS`, which used to go silent
  with no retry at all.

Measured on hand-labelled real Stops — 101 tuned on, 100 **held out** (labels by an agent that saw no
model output; the replay harness is not in the repo). Held out: category right 57% → 77%, wrongful
nudges 30 → 11 of 142, missed 43 → 34 of 58, false "broken" 25 → 7, silent 16 → 5. The spoken
phrases, blind-judged on the same held-out set: new 51, old 29, ties 20; judged wrong 86 vs 101 of 200. The detector
catches about two thirds of real waits; what it misses mostly has no marker at all ("The RAM bump was
still worth keeping regardless."). Labellers disagree on offers, so treat its precision as ~90%, not
the 49/52 measured.

**A Stop that waits on the agent's own work is not announced** (`works_on_its_own`,
`StopReading.still_working`). A coordinator dispatching subagents ends a turn after every launch and every
hand-back, and each one was announced as a finish ("Rendering hard-case pairs complete", four times in
five minutes, while the runs were still going), which buried the one real finish. When the last
message says it is waiting on its own runs, reviewers, build or workers ("Compile running.", "Once it
lands I'll…", "Heartbeats only. Waiter alive. Nothing to act on.") and nothing is asked of the
developer, the Stop is logged as `[Stop] still working`, spoken by nobody, and recorded as
`last_stop_category = "working"` — which `_idle_after_finished_turn` treats like `done`, so the idle
prompt after it nudges nobody either. What counts as running is deliberately narrow: an emulator or a
watcher is "still running" long after the agent stopped waiting on it, "waiting for review" is other
people, and Plannotator, "let me know when it finishes" or "holding on your nod" is a wait on you
(`_YOUR_TURN`). Any question in the closing sentences vetoes it, offers included: "Want me to dispatch
it now?" starts no nudge, but it is not a turn to keep quiet about. A wrong flag is the costly error —
a session blocked on you goes silent, with no nudge — so every alternative says whose work it is.
Hand-labelled, 380 real Stops, 88 of them this kind, labelled by agents that never saw the detector:
held out, 30 of 30 flags right and 30 of 48 caught. A blind audit of the 135 other Stops it flagged
found 125 right and 10 waiting on you; the vetoes above were written from those 10 and leave 3, each
with its ask before the closing sentences it reads (a choice of options, a manual transfer, "export
them first"). 11% of all Stops are flagged. A structural check — background agents launched in the transcript and not yet handed back —
was measured too and dropped: it added precision only by losing most of the recall, and it cannot see
workers run outside Claude Code.

**Engine subclass pattern**: `BaseEngine` in `engines/base.py` handles all shared logic (event filtering, lock/cooldown, template selection with cache-preference, commentary, playback); config and paths come from `home.py`, and every log line goes through `log_record.py`. Static subclasses set `templates_module`, `cache_dir`, `cache_ext` and implement `backfill(text)`. Every engine's Stop is `BaseEngine._handle_stop()`; only its phrase source differs (`_stop_phrase` / `_speak_stop`: templates for the static engines, the model for `DaemonEngine`). `DaemonEngine` overrides `run()` for PermissionRequest and Notification.

**Event filtering**: The top-level `events` config key controls which hook events trigger voice. `BaseEngine._is_event_enabled()` maps hook event names to config keys (`Stop`->`stop`, `PermissionRequest`->`permission`, `Notification`->`notification`, `PreToolUse`->`commentary`). All engine `run()` methods call this at the top. `UserPromptSubmit` has no `events` key — it is intercepted in `hobson.py` and never reaches an engine.

**Commentary abstraction**: PreToolUse handling lives in `BaseEngine._handle_commentary()` and is shared by all engines. Three verbosity modes (`commentary.verbosity`):
- `terse` — LLM-gated with SKIP-heavy bias; only voice notable events
- `normal` (default) — LLM-gated with relaxed bias; voice meaningful actions, skip only pure noise
- `chatty` — no LLM call; pick a template from `COMMENTARY_TEMPLATES` keyed by tool name
- `anomaly` — count/timer gates ignored entirely; speaks only when `anomaly_reason()` finds
  something surprising (the same action again, or a context touched for the first time). It
  is the only reader of the fingerprint ring: repetition is judged on `action_identity()`,
  the call's full input minus its `description`, not on the short display context

With `decider.backend` `"jev"`, `terse` and `normal` stop differing: whether a released batch is
spoken is the decider's call (see Batching), and Ollama only phrases it.

The seam engines override for everything spoken outside a Stop is `speak_dynamic(phrase, allow_cold_start)`. Static engines (`say`, `chatterbox`) inherit the default (macOS `say` with bark-lock gating); the daemon engines override it to route through their daemon. This means commentary works on every engine without duplicating the gating logic.

**Batching (the attention gate)**: terse/normal commentary does *not* speak per tool call.
`_handle_commentary_llm()` appends every call to a per-project pending queue and flushes — one
Ollama call, one utterance summarising the whole batch — only once `should_flush()` opens the gate
(`commentary.min_tool_calls`, default 5, **or** `commentary.min_seconds`, default 60). Appending
must always succeed, so the commentary lock is acquired at flush time only, never at append —
otherwise the lock would silently drop calls out of the batch instead of merely delaying them.
Two things override the gate:

- **Stop pre-empts**: a finished turn drops any queued mid-work commentary
  (`_drop_pending_on_stop`) before classifying the assistant's message.
- **Staleness**: `take_pending(stale_seconds=...)` discards items older than
  `commentary.pending_stale_seconds` (90) — a flush that finds only stale items speaks nothing.

**Worth hearing?** (`scripts/gate.py`, decider `"jev"` only): the gate decides when a batch is
*considered*; the decider then scores P(worth) for the batch summary, once per flush, and the batch
is spoken only at P ≥ 1 − `commentary.chattiness` (default 0.6, so 0.4). Below it, the batch is
held back with no Ollama call; above it, Ollama phrases it with the `"decided"` prompt, which has
no SKIP — left one, it declined 2 of the 7 batches the calibration rated worth hearing — and says
the batch has not run, so nothing has failed (over 36 generations, invented failures 12 → 5). No opinion
(no key, timeout) leaves the old verbosity bias in charge. Calibrated on 140 real logged batches
(80, then a held-out 60), labelled by the user's written rule — skip routine events, voice
completions, errors, questions: Ollama spoke on 106 and caught 5 of 7; P ≥ 0.4 spoke on about a
third as many and caught all 7 (lowest 0.46 and 0.49), AUC 0.91 on both sets. Its false positives
lean towards starting big work (dispatching an agent, booting an emulator). A PreToolUse batch says
what the agent is about to do, never how it went, so failures cannot show up here — Stop carries
those. The summary goes out through `risk.redact()`, at most 220 characters, with an Edit's
`(+3/-2 lines)` shielded from the path rule.

**Commentary must be about its batch** (`phrase_gen._off_batch_reason`, in the guard). Blind-labelled,
130 of 399 spoken commentary phrases did not describe the batch they were spoken for: the model
reworded something said a minute ago ("I'm committing the updated CLAUDE.md" for a batch building a
labelling set, spoken twice at P(worth) 0.77), spoke the branch name as the action ("I'm widening the
search filters" for a `cd`), or invented trouble for work that had not run ("I'm hitting a merge
conflict" for an `ls`). The guard rejects a PreToolUse phrase that shares no content word with the
batch summary (four-letter stems, camelCase and file extensions split, so `InvoiceService.kt` meets "the
invoice service"), or that claims a failure the summary does not mention. Together: 92 of the 130
caught, 6 good phrases lost — 96% and 91% precision on the two halves ("error handling" and its
kin describe code, not a failure, and do not count). It holds back about a quarter
of what commentary used to say, nearly all of it false. It runs before the duplicate guard, so a
rejected phrase costs no decider call. The summary keeps only the *last* call's context per tool
("5 Bash (Push and confirm PR state)"), which is part of why the model has so little to go on.

Measured effect on a real log: 28,345 PreToolUse events produced 1,297 flushes (4.6%), against a
simulated gate ceiling of 26% and a pre-batching baseline of 74%.

Repetition does **not** override the gate. A stuck detector ("going in circles") used to force a
flush when one action repeated three times; replayed over 12,805 real tool calls it caught nothing
that was genuinely a loop, and all 56 episodes it had announced ended with the agent finishing on
its own. It was removed (c21f0b5). The replay, `scripts/stuck_replay.py`, is in history at
433abef if the question is reopened — do not bring the detector back without re-running it.

**Session state** (`scripts/session_state.py`): per-project rolling memory keyed by project label
(ties to worktree/cwd, survives session restarts) at `~/.claude/hobson-sessions/<hash>.json`.
Holds the pending queue, the fingerprint ring (anomaly mode only), recently-voiced phrases (for dedup and prompt
continuity), `last_event_time`, `last_stop_time` and `last_stop_category`, each with one writer
(`mark_event`, `begin_stop`, `record_stop_category`).

Every write goes through `transaction(project)`: a per-project `flock` on `<hash>.lock`, a load, and
one atomic save (temp file + `os.replace`); an exception inside saves nothing. Hooks overlap — they
are all async — and each used to load the state, wait on a model for 0.3–20s, and save its stale copy
over whatever the others had written: eight processes appending 25 calls each kept 121 of 200. So
a transaction holds only dict work: do the model call, TTS or transcript read between two of them,
and apply the result in the second. A lock busy past `LOCK_TIMEOUT_SECONDS` (3s) is logged and
skipped rather than waited on. Readers (the watchdog, the idle-prompt check) use `load_session()`;
atomic saves mean they never see half a file.

**Nudge and watchdog** (`scripts/nudge.py`): a short-lived **detached** process, spawned the same
way `afplay` is so it survives the hook exiting. A repeat-until-acknowledged feature is the fastest
route to the user disabling Hobson entirely, so the design rules are not to be relaxed: escalating
gaps (`nudge.delays`, default 45/120/300s), a hard cap of that many re-announcements then silence
forever, instant cancel via the activity token, `silence_reason()` re-checked before every
utterance, one nudge per project (lock file), and never the same sentence twice. A nudge means
exactly one thing — this session is waiting on you — so there is no "stuck" nudge (see the note on
`_WAITING_PHRASES`). It is started by an `idle_prompt` / `agent_needs_input` Notification, which
only reaches Hobson if the Notification hook's matcher lists those types (see constraints). `--watchdog` is the Task 9 hang detector: it notices a session where nothing has
moved for `watchdog.minutes` (default 10). "Moved" means any tool call or permission request,
main session or subagent, which the entrypoint records in a per-project liveness file
(`nudge.record_alive`) — not just the tools commentary narrates. It stays quiet while the session
is waiting on the user (AskUserQuestion, ExitPlanMode, a permission prompt) and gives a Bash call at
least its own timeout. The decision is `nudge.watchdog_verdict()`, pure and tested; on a replay of
508 real sessions it fires 13 times where the old check fired 53, 6 of those real stalls.

A nudge starts only when the last turn left the user as the blocker: an `idle_prompt` after a Stop
classified `done` is neither announced nor nudged (it fires on every idle input, including after
finished work, and the Stop already said so) — one gate, `_idle_after_finished_turn()`. Nor is one
after a Stop recorded `working`: the agent's own subagents or build will wake it.
`last_stop_category` in session state holds the raw classification, `None` when unknown — unknown
still nudges.

**Presence** (`scripts/presence.py`; the sensor is `presence/main.swift`, built by
`scripts/build-presence.sh` into `build/HobsonPresence.app`): whether anyone is listening. The
helper is the only sensor and holds no policy. About once a second it writes
`~/.claude/hobson-presence.json` — `present`, `away`, `call` or `company`, with its source — and on
every change of state runs `presence.py --transition FROM TO --source SRC --away-for N`, detached.
Signals, cheapest first: screen lock or another console user (away), display asleep (away), a
*known call app* capturing the microphone (call — Core Audio's per-process objects, macOS 14.2+),
keyboard/mouse within `presence.idle_seconds` (present), then the camera by `presence.mode`:
`signals` never; `auto` (default) one ~2s look only when a hook is about to speak and you have been
idle (`presence._look` sends SIGUSR1); `continuous` a frame a second, which alone sees company
and a departure within seconds. Python reads the file: missing, stale (>5s) or malformed is
`unknown`, and **unknown behaves exactly as Hobson did before presence** — every failure lands there.

Every speak seam — `try_bark`, `speak_dynamic`, `DaemonEngine._speak_live` — calls
`_for_audience(phrase, kind)` → `presence.route`, once, so no engine or event can talk past an
empty room. `kind` is what the phrase is about (`stop_kind`/`event_kind` in `engines/base.py`):
away or call **holds** it on the salver (`~/.claude/hobson-held.json`, global, flocked, capped,
12h), except commentary, which is **dropped** (stale by your return) and never triggers a look;
company speaks a wait as `COMPANY_LINE` and holds the rest. `briefing` and `nudge` are never held.
It gates at the seam, not in `hobson.py`, on purpose: a Stop is still read and classified while
you are away, because the nudge gate and the briefing need `last_stop_category`. Presence is not
folded into `silence_reason()`: silence drops, away holds, and the log says which.

On return (away/call/company → present) one briefing, most urgent first (`URGENCY`), two told and
the rest counted, a session's repeated waits collapsed, sessions still waiting on you named
(`waiting_on_you`: a running nudge, an unanswered question Stop, a fresh permission request) —
those only after `MIN_AWAY_FOR_WAITS`. Leaving (a lock, or the camera in continuous mode — a look
in auto mode notices too late) says "Before you go — X is waiting on you" if something is.
With `presence.greetings` (default on — the user asked for Hobson to *always* react to them
coming and going) a return from away with nothing to tell is greeted ("Welcome back. All quiet
for the last 20 minutes.") and a departure with nothing waiting gets a farewell; the guards are
the point, since a voice that remarks on every movement gets switched off: no greeting under
`MIN_AWAY_FOR_GREETING` (20s, counted from when the camera last saw you), one farewell per `FAREWELL_EVERY` (300s), never the same line twice
running (`hobson-presence-lines.json`). The end of a call or of company is not an arrival: news
only. A nudge pauses while away without spending a step, with a
4h ceiling (`nudge.NUDGE_MAX_SECONDS`); the watchdog's line is held like anything else.

The phone switch (`presence.phone`, "auto" or an adb serial prefix): the helper reads the newest
accelerometer sample from `adb shell dumpsys sensorservice` (Android keeps it sampled for its own
`FaceDownDetector`) every 2–3s: z ≥ +7 face up (camera allowed), ≤ −7 face down (off, capture
stops), between keeps the last position. **A phone that cannot be read counts as face down** — a
privacy switch fails closed. A flip runs `presence.py --switch up|down`, which says "Camera on." /
"Camera off.". The helper publishes `camera_allowed`, and `needs_look` never asks for a look it
would refuse.

`presence.preview` (`hobson presence preview on`, `--preview`) floats an NSPanel over everything:
the feed (an `AVCaptureVideoPreviewLayer` on the sensor's own session, mirrored), a box on each
face, a dot on each fingertip of a raised hand, and a strip with the state (a thick border for 2s
after a wave). It is drawn on the *picture* inside
the panel, not the panel: the built-in camera delivers 1920×1080 frames whatever preset is asked
for, so a 4:3 mapping put the boxes below the video. A sensor launched with `open -j` starts
hidden, so with the preview it unhides itself (without taking focus) and is launched without `-j`.
Changing `mode`, `phone` or `preview` through the CLI restarts the sensor at once.

The menu bar (`MenuBar`, `Controls` in `main.swift`): while the sensor runs it has an icon (an SF
Symbol per state; `eye.slash` paused) and a menu: the state, **Show Preview** and **Presence On**.
Show Preview shows or hides the window in place, and closing the window unticks it. Unticking
Presence *pauses* it (`presence.paused`, `--paused`): the camera off, nothing sensed, the state
file `"off"` (no audience, so Hobson speaks as with presence off), and the icon kept so you can
resume. `hobson presence off` is different: nothing runs. `hobson presence on` resumes a pause
too. A toggle runs `presence.py --set KEY VALUE --keep-running` and **waits for it before the
sensor changes**, then rewrites the state file at once: `ensure_running` restarts a sensor whose
`mode`, `phone`, `preview` or `paused` disagree with the config, and a hook landing between the
two would have. It treats any fresh record as a running sensor (`helper_alive`), paused included;
gated on a known audience, it `open`ed a paused sensor every 15s. Paused, the sensor still exits
after `exit_after` without a hook, and the next hook starts it paused. Verified live: toggling the
preview and pausing from the menu left the same pid and no `sensor started` line. The icon
appears for everyone with presence on (the default), which is also how the camera-capable helper
shows itself.

Waves (continuous mode only; `WaveDetector`): 15 times a second the newest frame goes through
Vision's hand pose, on its own queue. A wave is a raised, open hand (three fingertips above the
wrist) swinging side to side: three reversals of at least 2.5% of the frame's width inside 2.5s,
**with a face in that same frame**, then 3s before another counts. The first swing is measured
from the extremes seen so far, not the first sample: from the first sample, a small far-away wave
(±2% of the width) never started. Each hand has its own track (`follow`: a hand joins the track
nearest it within `maxStep`, 0.15 of the width, or starts one), and its x is the centre of every
joint seen. Both matter: live, two still raised hands 0.3 apart were read as one hand swinging
between them whenever the detector caught one then the other, and Hobson answered a "wave" every
8s for 90s; and the fingertips alone come and go, moving a still hand's x by 0.023, where the
centre of its joints moved 0.007 over 10s. Only a hand in the current frame can wave. On
synthetic tracks a ±4%, a ±2% and a ±10% wave count, beside a still hand too; a reach, a still
hand, a slow drift and two still hands in any alternation do not (`swingCount`, `follow`: pure). It runs `presence.py --wave`, which
answers with the briefing if anything is held or waiting on you, else one of `WAVE_HELLOS`; one
answer per `WAVE_EVERY` (8s).

Camera permission belongs to the bundle only because it is launched with `open` (a binary run
from a hook would borrow the terminal's grant). Its designated requirement is the bundle id alone,
so a rebuild keeps the grant (verified). The prompt appears only with `--request-permission`
(installer, `hobson presence setup`), never mid-session. Frames never leave the helper or touch
disk; dark frames (mean luma < 12) count as "can't see", never "nobody", and an empty look is
taken twice before it means away. **A person is a human face, and nothing else** — the user's
rule: bodies, outlines and anything else that moves do not count, for arrival, leaving, company or
a wave. It was not always: on 30 frames of someone seated at a screen-lit desk looking down, faces
and upper-body rectangles found 0, full-body rectangles and person segmentation 30, and on faces
alone the sensor lost a seated person for 15–20s at a time and flapped at the old 8s grace. So the
grace is what carries it now: `presence.away_after` is 30s, and a keystroke within it also keeps
you present. What that costs: reading with your head down for over 30s without typing is away (a
farewell, speech held, a "Welcome back" when your face returns), and in `auto` a look at a
head-down reader says away. A face on a monitor or a poster counts as a person. Company is two
confident, non-overlapping faces (IoU ≤ 0.2) in 5 frames running. Built on the first real machine: OBS held the microphone all
day, which is why "call" is a list of call apps and not "the mic is in use".

**Quiet controls**: `silence_reason(config)` in `home.py` is the single gate, called by
`hobson.py` before any engine is loaded. It returns a reason string for: `muted`, a timed mute
(`mute_until`, an epoch set by `hobson off 30m`), or `quiet_hours` (`[start, end]` hours, wrapping
midnight). Everything downstream — including an in-flight nudge — checks it.

**UserPromptSubmit**: handled in `hobson.py` *before* config or engine work — it writes this
project's activity token (`nudge.activity_path()`, `~/.claude/hobson-activity-<key>`) and
returns. It never speaks; its only job is to cancel nudges the moment the user starts typing. Per
project on purpose: a single global token meant typing in one tab silenced every other session
that was waiting on you.

**Decider (Jev)** (`scripts/decider.py`): an optional remote classifier — Jev, via OpenRouter's
Decisions API — behind one seam with three question shapes (`choice`, `score`, `noul`).
`decider.backend` is `"local"` by default, which makes no network call and returns "no opinion".
Every failure (no key, timeout, bad response) is logged once and fails closed to the local result.
One question, `phrase_gen._p_restates()` — does this phrase restate one just spoken? — used two
ways. **Rescue**: when the near-duplicate guard rejects a phrase, release it if
P(restates) < `decider.dedup_restates_max` (0.5); its target is the tense transition Jaccard cannot
see, "I fixed the invoice sync" after "I'm fixing the invoice sync". **Block**: when the guard passes a
*commentary* phrase, silence it if P(restates) ≥ `decider.dedup_block_min` (0.7), which catches
the paraphrase Jaccard misses. The block is commentary-only — a Stop is also the turn-ended
signal, and in calibration one of its two high scores was a completion — compares only against
phrases from the last 120s with a known timestamp, and fails open. Those questions send the
candidate phrase and up to six recently spoken phrases. The commentary gate (see Batching) sends
the redacted batch summary.

**Permission risk** (`scripts/risk.py`): a destructive Bash permission request is announced as
"Careful — this one deletes files. It needs your approval." — a fixed sentence, never the model,
and never deduped. Three tiers: local rules (any `rm`, force-push, `reset --hard`, `DROP TABLE`
through a DB client, …) are instant and final; a strictly read-only command is cleared locally;
only the rest goes to the decider, as `risk.redact()` leaves it (no heredoc bodies or long quoted
text; URLs, hosts, IPs, emails, secrets, long tokens and every path but its last component
replaced), warning at P(destructive) ≥ `decider.permission_risk_min` (0.5). Rules match with quoted
text and heredoc bodies blanked, except code handed to an interpreter (`bash -c`, `python3 - <<EOF`).
The log records the reason, never the command. Fitted on 10,760 real Bash commands; the tests pin
the first draft's false alarms (e.g. `git restore --staged .`, which only unstages).
The key is `OPENROUTER_API_KEY` from the environment or `~/.claude/hobson.env` — **never**
`hobson.json`, which `hobson config show` prints. The confirmed wire format, including that
`noul` returns no confidence, is in the module docstring. Each call logs `cost=$…`, which
`hobson monitor` sums into spend to date.

**Per-project identity**: `project_identity` is **disabled by default and should stay that way**
unless the mechanism changes. It varied `afplay -r` per project; `-r` resamples, shifting pitch and
tempo together like tape speed, and on speech even a 3% shift moves the formants and sounds
robotic. It also silently degraded the macOS `say` fallback, which renders to AIFF and plays
through the same argv. Per-project identity belongs in voice *selection* (the kokoro daemon
supports a per-request voice), not in resampling the output.

**Recap and stats**: `hobson recap [minutes]` (`scripts/recap.py`) is **pull, not push** — the
user asked, so it reads recent log lines for the current project, has Ollama summarise them as one
first-person paragraph, and speaks it once. It deliberately bypasses
`phrase_gen.generate_or_skip` and calls `_chat` directly: that path's 4–12 word budget, dedup and
reject-to-silence guards are tuned for notifications nobody asked for, and a pull answer must never
go silent just because it resembles something said earlier. `hobson stats`
(`scripts/log_stats.py`) tallies what was spoken, queued and suppressed; `scripts/log_analyse.py`
measures voice-quality defect rates so every published rate is a command rather than a one-off.

**The log** (`scripts/log_record.py`) is the only interface between the hooks and everything that
measures them, so one module writes it and the same module reads it, for recap, stats, analyse and
`hobson monitor` (which pipes `tail -f` through `log_record.py --fields`). `write()` stamps
`[YYYY-MM-DD HH:MM:SS] [project] msg` (`BaseEngine._log` adds `[engine]`) and folds line breaks: a
Bash command with no description is its own context, and 178 lines of the real log were the rest of
one. `records()` reads the two older envelopes too (undated `[HH:MM:SS]`, and the oldest, with no
project tag, where the first tag is the engine) and folds those spilled lines back into their
record. An engine tag is known from `ENGINE_TAGS`, so `[nudge]` or a one-word project is never taken
for one; a test fails if a new engine's tag is missing. The three shapes more than one reader parses
have a formatter and a parser side by side: playback (`barked (source) -> 'phrase'`), outcome
(`[Event] (model) -> category -> 'phrase'`) and the generation trace (`gen[Event] ... raw='...' ->
...`). A phrase is written as its `repr()`, so one with an apostrophe is double-quoted: recap's old
regexes read single quotes only, and missed 53% of everything spoken.

**Ollama configuration**: A single model (default `llama3.2:3b`, ~4 GB RAM) handles both Stop event classification and contextual phrase generation. Configurable via `ollama.model` in config. `llama3.2:3b` was chosen via local A/B testing (see below); for a smaller footprint use `llama3.2:1b`; `qwen3.5:4b` is an Apache-2.0 alternative (slightly weaker at classifying "question" Stops). The Ollama server URL is also configurable.

Model choice is validated empirically by `scripts/ab_models.py` (dev-only, needs a live Ollama; classify corpus in `scripts/ab_corpus.json`, 60 balanced done/broken/question cases + a first-person generation pass). Latest run (n=60 classify, 40 gens): `llama3.2:3b` 58/60 classify (missed 2 "done") + 95% native first-person; `gemma4:e4b` 60/60 classify + 92% first-person; `qwen3.5:4b` 58/60 but only 18/20 on "question" and 35% first-person. `llama3.2:3b` stays the default — it edges gemma4:e4b on first-person while gemma edges it on classify, a near-tie that doesn't justify switching a proven default. Gemma 4's edge tiers (`e2b`/`e4b`) are dense with Per-Layer Embeddings, but that does not make them small on disk: `ollama list` reports `gemma4:e4b` at **9.61 GB** (Q4_K_M). Google's "under 1.5 GB" figure is for **E2B**, on **LiteRT**, at **2-bit** with PLE offload — a runtime-RAM number for a different tier, quantization, and runtime, not a GGUF download size. Do not treat the E-tiers as a lightweight swap without measuring `/api/ps` first.

**Personality system**: Voice personality is defined by JSON files in `scripts/personalities/<name>/personality.json`. Each personality has a `templates` section (categories, permission leads/actions, notification templates, generic phrases), which `bark_templates.py` lazy-loads. Templates are all a personality shapes: the static engines' phrases and every fixed-meaning announcement. The daemon engines' generated phrases come from `phrase_gen`'s own prompt whatever the personality (the `prompts` sections went in 22b8c90).

**Built-in personalities**: `hobson` (full 507-phrase template set), `minimal` (terse ~50 phrases), `pirate` (~48 templates), `snarky-dev` (~48 templates).

**Presets**: One-shot config appliers in `scripts/presets.json`. Apply engine + personality + events in one command. After applying, user has normal config they can customize.

**CLI shared picker**: `_pick_menu()` in `hobson` is a reusable arrow-key picker. Callers set `_PICK_OPTIONS[@]` and `_PICK_DESCS[@]`, call `_pick_menu $initial_sel`, and read `PICK_RESULT`. Used by `use`, `voice`, `personality`, and `preset` commands. The `events` command has its own multi-select, inline in the CLI.

**Setup wizard** (`scripts/setup_wizard.py`; the page is `setup/ui/`, the window
`setup/main.swift`, built by `scripts/build-setup.sh` into `build/HobsonSetup.app`): every choice
the installer used to ask in the terminal, and the new ones — engine and voice, personality,
events and commentary, the Ollama model, Jev, presence, the phone switch — in one window. The page
decides nothing: `probe()` reads the Mac, `recommend()` makes the Express loadout, `plan()` shows
the diff, `apply()` runs the tasks and streams each step (NDJSON) back to the page. Nothing is
written or installed before COMMIT; closing the window before it changes nothing.

- **The server is the security boundary.** A `ThreadingHTTPServer` on 127.0.0.1, random port. Every
  `/api` call needs the random token from the window's URL (`X-Hobson-Token`, compared with
  `hmac.compare_digest`) and a `127.0.0.1`/`localhost` Host header (DNS rebinding); static files
  are an allowlist (`STATIC`, plus clips matching `CLIP`). It writes config, saves a key and runs
  installers, and any web page in a browser can send requests to localhost — do not loosen any of
  the three. It exits with the window, or after `IDLE_EXIT_SECONDS` (1800) idle with no task
  running.
- **What reaches the config.** `clean_answers()` drops anything off-catalogue: engine, events and
  verbosity from fixed lists, a voice only as a catalogue id for that engine (`VOICES`, never a
  path; a custom voice already in the config is shown as YOURS and kept), a model only if it matches
  `MODEL_NAME` (any Ollama name — the brain screen takes any model, pulled on COMMIT), the phone
  only when the page answered it. `apply_settings()` writes only values that differ from
  `DEFAULT_CONFIG` and **removes** one set back to its default, so an improved default still
  reaches the install. `decider.backend: "jev"` is written only after `decider.check_key()`
  passed a real call in this session; the key goes to `hobson.env` (mode 600 from creation),
  typed, or found only in this shell's environment — which hooks started from the desktop app or
  an IDE do not inherit.
- **Sections seen** (`~/.claude/hobson-setup.json`): COMMIT records every section in `SECTIONS`.
  `unseen()` is what the installer offers the wizard for when it keeps a config (asked, and only
  interactively; `--update`/`--yes` just name them in the summary). An install older than the
  wizard (config, no record) counts as having seen `LEGACY_SEEN` (voice, events), which the old
  installer asked.
- **Exit status**: 0 applied, 1 a task failed, 3 no desktop (SSH, or no GUI login; the installer
  then writes the defaults), 10 closed before COMMIT. No Swift toolchain means the page opens in
  the browser instead.
- **Its voice is shipped as clips, not a model.** `setup/ui/lines.json` is everything the wizard
  says, by key; `scripts/cache-gen/setup_voice_gen.py` renders each line in Charles (Pocket TTS at
  temp 0.4) and, per offered voice, an audition line at the engine's default setting, in that
  engine's own venv (no daemons: their pid files belong to the running Hobson), as 48 kbps AAC in
  `setup/ui/voice/`, named by `bark_hash` of engine, voice, setting and text, listed in
  `manifest.json`. A line with no clip goes to `say`. `tests/test_setup_voice.py` fails on a line
  or a voice without a clip, an orphan clip, or fewer than two female voices per engine. Only
  Kokoro (Apache-2.0) and Pocket TTS/VCTK (CC BY 4.0) voices, credited in `LICENSES.md`: a voice
  cloned from someone's recording is never shipped.
- **Developing it**: the page runs against a mock backend (`setup/ui/mock.js`) at
  `index.html?mock`, `?mock=update` or `?mock=bare`, in any browser. A real run writes
  `~/.claude` and `settings.json`, and the hooks it installs point at the checkout it ran from, so
  run a worktree's wizard only under a scratch `HOME`.

**Installer** (`install.sh`): hooks, the CLI link and the presence sensor build, then the wizard
for a new install on a desktop, or for kept settings when `unseen()` has sections and you say yes.
With no desktop or `--yes` (which `--update`, run by `hobson update`, implies), a new install gets
`engine: say` and the default events, and nothing heavy (uv, Ollama, a model, a TTS venv) is
installed; `hobson setup` opens the wizard later. The camera permission prompt is the wizard's (on
COMMIT, a look with `request_permission`), not the installer's. The first-run hello goes through
`say` only when the wizard did not run: the wizard says its own, through the engine it configured.

**Distribution** (`install-remote.sh`, the `curl … | bash` entry point): clones into
`~/.local/share/hobson` (`HOBSON_DIR`) at `HOBSON_REF` (default `main`), then execs `install.sh`
with stdin reattached to `/dev/tty` — piped, a prompt would read EOF and `set -e` would end the
install before any hook was written. No tty at all means `--yes`. Everything is inside `main()` so a
truncated download runs nothing. `install.sh --yes` never installs anything heavy (uv, Ollama,
models) — `confirm` answers no unattended. **Re-running is the update path**: an existing
`hobson.json` is kept, and `settings-merge.py` leaves a `settings.json` that is already current
untouched (no rewrite, no backup). Neither the installer (`engine`/`personality`/`events` only) nor
the wizard (non-defaults only) writes a default; every other key comes from `DEFAULT_CONFIG` at
load time, so a default improved later reaches old installs.
`hobson update` fast-forwards the checkout and runs `install.sh --update`. CI
(`.github/workflows/ci.yml`) runs that whole cycle under bash 3.2 and Homebrew bash 5.

**Cache key**: `bark_hash(text)` = SHA-256 first 16 hex chars. Must be consistent across `bark_templates.py`, `engines/base.py`, and all cache-gen scripts.

## Configuration

Config lives at `~/.claude/hobson.json`. Key sections:

```json
{
  "engine": "say|kokoro-realtime|chatterbox|pocket-tts",
  "personality": "hobson|minimal|pirate|snarky-dev",
  "events": ["stop", "permission", "notification", "commentary"],
  "cooldown": 2.0,
  "volume": 3,
  "muted": false,
  "mute_until": 0,
  "quiet_hours": null,
  "ollama": {
    "model": "llama3.2:3b",
    "url": "http://localhost:11434"
  },
  "kokoro": { "voice": "am_puck", "speed": 1.1, "daemon_port": 19849, "daemon_idle_timeout": 600 },
  "chatterbox": { "device": "mps", "exaggeration": 1.0 },
  "pocket_tts": { "voice": "charles", "language": "english", "temp": 0.7, "daemon_port": 19850, "daemon_idle_timeout": 600 },
  "project_identity": { "enabled": false, "spread": 0.06 },
  "commentary": {
    "cooldown": 8.0,
    "tools": ["Agent", "Edit", "Write", "Bash"],
    "verbosity": "terse|normal|chatty|anomaly",
    "suppress_subagents": true,
    "min_tool_calls": 5,
    "min_seconds": 60.0,
    "pending_stale_seconds": 90.0,
    "repeat_window": 180,
    "chattiness": 0.6
  },
  "nudge": { "enabled": true, "delays": [45, 120, 300] },
  "watchdog": { "enabled": true, "minutes": 10 },
  "presence": { "enabled": true, "mode": "signals|auto|continuous", "idle_seconds": 60, "away_after": 30, "greetings": true, "preview": false, "paused": false, "exit_after": 1800, "call_apps": [], "phone": null, "adb": null },
  "decider": { "backend": "local|jev", "model": "typesafe/jev-1.13", "timeout_ms": 4000, "dedup_restates_max": 0.5 }
}
```

`commentary.repeat_window` is anomaly mode's lookback for "back on the same thing again".
`commentary.chattiness` (0–1) is read only with the decider on: a batch speaks at
P(worth) ≥ 1 − chattiness.

`mute_until` is an epoch timestamp (0 = not muted); `quiet_hours` is `[start_hour, end_hour]` and
wraps midnight. Both are read only through `silence_reason()`.

`load_config()` in `home.py` merges user config with `DEFAULT_CONFIG` (shallow top-level, deep merge for sub-dicts). It also migrates the deprecated `kokoro.realtime_events` key to the top-level `events` key.

## Key paths

Every path under `~/.claude` comes from `scripts/home.py`, resolved from `$HOME` each time it is
asked for (`state_dir()`), so a test isolates all of it by setting HOME. The shell scripts and the
two TTS daemon scripts keep their own copies.

- Config: `~/.claude/hobson.json` (engine, personality, events, cooldown, volume, Ollama models, per-engine settings)
- Personalities: `scripts/personalities/<name>/personality.json` (templates)
- Presets: `scripts/presets.json` (one-shot config appliers)
- Hooks: injected into `~/.claude/settings.json` by `scripts/settings-merge.py`
- Lock: `~/.claude/hobson.lock` (file-based lock + cooldown timestamp)
- Commentary lock: `~/.claude/hobson-commentary.lock`
- Nudge/watchdog locks: `~/.claude/hobson-nudge-<key>.lock`, `hobson-watchdog-<key>.lock` (`home.project_file`; `<key>` is `home.project_key`)
- Session state: `~/.claude/hobson-sessions/<hash>.json` (pending queue, fingerprints, recent phrases), with `<hash>.lock` beside it for `transaction()`
- Liveness: `~/.claude/hobson-alive-<key>`, one per project (last tool call or permission request: time, event, tool, timeout — never its contents; read by the watchdog)
- Activity token: `~/.claude/hobson-activity-<key>`, one per project (written by the UserPromptSubmit hook; cancels that project's nudge)
- Presence: `~/.claude/hobson-presence.json` (the helper's state, rewritten every second; removed when it exits), `hobson-presence.lock` (one helper), `hobson-presence.spawn` (spawn throttle), `hobson-presence-lines.json` (the last greeting/farewell said, and when); the salver `hobson-held.json` + `hobson-held.lock`; the helper at `build/HobsonPresence.app` (gitignored)
- Setup record: `~/.claude/hobson-setup.json` (the wizard sections this install has seen); the wizard's page at `setup/ui/` (its voice clips in `setup/ui/voice/`), its window at `build/HobsonSetup.app` (gitignored)
- Decider key: `~/.claude/hobson.env` (`OPENROUTER_API_KEY=…`, mode 600; read by `decider._find_key()`, never logged)
- Log: `~/.claude/hobson.log` — `[YYYY-MM-DD HH:MM:SS] [project] [engine] msg` (engine lines) or `[…] [project] msg`; older lines have only `HH:MM:SS`. Written and read only through `scripts/log_record.py`
- Daemon pid/log: `~/.claude/{kokoro,pocket-tts}-daemon.{pid,log}`; playback scratch WAVs at `~/.claude/kokoro-playback.wav`, `~/.claude/pocket-tts-playback.wav`
- Caches: `~/.claude/voice-cache-chatterbox/` (pre-gen), `~/.claude/voice-cache-kokoro-realtime/` (runtime)
- Venvs: `venvs/{kokoro,chatterbox,pocket-tts,dev}/` (created by the wizard or `hobson setup <engine>`, gitignored)
- Models: `models/` (kokoro ONNX models, chatterbox reference audio -- gitignored)

## CLI commands

```bash
hobson status              # Show engine, personality, events, config
hobson on                  # Unmute
hobson off [duration]      # Mute, optionally timed: 45, 90s, 30m, 2h (recurring: quiet_hours in config)
hobson use [engine]        # Switch engine (interactive picker; pocket-tts only through hobson setup)
hobson personality [name]  # Switch voice personality (interactive picker)
hobson preset [name]       # Apply a configuration preset (interactive picker)
hobson events              # Configure which events trigger voice (interactive multi-select)
hobson commentary on|off   # Toggle running commentary
hobson commentary verbosity [terse|normal|chatty|anomaly]  # Set commentary verbosity (picker if no arg)
hobson commentary chattiness [0-1]  # How much commentary the decider lets through (picker if no arg)
hobson volume [0-10]       # Get or set playback volume
hobson voice [name]        # Switch Kokoro voice (interactive picker)
hobson test                # Play a test bark
hobson recap [minutes]     # Speak a summary of recent activity (default 10m)
hobson presence [status|on|off|mode [signals|auto|continuous]|greetings on|off|preview on|off|phone [auto|off|SERIAL]|look|setup|stop]
hobson lines [category]    # Show voice lines (from active personality)
hobson monitor             # Watch bark activity in real time (and decider spend to date)
hobson stats               # Show what was spoken, queued, and suppressed
hobson doctor              # Run diagnostics (config, hooks, personality, deps, Ollama, daemon, decider, disk, performance tips)
hobson config show         # Pretty-print full config with defaults
hobson config reset        # Back up and reset config to defaults
hobson cache-gen [--force] # Generate voice cache for current engine
hobson setup               # The setup wizard: every choice in one window, written on COMMIT
hobson setup kokoro|pocket-tts|chatterbox  # Install one engine's venv + models, no window
hobson daemon start|stop|status # Manage TTS daemon (kokoro)
hobson version             # Print the version
hobson update              # Fast-forward the checkout, refresh hooks, keep settings
hobson uninstall [--yes]   # Remove hobson (--yes: everything incl. the managed checkout, no prompts)
```

## Testing

Automated suite uses **pytest** (offline + silent — all Ollama HTTP and afplay/say
side-effects are mocked, and `~/.claude` state is redirected to a tmp dir by the
`claude_home` fixture in `tests/conftest.py`, which sets `$HOME`):

```bash
python3 -m venv venvs/dev && ./venvs/dev/bin/pip install -r requirements-dev.txt
./venvs/dev/bin/python -m pytest tests/                          # run the suite
./venvs/dev/bin/python -m pytest tests/ --cov=scripts --cov-report=term-missing  # + coverage
```

Coverage is report-only (no failing threshold). The kokoro / pocket-tts daemon and
`_speak_live` native paths are intentionally uncovered — they need a live daemon and ML
models. So is the presence helper (`presence/main.swift`): it needs a camera and a desk, and is
checked by hand with `HobsonPresence --signals`, `hobson presence look` and a live run. The suite is **919 tests** and runs in about two seconds; if it takes much longer,
something is reaching the network.

Do not read a pass from a pipeline: `pytest | tail` masks pytest's exit code, so an `&&`
chain will happily proceed past a real failure. Check the exit status explicitly.

Manual testing:

```bash
# Test each event type
echo '{"hook_event_name":"Notification","message":"test"}' | python3 scripts/hobson.py
echo '{"hook_event_name":"PermissionRequest","tool_name":"Bash"}' | python3 scripts/hobson.py
echo '{"hook_event_name":"Stop"}' | python3 scripts/hobson.py

# Check config, diagnostics, personality
./hobson config show
./hobson doctor
./hobson status

# Test personality switching
hobson personality minimal && hobson test
hobson personality hobson && hobson test

# Test presets
hobson preset quick-beep && hobson status

# Check event filtering (set events to ["stop"], verify permission doesn't bark)
# Check Ollama model override (set a different model, check hobson.log)

# Check settings.json hooks
python3 scripts/settings-merge.py --check

# CLI
./hobson test
```

## Important constraints

- macOS only (depends on `afplay` and `say` commands, and `fcntl` for file locking)
- `bark_hash()` must produce identical output in all files -- changing the hash function breaks all caches
- `settings-merge.py` identifies our hooks by their entrypoint, `…/scripts/hobson.py` in the command (plus the legacy `claude-bark` / `voice-bark` names) -- hook commands must keep that path. Not the bare word: a bare name claims the hooks of anyone whose home is named after it, and uninstall deletes them -- and Hobson is a surname. Every cleanup of an old link checks for our `scripts/settings-merge.py` first, so another tool's command is never touched
- Legacy names: `migrate_legacy_state()` in `home.py` moves a `claude-bark.json` to `hobson.json`, only when the target is missing, and turns `personality: alfred` into `hobson`; the installer, the CLI and every hook but UserPromptSubmit call it, one `lstat` once done. The chatterbox lookup still finds `models/alfred-reference.*`
- The `${CLAUDE_PLUGIN_ROOT}` variable in `hooks/hooks.json` is for future plugin mode; standalone install uses absolute paths via `settings-merge.py --install-dir`
- Hook commands are `"<python>" "<install_dir>/scripts/hobson.py"`, the interpreter pinned by absolute path at install time: Claude Code runs hooks with its own `PATH`, which from the desktop app or an IDE can resolve `python3` to the Command Line Tools stub. The code must keep running on the stock macOS **Python 3.9** (CI runs the suite on it)
- `settings-merge.py` writes to `$CLAUDE_CONFIG_DIR/settings.json` when that is set, writes *through* a symlinked settings file, and keeps its mode. It grants no permissions: the `Bash(say:*)` entry old installs added is only removed (on uninstall)
- The shell scripts run under `set -e` on bash 3.2 *and* Homebrew bash 5. Never `((x++))` or `cond && ((x++))`: from 0 it is a failing command on bash 5 (verified on 5.3; 3.2 lets it pass) and ends the script (it broke the installer's menus and `doctor`). Write `x=$((x + 1))`. Nor `cmd | grep -q` or `cmd | head` under `pipefail` — the early exit SIGPIPEs `cmd` (`git log | head -15` broke `hobson update` past 16 commits; use `-n`). Nor `"${arr[@]}"` on a possibly empty array: bash 3.2 calls it unbound under `set -u`
- `settings-merge.py` injects **five** hooks (PermissionRequest, Stop, Notification, PreToolUse, UserPromptSubmit), all `async: true` — Stop fires and forgets so TTS doesn't block the user; afplay survives the hook process exiting. `hooks/hooks.json` (plugin mode) now matches: Stop is `async: true` there too
- The Notification hook carries a matcher, `permission_prompt|idle_prompt|agent_needs_input` (Claude Code matches it exactly, before Hobson runs). Drop `idle_prompt` / `agent_needs_input` from it and nudges silently never fire — which is how they went unused until the matcher was fixed. `settings-merge.py --check` fails on a hook that is missing *or* out of date, not only missing
- `UserPromptSubmit` must stay the cheapest path in `hobson.py` — it returns before config or engine load, writing only the activity token, and imports `home`, `log_record` and `nudge`, never an engine (a test runs it under `-X importtime`)
- Nudge and watchdog run as **detached** processes, so they outlive the hook and cannot be gated by it: each must re-check `silence_reason()` and the activity token before every utterance
- The commentary lock is acquired at flush time only. Acquiring it at append time silently drops tool calls out of the batch instead of delaying the announcement of them
- The deprecated `kokoro.realtime_events` config key is auto-migrated to the top-level `events` key by `load_config()`
- Personality templates are loaded lazily on first access via module-level `__getattr__` in `bark_templates.py` — personality JSON is read once per process
- Session state is written only inside `session_state.transaction()`, and a transaction never spans a model call, TTS or a transcript read (see *Session state*)
- The presence helper is launched with `open` (its own camera permission), `-n` for a one-off
  look (without it `open` hands the request to the running sensor, which ignores it, and `-W`
  waits forever). Its designated requirement stays `identifier "local.hobson.presence"`, or every
  rebuild loses the camera grant. It never writes a frame anywhere
- Presence fails to today's behaviour: missing/stale state is `unknown` and speaks. The phone
  switch is the one exception and fails the other way: unreadable = camera off
- Changing personality invalidates chatterbox cache (different template text = different audio); kokoro-realtime cache is fine (keyed by phrase text, auto-evicted)
- `chatterbox.py` reference audio lookup: tries `<personality>-reference.wav`, then `hobson-reference.wav`, then `reference.wav`
