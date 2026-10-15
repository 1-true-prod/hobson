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
    -> engine.run(hook_input)            (BaseEngine.run, the one dispatcher)
        -> PermissionRequest / Notification: BaseEngine._handle_attention() --
           fixed-meaning events from templates (_fixed_announcement), else
           _event_phrase(): pick_permission(tool_name) / pick_notification()
        -> Stop: BaseEngine._handle_stop() -- stop_outcome.read(), then classify() via
           Ollama (configurable model) -> StopReading.settle() -> pick(done/broken/question)
        -> then, for a Stop: BaseEngine._handle_plugin_notes() -- plugin_notes.claim(), the
           first note summarised and spoken as kind "note" (see Plugin notes)
    -> engine.speak(phrase, kind)          (BaseEngine.speak, every engine's)
        -> clip(): cached audio? : macOS say + background backfill -> play()
```

**Daemon engines** (kokoro-realtime, pocket-tts) generate phrases live via Ollama + daemon TTS:
```
hobson.py -> BaseEngine.run(hook_input)    (the same dispatcher)
    -> _is_event_enabled() check against config "events" list
    -> Stop: BaseEngine._handle_stop() -- stop_outcome.read(), then
       phrase_gen.generate_or_skip("Stop", reading.context, stop=reading)
    -> then, for a Stop: BaseEngine._handle_plugin_notes() -- plugin_notes.claim(), the
       first note summarised (local model) and spoken as kind "note" (see Plugin notes)
    -> PermissionRequest / Notification: _handle_attention() -> _event_phrase():
       phrase_gen.generate_or_skip(event, detail)
       via Ollama (configurable model) -- one call decides and phrases; detail from
       _describe_event(). Except fixed-meaning events, spoken from templates
       and never the model (_fixed_announcement): a waiting-on-you Notification
       (idle_prompt / agent_needs_input), a question dialog (AskUserQuestion /
       ExitPlanMode arrive as PermissionRequest), a destructive permission request,
       and Claude Code's permission_prompt reminder (said nothing for when the
       request was said for that session in the last 20s)
    -> PreToolUse: BaseEngine._handle_commentary() (batched; see below)
    -> engine.speak(phrase, kind)
        -> clip(): daemon generate : macOS say fallback -> play()
```

The two are one engine, `DaemonEngine` in `engines/daemon.py`. Each backend is a `DaemonSpec`
(`kokoro_realtime.py`, `pocket_tts_realtime.py`): its config section, venv, daemon script, playback
file, startup polls, generate timeout, and which options go to the daemon's CLI and which into every
request. They were two copies until a fix to one kept missing the other. `hobson daemon` reads the
same spec, through `DaemonEngine.cli_lines()` (port, files, the start command): the CLI kept its own
copy, for Kokoro alone, and could not start, stop or report Pocket TTS. Its `stop` kills a listener
on the port only when that listener runs this checkout's script: matched by name alone, it killed
another checkout's daemon on the same port (the sandbox's, or yours from a scratch HOME).

The daemons were the same story on the other side of the seam, and it cost more: two near-copies
of an HTTP server with no guard at all, so any web page could POST `/shutdown` or `/generate` (a
`text/plain` body needs no CORS preflight), and Pocket TTS fetched whatever URL, or loaded whatever
path, a request named as its voice. Now `kokoro-daemon.py` and `pocket-tts-daemon.py` are only
backends (load a model, turn text into WAV); `scripts/tts_daemon.py` serves both, behind
`scripts/loopback.py`, the guard the setup wizard runs too:

- **Host** must be `127.0.0.1:PORT` or `localhost:PORT` (DNS rebinding), on every method and route.
- **Origin**, when a browser sends one, must be this server. The engine and curl send none.
- **Token** in `X-Hobson-Token`, compared in constant time, on `/generate` and `/shutdown`
  (`/health` needs none). A daemon writes a fresh one at mode 600 to
  `~/.claude/<script>.token`, **after** it holds the port: of two cold starts at once, the loser
  must not overwrite the winner's token, which would lock every client out until idle-out. The
  engine, `hobson daemon stop` and uninstall read it from there.
- **Voice**: a request may name one from its backend's catalogue (Kokoro's voices file; Pocket
  TTS's own table of names, read from the library, empty if it ever moves) or the voice the daemon
  was started with, which may be a path the user configured. Anything else is a 400.

Test the guard through `tests/test_loopback.py`, which drives the runner over real HTTP with a
stand-in backend. A new local server takes `loopback.Server` and `GuardedHandler`; it does not
grow its own checks.

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
  figure is measured, not guessed: commentary speaks without `cold_start` (it will not block a
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

**Pocket TTS 3.x** (checked 2026-10-06 on 3.3.0). `requirements-pocket-tts.txt` pins `pocket-tts==3.3.0`
(the version, not a hash; its dependencies still float), so a fresh install gets the release that was
checked, and a bump is a deliberate step after checking PyPI's attestation for it. The daemon touches only `TTSModel.load_model`,
`get_state_for_audio_prompt`, `generate_audio`, `sample_rate` and the voice table above, and all of
them still work as it uses them. `language: english` now means `english_2026-09`; the dated
`english_2026-04` and `english_2026-01` can still be named. The weights the wizard fetches
(`pocket-tts-without-voice-cloning`, the folder `engine_ready` looks for) cannot clone from an audio
clip, on 2.1 or on 3.3 (`has_voice_cloning` is False); cloning needs the gated `kyutai/pocket-tts`
repo and a Hugging Face login. A custom voice therefore arrives as a path to a precomputed
`.safetensors` voice state, which is a snapshot taken through one model. One such state, made on
2.x, was rendered through `english_2026-04` and `english_2026-09`: the long-term spectrum differed
by 2.4–2.8 dB where two renders of the same model differ by 1.9–2.1 dB, and median pitch stayed
within the noise between renders. Close, but not identical, so pin
`pocket_tts.language` to a dated model when a voice has to stay as it was made.

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

**The last message comes from the hook, not the transcript.** Claude Code writes the transcript
asynchronously, and at Stop time it usually does not yet hold the turn's final message. Read from the
transcript alone, 339 of 470 matched Stops since 2026-10-01 (72%; 77 more unmatched) were classified
and phrased on the message *before* the last one, and 86 final questions started no nudge.
`stop_outcome.read` therefore appends the hook input's `last_assistant_message` as the final turn
whenever the transcript does not already end with it, read through `_prose` like any turn. The field
was checked live on Claude Code 2.1.299: present and complete at 2,620 and 12,354 characters. Every
Stop number in this file from before this change (the held-out figures below included) was measured on
complete transcripts, so it describes the classifier on the right input, not what the live hook saw.

**The model is not shown what was said before.** The session block used to list the last six spoken
phrases ("Recent:"), of any age; 93% were over two minutes old, and llama3.2:3b reused them: 67% of
off-batch commentary took two or more content stems from one (2% for a Recent block from an hour away),
and paired replays of real Stops carried a stale phrase 33 times with the block and never without,
with no category changed. The block now says only "No announcements yet this session." when nothing was
said. `recent_voiced` is still kept, for the duplicate guards and the decider, which read it. The rule
"Do not repeat or reword anything under Recent" and the few-shot examples' Recent lines were left as
they were, because the replays measured that prompt. The measurement (one arena over the harness-design
paper) is in `research/2026-10-14-harness-arena/`, which is untracked.

**Do not give commentary the Task line.** Replayed on 80 real commentary batches and blind-judged, the
old prompt beat it 36–25: the model spoke the request as the action ("I'm opening a report for you").
Commentary, permission and notification prompts were byte-identical to before the Stop work until the Recent block was removed from all of them (above).

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
  it was rejected (`_retry_turn`): a reply over `MAX_WORDS`, which used to go silent with no retry
  at all, or a `doubts()` "broken".
- **A Stop is never rejected as a near-duplicate.** It is the turn-ended signal, news in the same
  words. The retry a rejection used to buy ("Not that: … Say something else.") left a 3B model with
  nothing else true to say, and it made things up: replayed on 19 real near-duplicate Stops with the
  complete last message, it turned 8 "broken" ("I failed to fetch the ticket's Scope."), where
  accepting the duplicate turned none (bad outcomes 10–0, exact McNemar p=.002; n=19, one seed,
  agent-labelled). `settle()` relabelled the category, but the invented failure was still spoken.
  The rejection also caused 11 of the 40 silent Stops from 09-27 to 10-14.

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
`last_stop_category = "working"` — which `session_status.idle_after_settled_turn` treats like `done`, so the idle
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

**Engine subclass pattern**: `BaseEngine` in `engines/base.py` handles all shared logic (event filtering, the one dispatcher, fixed announcements, commentary, the Stop pipeline, speaking); config and paths come from `home.py`, and every log line goes through `log_record.py`. What an engine supplies is narrow: its phrase sources (`_stop_phrase`, `_event_phrase`: templates for the static engines, the model for `DaemonEngine`) and how a phrase becomes audio (`clip()`, `render()`, `warm()`). Static subclasses set `templates_module`, `cache_dir`, `cache_ext` and implement `backfill(text)`. Before the dispatcher was one, the static engines' own `run()` never marked the session alive, never recorded what it said, and did not rotate the fixed lines; `tests/test_speak_contract.py` drives every family through the same events and reads what was said from the log.

**Kinds** (`scripts/kinds.py`): every phrase has a kind (`waiting`, `broken`, `stalled`, `done`,
`info`, `commentary`, `nudge`, `briefing`, `answer`, `thinking`), and `KINDS` declares once what
each means to everyone who decides on it: the briefing's urgency, never held, said in passing
(dropped rather than held or queued, no look, off the face), waits on you (the face holds it),
offers the face's buttons, Hobson's own talk (recap), names the project. `stop_kind` and
`event_kind` are where hook events get theirs. A speaking path must name one (`kinds.require`):
`hobson recap` spoke with none, which counted as news, so it was held while you were away and
retold by the next recap as work. `face.js` keeps its own `MOODS`/`ACTION_KINDS` for drawing;
`tests/test_kinds.py` holds them to the table.

**Session status** (`scripts/session_status.py`): who is waiting on you, what failed, what is
working or finished, across every session. The facts are written in four places (session state,
`nudge.record_alive`, `nudge.touch_activity`, a running nudge's lock) and read only here:
`waiting_on_you`, `reading` (Ask Hobson), `idle_after_settled_turn` (a `done` or `working` Stop
leaves nothing waiting), `nudge_type`, `is_question_dialog`, and `WAITING_NOTIFICATIONS`, which is
`nudge.WAITING_TYPES` plus `permission_prompt` (a wait that starts no nudge). Every session file is
read through `session_state.load_all`, never raw. nudge.py stays the writer, importing none of
this: the UserPromptSubmit path must stay cheap.

**Event filtering**: The top-level `events` config key controls which hook events trigger voice. `BaseEngine._is_event_enabled()` maps hook event names to config keys (`Stop`->`stop`, `PermissionRequest`->`permission`, `Notification`->`notification`, `PreToolUse`->`commentary`). All engine `run()` methods call this at the top. `UserPromptSubmit` has no `events` key — it is intercepted in `hobson.py` and never reaches an engine. The map is `home.EVENT_KEYS`, read through `home.event_enabled`, because the silenced hook gates on it too and imports no engine.

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

**Speaking** is one method, `BaseEngine.speak(phrase, kind, cold_start=False)`, for everything Hobson says, from a hook or not (nudge, presence, ask, recap and coach call it too): the audience (`_for_audience`, which refuses a phrase without a kind), the busy voice for commentary, the bark lock and its cooldown (`_claim_bark_lock`), then the engine's `clip()` and `play()` (afplay, the voice lock, the face), and one playback log line. It returns False only when presence held or dropped the phrase. `cold_start` is for a phrase that may wait for a cold daemon and is never dropped by the cooldown: a Stop, and anything said because you asked or something waits; without it a phrase is skipped while another hook holds the bark lock or spoke within `cooldown` seconds, and `warm()` starts a down daemon for next time. It replaced three hand-rolled copies (`try_bark`, `speak_dynamic`, the daemon's `_speak_live`), each with its own cooldown. On the static engines `clip()` reads the template cache for every phrase (the destructive warning and the fixed waiting lines play from it on chatterbox), and backfills a miss unless the kind is said in passing or Hobson's own talk.

**One voice at a time** (`scripts/voice.py`): every clip leaves through `BaseEngine.play`, which
first waits its turn on `~/.claude/hobson-voice.lock` and hands the locked descriptor to the player
(`pass_fds`). A flock belongs to the open file, not the process, so the lock is held for exactly as
long as afplay plays, although the hook that started it has exited. The next clip, from any
session, waits for it (`voice.WAIT_SECONDS`, 30, then plays anyway: a stuck lock must not silence
Hobson). Before it, nothing stopped two clips playing at once: two sessions finishing together
talked over each other, and an agent reading a spec aloud through a loop over `hobson test "…"`
(which returned as soon as afplay started) stacked eighteen paragraphs on top of each other in a
minute, with the other sessions' announcements on top. Commentary does not queue: it is dropped
while anything plays (`_voice_busy_for`), stale by the time its turn would come. A daemon engine
writes each phrase to its own WAV (`mkstemp`, played with cleanup): the one fixed file was
rewritten by the next phrase while the last still played, and a phrase waiting for its turn would
have played its successor. The bark lock is let go before a clip waits for its turn, on every
engine, so a phrase past the cooldown queues behind the one playing rather than being dropped.

**hobson say** (`scripts/read_aloud.py`): reads text aloud on demand, from arguments, `-f FILE`
or standard input. The text is cut into passages (paragraphs; a long one at sentence ends, at most
`MAX_CHARS` 300, about 20s of speech; Markdown markers dropped, headings and list items given a
full stop) and each is rendered (`engine.render`, the daemon with a 30s timeout, else `say`) while
the one before it plays, then played as kind `answer`, so presence never holds it. The command
returns when the last passage has finished, so a shell loop of `hobson say` reads in order. A
reading holds `hobson-reading.lock` throughout, so a second reading waits for the first instead of
alternating with it; announcements take only the voice, and are heard in the 0.35s gap between
passages (`GAP_SECONDS`, which is also why they win that race). `hobson off` or quiet hours stop
it within a quarter of a second (`silence_reason` is polled while a passage plays), as do Ctrl-C,
SIGTERM and SIGHUP; the passage playing is stopped and every clip removed however it ends. `hobson
test "TEXT"` is the same path. Log lines are `[read] …` plus one `barked (read-aloud) [answer]` per
passage, and recap reads both as Hobson's own talk.

**hobson coach** (`scripts/coach.py`): a script of spoken steps run on Hobson's own clock, for a
guided session an agent cannot pace itself: its harness blocks `sleep`, and a turn through the
agent takes far longer than "hold for two seconds". Chained `hobson say` calls had no pauses but
the time each line took to say. One step a line, from `-f FILE` or standard input: `say TEXT`,
`wait SECONDS`, `countdown N` (N to one, a second apart, at most 10; the next step starts a
second after "one") and `ready [SECONDS]` (any key, click or mouse movement after it begins, else
the time, 60s by default). The whole script is parsed before anything is said, and every clip is
rendered before the clock starts: a countdown rendered as it went would stretch by Pocket TTS's
seconds a line. Standard output is NDJSON written as it happens -- `start`, a `step` as each ends
(seconds from the start; `ready` adds `by: input|timeout` and `after`), one `end` -- so a run
killed at the harness's 10-minute limit still leaves what it did. It only reports: what to do with
an answer is the agent's call.

- **Clips are cut to their speech** (`_trim`, `speech_span`; WAV only, float32 or 16-bit). A
  Pocket TTS clip of one number measured 0.9-1.8s, opening with a 10 ms click at -42 dB and then
  -66 dB until the word, which came anywhere from 0 to 1.3s in. Played as rendered, each number
  landed late by a different amount, and a clip outlasting its beat held the voice lock and pushed
  the next number back. Trimmed (speech = 35 dB under the peak for 30 ms running, so the click
  does not count; 20 ms kept before, 120 ms after): 0.28-0.77s over 15 renders, the word within
  the first 0.09s. A number still sounding at the next beat is stopped all the same, so no engine
  can make a beat slip. `say`'s AIFF is left as it is (0.44-0.55s a number). Checked without a
  speaker (scratch HOME, `say`, a stand-in afplay logging start times): the numbers started 1.03s
  and 0.97s apart. Not yet heard on Pocket TTS.

- **Input** for `ready` is `CGEventSourceSecondsSinceLastEventType`, read through ctypes: the
  counter the presence helper reads, no permission, no helper (presence may be paused). It says
  *that* something was pressed, not what; telling keys apart would need Input Monitoring. A key
  goes to whichever window has focus, Claude Code's prompt included, so a click or a clicker is
  the safe signal.
- **Everything else waits.** A run holds `hobson-reading.lock` (a `hobson say` elsewhere waits for
  it) and `hobson-coaching.lock` (`voice.coaching`). While that is held, `presence.audience` is
  `coaching` before it reads the sensor -- presence on, off or paused -- so every other session's
  announcement is held on the salver, commentary is dropped, a nudge pauses
  (`pauses_nudge`), and a transition or a wave says nothing. When the run ends, however it ends,
  `coach.brief` tells what was held ("In the meantime: …"), unless Hobson was silenced, when it
  stays on the salver.
- **Stops** like a reading: `hobson off`, quiet hours, Ctrl-C, SIGTERM, SIGHUP; the clip playing is
  stopped and every clip removed. A run waits at most 60s for a reading already going
  (`READING_WAIT`). Exit 0 done, 2 a bad script (nothing said), 130 interrupted, else 1. Log lines
  are `[coach] …` plus `barked (coach) [answer]` per clip, both Hobson's own talk to recap.

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
those. The summary goes out as a `decider.Summary`: redacted by the decider, at most 220
characters, an Edit's `(+3/-2 lines)` kept (the path rule skips it).

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

**The session you are looking at gets more** (`presence.watched`, the `focus` config section). The
user's call, and the opposite of the field: Claude Code, Codex, Cursor and peon-ping go quiet about
the session in view; Hobson narrates it, in very short updates, often. Every other session is as
before. A session is watched when presence says `present`, the helper's record names a
`frontmost` app (its bundle *path*, `/Applications/Ghostty.app`, because that is what
`terminal_jump._owning_app` reads off `ps`; a bundle id would never compare equal), and that app
owns the terminal: in tmux, this hook's own `$TMUX_PANE` must be the current pane of the tmux client
you are looking at: the one flagged `focused` in `#{client_flags}` (tmux's focus events), else the
one used most recently (`terminal_jump.active_tmux_client`, one `tmux list-clients`); outside tmux, the
tty in the liveness record (app level only: Ghostty does not expose its tabs' ttys). A helper
built before this publishes no `frontmost`, so nothing is watched until `scripts/build-presence.sh`
has run and the running helper is stopped (`hobson presence stop`; the next hook starts the new
one). `ensure_running` does not restart it for the missing key on purpose: an old binary restarted
still lacks it, and would be killed and respawned every 15s with its camera. The fallback, tmux's `client_activity`, counts typing, not
focus, and across all clients: with focus events off, a tmux client in another app typed into a
moment ago wins over the Ghostty one you are only reading, and the pane you look at reads as
unwatched until you type there. Ties between clients fall to the tty's name. With the `focused`
flag, clicking to another window moves the watch without a keystroke. The decision is
the pure `presence.watching`. Unknown, away, a call, company, presence off, paused or face-only, a
stale record, any failure: not watched, which is today's behaviour. No new permission is asked for.
For the watched session, terse/normal commentary only (chatty and anomaly are untouched):

- The gate opens at `focus.min_tool_calls` (2) or `focus.min_seconds` (15) instead of 5 or 60, and
  the log says `[PreToolUse] flush (watched)`, which `hobson stats` counts as "watched session".
- Its cooldown is `focus.cooldown` (4s), measured from its own last flush and from one timestamp
  that only watched flushes read and write (`hobson-focus.lock`, so several watched sessions, which
  happens outside tmux where every session in the front app counts, share one 4s pace). It neither
  reads nor writes the shared commentary timestamp. Reading it, a watched session beside a busy
  unwatched one flushed about every 9s in batches of 6 instead of every 4s in batches of 3; writing
  it, it kept every other session short of its 8s window until its batch went stale. Tests hold
  both. The cost: an unwatched session can speak about 2s after a watched phrase where it used to
  wait 8s, and a watched session speaking often makes an unwatched batch more likely to be dropped
  at `_voice_busy_for` after its model call has been made.
  The shared pace is first come, first served. Two sessions are watched at once only outside
  tmux, where `presence.watched` asks whether the app owning the session's tty is the frontmost one
  and every session in that app (two Ghostty tabs) passes; in tmux only the pane that is current
  in the client you used last is. A quiet watched session beside a busy one loses its turns: in a
  seeded replay (600s, Poisson arrivals, 5 seeds a case, the real flock and session state, a fake
  clock), one at 0.2 calls/s flushed 47-56 times alone, 16-24 beside a watched one at 1 call/s
  (longest silence 78-133s, 9-25% of its calls lost) and 8-14 beside one at 2 calls/s (105-233s,
  28-54% lost); beside one at 0.3 or 0.1 calls/s it lost 0-2%. Every loss is the `MAX_PENDING`
  8-call cap trimming the deferred queue, none is staleness. Kept as it is, and
  the number to know if two busy tabs of one terminal ever feel short of commentary.
- No worth call to the decider: a batch you are looking at is worth hearing, and the question is
  about a user "not watching the screen". The duplicate guard's decider calls (rescue and
  restatement) still run, so with `jev` the more frequent watched flushes cost about one call each,
  as before ($0.000017 a call in the log). On 2,387 real P(worth) values in the log, today's bar
  (0.4) passes 18.6%; +0.2 chattiness would pass 30.9%, still holding back two thirds; skipping
  passes all. The logged values carry their batch size (1: 946, 2: 250, 3: 238, 4: 224, 5: 688, 6-8: 41),
  and 2-call batches pass today's bar 27.2% of the time against 16.4% for 5-call ones. The
  phrase comes from the `decided` prompt, which has no SKIP.
- The budget is `focus.min_words`-`focus.max_words` (2-6), passed to the prompt and the length
  check (`phrase_gen.generate_or_skip(words=…)`), and restated after the event. Stated among the
  rules alone, llama3.2:3b kept writing 7-11 words, and on 100 real batch summaries ×2, 67-76% (72% over both replays) failed
  the length check; restated, 92% passed the length and batch guards at 4.3 words on average, with no recent
  phrases to repeat (the 4-12 prompt, unchanged: 83% at 7.9). The off-batch guard took 7% of the
  short ones against 4%: short phrases share fewer words with the batch, not many fewer. Replayed
  in order, one project's 100 summaries 15s apart with each spoken phrase carried forward and the
  decider local, the near-duplicate guard took 2 of 100 at 2-6 (84 spoken) and 4 at 4-12 (81 spoken) in the run
  saved in the trail (`dup_replay_r1.txt`); earlier unsaved runs gave 1-3 and 2-3, and 89 spoken
  with 3 rejected at 2-6, 82 with 0 at 4-12. The rate moves between runs and is a few percent
  either way. On a synthetic repeating cycle it rejects 6 of 14 short phrases, not measured on
  real use: an open question for the listening test.
- Stops, waits, permission requests, presence routing and the busy voice are unchanged: commentary
  is still dropped while a clip plays or you are away.

The check costs the PreToolUse hook nothing until the record says present with an app in front,
then one `tmux` call (a pane that is not current stops there), then the `ps` walk. Measured
queue-only from the hook, 20 runs each, on a noisy machine, median: about 90 ms before (alternated
with the branch), about 90-110 ms with a frontmost app (medians of 101.0 and 91.5 ms for the watched pane, 109.7 and 101.9 ms for another one). The
real frontmost app was Chrome, so the app in front was emulated as Ghostty in the record; the tmux
client and the `ps` walk were real (`bench_r2.txt` in the trail). UserPromptSubmit never runs it. In tmux,
the watched decision is per pane: each hook reads its own `$TMUX_PANE`. The pending queue and
`last_flush_time` are per project, the cwd's label, so two panes in one cwd share a queue, and the
watched one flushes the other's calls. Outside
tmux the tty comes from the project's liveness record, last writer wins. `focus` is a
top-level section because `with_defaults` merges one level deep: nested under `commentary`, setting
one key would drop the others' defaults. The numbers are a first guess, to be tuned by ear.

**Session state** (`scripts/session_state.py`): per-project rolling memory keyed by project label
(ties to worktree/cwd, survives session restarts) at `~/.claude/hobson-sessions/<hash>.json`.
Holds the pending queue, the fingerprint ring (anomaly mode only), recently-voiced phrases (for the duplicate guards;
no longer shown to the model), `last_event_time`, `last_stop_time` and `last_stop_category`, and the last
permission request said and its Claude Code session (`last_wait_time`, `last_wait_session`), each with one
writer (`mark_event`, `begin_stop`, `record_stop_category`, `record_wait_said`).

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
finished work, and the Stop already said so) — one gate, `session_status.idle_after_settled_turn()`. Nor is one
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

The one speak seam, `BaseEngine.speak`, calls
`_for_audience(phrase, kind)` → `presence.route`, once, so no engine or event can talk past an
empty room. `kind` is what the phrase is about (`stop_kind`/`event_kind` in `engines/base.py`):
away or call **holds** it on the salver (`~/.claude/hobson-held.json`, global, flocked, capped,
12h), except commentary, which is **dropped** (stale by your return) and never triggers a look;
company speaks a wait as `COMPANY_LINE` and holds the rest. `briefing` and `nudge` are never held.
It gates at the seam, not in `hobson.py`, on purpose: a Stop is still read and classified while
you are away, because the nudge gate and the briefing need `last_stop_category`. Presence is not
folded into `silence_reason()`: silence drops, away holds, and the log says which.

On return (away/call/company → present) one briefing, most urgent first (`kinds.Kind.urgency`), two told and
the rest counted, a session's repeated waits collapsed, sessions still waiting on you named
(`session_status.waiting_on_you`: a running nudge, an unanswered question Stop, a fresh permission request) —
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
Changing `mode`, `phone`, `preview` or `waves` through the CLI restarts the sensor at once.

The menu bar (`MenuBar`, `Controls` in `main.swift`): while the sensor runs it has an icon (an SF
Symbol per state; `eye.slash` paused) and a menu: the state, the session list (below),
**Ask Hobson**, **Show Preview**, **Presence On**, **Answer Waves** and **Show Hobson**. Show Preview shows or hides the window in place, and closing the window unticks it. Unticking
Presence *pauses* it (`presence.paused`, `--paused`): the camera off, nothing sensed, the state
file `"off"` (no audience, so Hobson speaks as with presence off), and the icon kept so you can
resume. `hobson presence off` is different: nothing runs. `hobson presence on` resumes a pause
too. A toggle runs `presence.py --set KEY VALUE --keep-running` and **waits for it before the
sensor changes**, then rewrites the state file at once: `ensure_running` restarts a sensor whose
`mode`, `phone`, `preview`, `paused` or `waves` disagree with the config, and a hook landing between
the two would have. It treats any fresh record as a running sensor (`helper_alive`), paused included;
gated on a known audience, it `open`ed a paused sensor every 15s. Paused, the sensor never exits
on its own: its icon is the only way to resume from the menu, and it senses nothing. It used to
leave after `exit_after` without a hook like an unpaused one, taking the icon with it, which read
as the icon vanishing. Every exit is logged, `[presence] sensor exited (why)`. Verified live: toggling the
preview and pausing from the menu left the same pid and no `sensor started` line. The icon
appears for everyone with presence on (the default), which is also how the camera-capable helper
shows itself.

**The session list** is the same reading Ask Hobson speaks, kept in view. `python3
scripts/session_status.py --json` prints `session_status.menu_json()`: `waiting`, the count, and `rows`,
each `{project, state, text}`, waiting first, then failed, working, and the last hour's finished.
Under the status line the menu draws one row per session ("● webapp — waiting on you (question)",
the dot coloured by state); a row's click runs `presence.py --face-action take-there PROJECT`, the
face's own button, and a count beside the icon is the number waiting. The menu opens on what was last
drawn: `Controls.refreshSessions` reads on a global queue (never the main thread), on opening the
menu, on a face signal and every 15s, and swaps the rows in when it returns, with "(updating)" on
the header if it takes over 300ms. One read at a time (a trigger that finds one running does
nothing, and a read is ended after 10s), so a slow Python cannot pile up. A read costs about 50 ms
warm and 145 ms on the first run, measured with three sessions. A face-only helper has the menu too:
`Controls` is built whatever the presence setting.

The list is careful with the pointer and with the nudge. A read that returns the rows already shown
leaves them alone (`setSessions` compares), since swapping every row moves the one being aimed at,
and the menu reads again as it opens. Before the first read the list says "Reading sessions…", a
first read that fails "Could not read sessions", and a later failure keeps the rows with "(last
read failed)" on the header. A read that ignores SIGTERM is killed two seconds later: it blocks on
the pipe, and `sessionsReading` would otherwise stay set and stop every refresh. With no count the
status item is square (24 pt, as it was before the list); a variable length gave 32 pt, so the
length is set to variable only while a count shows. The count's probe, `nudge.running`, takes
each nudge lock for a moment, and a nudge that starts in that moment used to find it held and give
up ("a nudge is already running"), the very time one starts, as a wait is announced and the face
signals the helper; the nudge now retries its lock for a second before it concludes another runs
(`nudge._take`). A finished nudge or watchdog leaves its lock file in place: deleting it let a waiter lock an unlinked inode, which the probe could not see, and a third nudge ran beside it. Waiting rows run most recent first, by the session's last turn or tool call, and
a row reads "waiting on your approval" from `session_status.NEED`, the one table the spoken lines
use as well. A session with no project name has nowhere to jump to and is not a row.

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
**Answer Waves** in the menu (`presence.waves`, default on; `--no-waves`) stops the detector itself, not
only its answer: the hand-pose pass is the whole cost of waves, and the camera stays for faces. A
face-only helper senses nothing, so it is expected to have `waves` on and is never passed the flag.

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

**Face** (`scripts/face.py`; the window is `presence/Face.swift`, the page `presence/face/`):
presence lets Hobson see you, and this is the other direction. Each line he speaks appears in a
small window, top right under the menu bar: Valet, an ASCII bust drawn in code (parted hair, wing
collar, a glowing bow tie), saying it over a slightly spotty signal, then switching off like a CRT.
It is a viewer, never a player: every playback goes through one seam, `BaseEngine.play` (afplay
first, then `face.show`, which never raises), after `_for_audience`, so a held or dropped phrase
never shows and a face failure leaves the voice as it was. The say fallback renders its AIFF before
playing (`_render_say`, about half a second) so there is audio to follow. The one other way in is
a silent line, shown while Hobson is silenced: it comes from `hobson._show_silently`, not from a
playback, and does not pass `_for_audience` (see *Quiet controls*).

- **The hand-off.** `face.show` writes `~/.claude/hobson-face.json` (mode 600: it holds the phrase)
  and sends the helper **SIGUSR2**, only when its record says `"face": true`: SIGUSR2's default
  action is to exit, so a helper built before the face would be killed, and `ensure_running`
  restarts that one instead. The helper reads the record at once, with the audio it names (every
  clip is deleted when afplay finishes):
  RMS per 16 ms, normalised to a loud frame, the quiet gated to silence. The page gets it through
  `callAsyncJavaScript` arguments, never built script text, and shows the caption through
  `textContent`; its CSP is `default-src 'none'; script-src 'self'` (verified in WKWebView under
  `file://`). The mouth follows the loudness, and the caption is revealed by cumulative loudness,
  so a pause does not move it. With no audio (`hobson face test`, or a clip already gone) it
  follows syllables estimated from the text. A line more than 10s old, or nearly over by the time
  a cold-started helper reads it, is skipped.
- **What shows.** Everything spoken but commentary (`face.commentary`, off: a window every minute
  of work is noise). The kind sets the mood: waiting, nudge and a watchdog stall need you (brows
  up, a tilt); `broken` alone is red; done nods; the briefing bows. In company the company line
  goes without the project, as the voice does (never while silenced: the helper senses nothing then). While Hobson is silenced, nothing is spoken, and
  what waits on you is shown instead (a silent line, below).
- **How long.** A said line goes 1.5s after its audio ends (30s at most). A line that waits on you
  (waiting, nudge, with `face.hold_waiting`) stays until that session's activity token is newer
  than the line, which is to say you typed there, for at most 10 minutes. A click dismisses it; a
  drag moves it, and the corner is kept in the helper's defaults (`faceTopLeft`).
- **Silent lines** (`face.show(..., silent=True)`, `"silent": true` in the record): shown while
  Hobson is silenced, with no audio. The page shows the line whole at once with the mouth still,
  as for "One moment.", in its kind's pose and chip. There is no "once said" for it to go after, so
  it carries the activity token and stays until you type in that session, whatever its kind and
  `face.hold_waiting`, for at most 10 minutes; a silent `broken` would stay the same way (no hook
  path shows one today: the rules alone never read a Stop as broken). Unlike an `asked` line, it
  respects the face being off. Its Skip / Take me there buttons are its kind's, as for any line.
  The face has one slot: a silent line is replaced by any later line, from any session, and is
  not brought back when that one goes.
- **Skip / Take me there** (`scripts/terminal_jump.py`): a line that may still have a nudge running
  against it (waiting, nudge, stalled, broken) shows two buttons, drawn by the page itself. Skip
  touches the project's activity token (`nudge.touch_activity`) -- the same signal a typed prompt
  gives, so a running nudge or watchdog stops at its next check and the line's own "typed" hold
  takes it down. Take me there reads what `nudge.record_alive` last captured for that project
  (`terminal_info`): inside tmux only `$TMUX_PANE` (stable across window/session renames, unlike a
  name or index); outside it, the terminal's tty. A hook has no tty of its own (Claude Code starts
  it in a session of its own, so `/dev/tty` fails: every live record lacked one, and the first
  version raised nothing for a week), so it is the nearest ancestor's, Claude Code's, one `ps` a
  step (`_ancestor_tty`). Then two independent best-effort steps. tmux: `select-window` /
  `select-pane` on the pane, then `switch-client` for the client already showing that session,
  else the one used most recently, asked of tmux at the click -- the pane's own tty is no terminal
  window. The terminal: the app owning the client's (or the recorded) tty, found by walking up
  from the processes on it to the first `.app` (the outermost, so an IDE's helper resolves to the
  IDE). In iTerm2 or Terminal.app, whose AppleScript dictionaries expose a session's/tab's `tty`,
  that tab is selected (the first use prompts for Automation permission); any other app is
  brought forward with `open -a`, which needs none. Ghostty (1.3) scripts its terminals but not
  their ttys, so it gets the app, not the tab. Either half failing (closed pane, no client, quit
  app) costs only that half, logged, never an error dialog. Both buttons are DOM
  elements in the page, not drawn by Swift: `FaceOverlay`, which otherwise eats every click so a
  plain click dismisses and a drag moves the window from anywhere, lets a bottom strip through to
  them (`hitTest`, `passThroughBelow`) -- sized from the page's own `{type: "size", ..., actions}`
  message, never a guessed constant, since only the page knows its real layout.
- **Focus.** A borderless non-activating panel that can never become key, shown with
  `unhideWithoutActivation` + `orderFrontRegardless` (the helper starts hidden under `open -j`):
  typing carries on wherever it was (verified: the frontmost app is unchanged while it shows).
- **He looks at you** from the largest face in the helper's own sighting, the camera taken as the
  top centre of the main screen and you about 60 cm away (`FaceController.direction`): live in
  continuous mode, the look before he speaks in auto, a wandering glance with no sighting. The
  face boxes never leave the process, as before.
- **Without presence.** With `presence.enabled` false and the face on, `ensure_running` starts the
  helper **face only** (`--face-only`): it senses nothing, writes state `"off"` (no audience, so
  routing is unchanged) and `face_only`, and keeps `paused` as configured, since a face-only
  helper reporting a pause would be restarted by every hook. `expected()` is the one place that
  says what a helper should be running with; the argv and the restart check both come from it.
  The helper does not exit while a line is on screen. Silenced (`home.silence_reason`: a mute, a
  timed mute, quiet hours) is the other case that runs face only, a camera helper included: see
  "Muting turns the camera off".
- **Cost.** A WebContent process, 60-100 MB while it exists: the web view is made on the first
  line and released after 10 minutes hidden. Nothing renders while the window is hidden.
- **Developing it.** `presence/face/index.html?mock` runs the page alone in a browser, cycling
  sample lines (every other one with a synthetic loudness track). For the window, build the
  worktree's helper and run it by hand in a scratch HOME with `open -n` (without `-n`, `open` hands
  the launch to a running helper of the same bundle id): `open -n -g -j build/HobsonPresence.app
  --args --home $H/.claude --face --face-only --face-page presence/face ...`, then `HOME=$H python3
  scripts/face.py --test "..." --kind done`. In the real HOME a worktree would signal the live
  helper. A scratch HOME has no hook activity, so touch `hobson-presence.spawn` there or the helper
  exits after a minute.
- **Breadcrumbs** (`[face]` in the log, shown by `hobson monitor`): `face.py` says which helper it
  signalled, or why none (none running, or one without the face, restarted). The helper reports
  each line once, as it goes: how long it was up, whether the mouth followed audio or the text, and
  why it went (`said`, `typed`, `clicked`, `replaced`, `cap`, `face off`); a line it skipped (too
  old, nearly over after a cold start); audio it could not read; a page that never ran within 5s
  or crashed (red in the monitor). The helper never writes the log itself: it runs `presence.py
  --face-event EVENT PHRASE PROJECT DETAIL` detached, and `face.log_event` writes only known
  events. Those lines carry the project in the body ("for webapp"): the envelope's is the helper's
  cwd, `[unknown]`. No `[face]` or `[ask]` line ends in `-> 'phrase'`, the shape every reader takes
  for speech; a test holds `log_stats` to zero barks for them.

**Ask Hobson** (`scripts/ask.py`; the menu bar's *Ask Hobson* submenu, `hobson ask`): the one
thing Hobson says because you asked, about every session at once. Three questions:

- **What needs me?** (`status`) Read from `session_status.reading` (session state, liveness, activity, a running nudge) and the salver,
  **no model**: who is waiting on you, what was held for your return (told, and taken off the
  salver, as a wave's answer does), what stopped on a failure in the last 2h with nothing since,
  then what finished in the last hour and what is working (a tool call in the last 2 min after its
  last Stop), as counts past one. Two told, the rest counted; nothing at all is "All quiet".
- **What failed?** (`failed`) The same reading, each failure with the last thing said of it.
- **What have you been doing?** (`recap`) `recap.across_projects`: the last 30 minutes, the three
  projects most recently at work, **one model call per project** inside one 25s budget, joined in
  code ("On webapp: …"). Asked about three at once, a 3B model blends them. The per-project
  prompt has no examples and says to use only the notes: given `build_prompt`'s engineer register,
  llama3.2:3b copied its example sentence verbatim into a project that had done neither, and
  invented file names ("odio dot P Y"). Every reply is checked before it is said
  (`_echoes_the_prompt`, `_not_in_the_notes`: two content stems the notes lack, beyond summary
  verbs), and one that fails, or a call that fails, is replaced by the session's own latest line,
  which is true because it was said. Measured live on the real log, 3 runs × 3 projects: nothing
  invented after the change; 4 of 9 replies replaced.

An answer is kind `answer`: never held (`kinds.Kind.never_held`: you are at the menu, whatever the
camera thinks), shown with no project in the corner. The face shows "One moment." (kind
`thinking`: whole at once, mouth still, eyes up and aside) at once for every question, even the
instant ones, which take 2–4s to be heard on Pocket TTS; it holds the window until the answer
replaces it, 25s at most. Muted or in quiet hours an answer is shown and not spoken, and with the
face off too (`asked` in the record, which the helper honours in `Controls.lineArrived` and in the
per-tick hide). It is spoken with `cold_start`, so the bark lock never drops it. Every step is an `[ask]` line: the question and where from, the answer with
what it was built from and, for the recap, each call's time or failure.

**Plugin notes** (`scripts/plugin_notes.py`): Claude Code's built-in plugin `cc-plugin-you-should-know`
leaves a note for a session now and then ("Heads up: …"), and Hobson reads it aloud. The source is
`~/.claude/plugins/store/cc-plugin-you-should-know_builtin-<hash>.json` (globbed, undocumented and
new, so every read is defensive): `sessions` holds one entry per session, and an entry with a `view`
(`kind: "offer"`, a one-sentence `line`, a Markdown `explanation`, `shownAt` in epoch milliseconds) is
a note. Its `id` is the transcript's name, and the first `cwd` in that transcript gives the project.

- **When.** At a Stop, after the Stop's own announcement (`BaseEngine._handle_plugin_notes`, the end of
  `run`): a note is offered during a turn, so a Stop follows it, and it needs no new process. Spoken as
  kind `note`, with `cold_start`: news, so presence holds it while you are away and the face shows it.
- **Once.** `claim` takes the notes not in `~/.claude/hobson-plugin-notes.json` (`(id, shownAt)` pairs,
  every note still in the store; only keys that have left it are pruned: past a cap the oldest were fresh
  again at every Stop) under a flock and records them *before* anything is spoken, so two Stops at once cannot
  both read one; a failed speak loses the note rather than repeating it. The first run (no state file)
  records every existing note as said, so an install never reads out old ones; a store that cannot be
  read, or is missing, does not count as a first run, so a store that appears later is its own baseline.
  A note over 12 hours old (the salver's age) is recorded and not said: one written after its turn's Stop
  can wait hours for the next. The same note in two store files (a plugin update leaves the old hash's)
  is one. Several at once: the oldest is said and the rest counted ("And one more, for webapp."), and the
  counted ones are never said.
- **What is said** is the note's own `line`, then one model sentence on what to do about it, only if that
  reply passes `recap._not_in_the_notes` and `_echoes_the_prompt`. The model is not asked to summarise:
  an earlier trial had llama3.2:3b say the whole note, in a butler's voice or plainly, and none of 15 to 25
  replies passed the check (its outputs were not kept, so that figure cannot be re-checked), and some
  reversed a cause ("pushes to production after the review check"). Asked only for the follow-up, on five
  synthetic notes, four tries each (n=20, one run, llama3.2:3b, `summary()` as shipped; the outputs are in
  the trail's `summary-rerun.json`), 10 passed the check and 10 fell back to the line alone, which is
  already the plugin's summary. An earlier run on the same notes kept 7, so expect 7 to 13 of 20. Of the
  10 kept, 6 read as right, 1 as vague and 3 as loose or wrong ("Skip payment tests if PAY SANDBOX KEY is not set
  initially", "Edit lock dot JSON to invalidate cache before bumping batch dependencies"). The check is about topic, not truth: it asks only that the reply's content words occur in the
  note, so a reversed instruction ("Run the drop before the copy.") passes it. None of 54 live follow-ups
  was reversed, which is a count of what happened, not a guarantee. The reply is never a hook's `generate_or_skip`: its budget and duplicate guards are for
  announcements nobody asked for, and a note must not go silent.
- **Another session's note** is filed under its own project: `speak` takes a `project`, which
  `presence.route` (the hold and the briefing) and the face's corner use. The phrase names it once
  ("On webapp: …") and the briefing does not name it twice. The project is the first `cwd` in the first
  200 lines of the session's transcript (`project_of`); a transcript line that is not an object is
  skipped, not the file.
- **Private.** A note is session text: only local Ollama sees it (never the decider), and its words stay
  out of `hobson.log` from every path. `note` is a private kind (`kinds.Kind.private`) and its phrase is a
  `log_record.Unlogged`, a string whose `repr()` is `note aaaaaaaa, 245 chars`; every phrase writer logs
  with `!r`, so the playback, the held and dropped lines, the briefing, the face's events (the helper
  reports the record's `logged` field) and the salver's rebuilt briefing text carry the placeholder, while
  the voice, the face and the salver get the words, and a TTS daemon's own log records a phrase's length, not its words. A note is never backfilled into the chatterbox cache
  (its phrase would sit on an argv forever), and recap does not read it as work.
- **A changed source** (a file that will not parse, or has no `sessions` list) yields nothing and logs one
  `[plugin-notes]` line per process; an entry missing a key is no note. An entry whose `view` fails
  validation, or whose `shownAt` is not a finite epoch in milliseconds (seconds look 1970-old), is skipped
  alone and logged once per process by its keys, never its values.
- **While muted** (`hobson._show_notes_silently`, beside H2's `_show_silently`): a silenced Stop still
  claims every new note, as above, and shows the first on the face with `silent=True`, its `line` only: no
  model, no engine, no words in the log (a test runs the entry under `-X importtime`). The log line says `note claimed`, `N notes claimed` and `note aaaaaaaa shown`. A wait
  that this Stop just put on the face keeps the window: the notes are claimed and
  not shown. With the face off they are claimed and not shown. A note is therefore never said after
  unmuting: muted, it is shown once and marked said.

**Quiet controls**: `silence_reason(config)` in `home.py` is the single gate, called by
`hobson.py` before any engine is loaded. It returns a reason string for: `muted`, a timed mute
(`mute_until`, an epoch set by `hobson off 30m`), or `quiet_hours` (`[start, end]` hours, wrapping
midnight). Everything downstream — including an in-flight nudge — checks it.

Silent is not blind. A Stop that arrives silenced is still read by the rules alone
(`hobson._read_silent_stop`: `works_on_its_own`, `awaits_developer`; no model, nothing said, no
nudge), and its category is recorded as `working`, `question` or unknown. Before, it was skipped
unread, and the last category Hobson knew stood in for every turn since. The user mutes so that a
meeting does not hear Hobson, not to stop him keeping track, and was muted 66 of 169 active hours in
three weeks. `hobson on`, when it ends a mute, says in one line who is still waiting on you
(`presence.on_unmute`, from `session_status.waiting_on_you`), never the backlog. A timed mute that
runs out has no such moment and says nothing. Every silenced line names its event (`silenced (muted):
Stop, read as question`), and `hobson off`/`on` log `[mute] on [for 30m]` / `[mute] off (N waiting on
you)`. Before these, the log could not say how long a mute lasted or what it covered.

Silent is not invisible either. While silenced, what waits on you is shown on the face without a
sound (`hobson._show_silently`): a permission request, a waiting notification (`idle_prompt`,
`agent_needs_input`, `permission_prompt`) and a Stop the rules read as `question`. The user mutes
for meetings, and Hobson used to show nothing then. Quiet hours behave the same as a mute, by the
user's decision, and so does a timed mute. The line is the fixed one the engines speak, chosen in
`scripts/announcement.py`, which both use (`BaseEngine._fixed_announcement` calls it) and which
imports no engine: a silenced PermissionRequest loads no engine, no model and no `phrase_gen`
(`tests/test_muted_face.py` drives one under `-X importtime`). A silenced Stop still imports
`stop_outcome` and `ollama_client` for the rules, and calls neither model, as before. Nothing
leaves the Mac: the destructive warning comes from `risk.py`'s local rules only
(`permission_risk(..., remote=False)`), and a command they cannot place, or rules that fail, get the
ordinary permission line. The same gates hold as for the voice: the `events` list (a malformed one,
such as `null`, turns every event off rather than raising) and `face.enabled` (off shows nothing).
Two more are the silenced path's own:

- **An idle prompt is shown only after a Stop read as `question`.** The engines nudge after an
  unknown ending too, but the silent read has no model: it cannot tell a finished turn from an
  unknown one, so every finished turn of a meeting would put up a "needs you" held for 10 minutes.
  `agent_needs_input`, permission requests, question dialogs and question Stops show regardless.
- **Claude Code's reminder of a request just shown is not shown** (its vaguer line would replace
  the specific one). A request the face will show writes
  `record_wait_shown` (`face.deliver` returned `SIGNALLED`, a running helper told, or `STARTED`, the
  helper launched, which reads the record at launch and shows the line; `face.show` is True only for
  the first, so the hook asks `deliver`). Nothing is written when face is off, the write failed, or
  the spawn was throttled with no helper running. It is read only here: `record_wait_said` stays the engines', because nothing was
  heard. A mute that ends inside the 20s echo window lets the reminder be spoken as before.

**Muting turns the camera off.** The helper quits after `exit_after` with no hook activity, and a
muted hook is not activity, so a silenced permission request in a long meeting found none running
and `face._signal` started one as configured: with `presence.mode: continuous`, the camera and its
green light, in the meeting the user muted for. Round one made a helper started while silenced face
only and left a running camera helper alone ("accepted"). The premise was wrong: `_record_alive` runs
before the silence check, and the helper counts the liveness files as activity, so a camera helper
running when the user muted stayed on for as long as any session worked. Now silenced
(`home.silence_reason`: a mute, a timed mute, quiet hours) means face only, for every helper:
`expected(config, silenced)` says so, `_matches` compares a running helper with it like any other
setting, and the one mismatch rule in `ensure_running` does the rest. A mismatched helper is stopped
and the new one started **in the same call**, once the old process is gone (`_exited`: `getpgid`,
polled for two seconds at most): `open` without `-n` hands a launch to a running instance of the
same bundle, so a start issued while the old helper still exits is lost, and the silenced hook that
shows a line needs a helper to read it now. If the old one is slow to go, nothing is started and the
next hook does it (the old rule). Unmuting is the same flip the other way: a face-only helper
that disagrees with the config is stopped and the full one started. With the face off there is no
face-only helper to turn into, so a silenced `ensure_running` stops a camera helper and starts none.

It runs within seconds of the change wherever one is made. `hobson off` and `hobson on` run
`presence.py --start` detached, which is `ensure_running`. A silenced hook calls `_ensure_presence`
too, after `_silenced_what`, so a timed mute or the start of quiet hours (no command at all) is seen
by the next hook, and its end by the next one after that; nothing in between needs the camera. It is
a stat and a read when nothing changes, and repeated hooks, silenced and then not, neither stop nor start anything
(`test_hooks_in_a_row_change_nothing_once_the_helper_matches`). The order matters for a hook that
also has a line: `face._signal` does not signal a camera helper while silenced (the line would go to
a helper that is being stopped) and falls through to `ensure_running`, which starts the face-only
one, and the line, already written, shows when it launches. `record_wait_shown` is written on any
truthy `deliver` (`SIGNALLED` or `STARTED`); only in this turning case does it depend on `STARTED`,
and it records nothing if `_exited` times out before the old helper is gone. `_ensure_presence` runs
after (`test_a_silenced_hook_shows_its_wait_before_it_reconciles_the_helper`): reconciled first, the
old helper unlinks its state, `_signal` finds no live helper and is throttled, and the wait is not
recorded as shown. Two hooks turning the helper at once both call `open`, one deleting the other's
fresh spawn marker, which is harmless: `open` without `-n` reuses the instance, and a second one
loses the flock and exits (`singleInstance` in `main.swift`). `_restart` (the menu's toggles,
`hobson presence ...`) stops a helper first only when not silenced; silenced, `ensure_running`
alone decides, so a face-only helper is left alone through a setting change and a camera one is
turned.

What the user gives up: while silenced, presence senses nothing (state `off`, no audience), so the
company line of H2 and of the notes cannot fire while muted (`presence.audience` was read there, and
no longer is): the face shows the full line even with someone in the room. Nothing else depends on
the camera then. Muted, Hobson speaks, holds and pushes nothing: the engines never run
(`silence_reason` returns before them), `presence.route` is reached only from `speak`, so neither
the salver nor `push.notify` is; nudge, recap, ask, `hobson say` and coach each check
`silence_reason` themselves; greetings, farewells, waves and a return briefing are the helper's
transitions, which a face-only helper does not produce, and `on_transition` skips the briefing
while silenced anyway. What was held before the mute stays on the salver for the next return. The
session list, its badge and the notes are not part of the sensing, and are still there in face only
(the menu hides only Preview, Presence and Answer Waves). After an unmute the full helper starts
fresh, so a return from away that straddled the mute is not briefed.

Away, coaching or unknown, a silent line is shown as it is and waits for you; a line shown while away is not
added to the return briefing. `face.show` can start the presence helper
(`presence.ensure_running`), face only while silenced. Nothing is recorded as voiced, since nothing was heard. The log line ends
`, shown`, `, not shown (face off)`, `, not shown (<error>)` or `, not signalled (see [face])`,
e.g. `silenced (muted): PermissionRequest, shown`, never with a phrase: the `[face]` line carries
it.

**UserPromptSubmit**: handled in `hobson.py` *before* config or engine work — it writes this
project's activity token (`nudge.activity_path()`, `~/.claude/hobson-activity-<key>`) and
returns. It never speaks; its only job is to cancel nudges the moment the user starts typing. Per
project on purpose: a single global token meant typing in one tab silenced every other session
that was waiting on you.

**Decider (Jev)** (`scripts/decider.py`): an optional remote classifier — Jev, via OpenRouter's
Decisions API — behind one seam, `decider.probability()`: P(label) for a `choice` question.
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
candidate phrase and up to six recently spoken phrases, each redacted. The commentary gate (see
Batching) sends the redacted batch summary.

**Phone push is the other place data leaves the Mac** (`scripts/push.py`; `hobson push`). Off by
default. With `push.backend` "ntfy", `presence.route` calls `push.notify` right after `hold()`, only
when the state is `away`: not `call`, not `company` (a buzz would say what the voice was kept from
saying), and only for the kinds in `push.kinds` (`waiting`, `broken`, `stalled`; a `done` can wait
on the salver). One POST to `push.url`: the body is a fixed sentence per kind from `push.body_for` ("webapp is
waiting on you.", "webapp stopped on a failure.", "Nothing has moved on webapp for 10 minutes.":
the minutes are the digits read from the watchdog's own line), the title the project label, and
nothing from the session. It is not the spoken phrase: on the daemon engines that is model output
written from the session's last message (the live log holds "Should I add a note on test [8]'s
cancel path?"), and a held phrase was never meant to leave the Mac. There is no switch for it. The token, `NTFY_TOKEN`, is read from the environment or
`hobson.env` by `home.read_env` (the decider's `find_key` uses the same reader), never from
`hobson.json`, never logged, and sent only where `home.https_or_loopback` allows. A redirect is
refused (`_NoRedirect`): urllib would forward the `Authorization` header to wherever it points. A
push is refused for want of a token even to a self-hosted server, since a topic URL is guessable.
The hook never waits: `notify` claims a slot in `hobson-push.json` (one push per project and kind in
10 minutes, which folds a session's repeated waits into one buzz; `push.per_hour` an hour in all,
default 6), then starts `push.py --send` with the payload on its standard input, in a session of
its own (`start_new_session=True`, so it outlives the hook; `nudge.py` is spawned differently). The hourly slot is
given back, under the same lock, when no server answered (the child cannot start, the connection
fails or times out) or it answered with a 5xx, so an outage while away does not use up the hour. Any
answer under 500 (a redirect or a 4xx) keeps it (a revoked token's 401, ntfy rate-limiting the client's 429): the server answered, and a
retry per held phrase would only be told no again (12 notifies against a 401 stand-in made 12
requests before this, against 200 one). The project-and-kind collapse is never given back, so
whatever fails is retried at most once per project and kind in 10 minutes. Only `waiting`,
`broken` and `stalled` have a sentence (`body_for` has no catch-all): any other kind in
`push.kinds` ("done"), or a `kinds` that is not a list, is ignored with one `[push] not sent`
line, and a `push.url` that is not a string is no url. Timestamps in the future (a clock stepped
back) are dropped rather than kept. `hobson push on` refuses a malformed URL or plain http off this machine, and
`route` returns what it did without push whatever push raises. The child makes the request and writes one `[push]` line, `sent (waiting,
200)` or `failed (waiting): HTTP 401`, a status or an exception's class and never its text, which
could carry the URL. No `[push]` line ends in `-> 'phrase'`, and recap reads them as Hobson's own
talk. `hobson push test` sends one synchronously. Tested over real HTTP against a loopback
stand-in, the detached child included (`tests/test_push.py`). Remote approve and deny are not
here: they need a blocking hook, which Claude Code ships natively.

**The decider is one of two places data leaves the Mac (phone push, above, is the other), and the one that sends session text.** A question's state is a list of parts: a plain
string is Hobson's own framing, sent as written; session text goes as a `decider.Command`,
`Summary` or `Phrase`, and `decider.redact()` runs on it by kind, to the length its question was
calibrated on (600, 220, 200 characters). Redaction used to be each caller's job and lived in
`risk.py`; the duplicate check skipped it, so spoken phrases went out as spoken. The key goes
only to an https endpoint (plain http only on loopback); an endpoint that does not parse at all
(`http://[::1`) is refused the same way, with the reason "not a valid URL", where it used to raise
out of `probability` and `check_key`. Tests see what left through the `jev`
fixture, which records each request after redaction: stubbing a caller's decider call would
test the state before it.

**Swapping the decider's model.** Another model on OpenRouter's Decisions API takes the same
request, so it needs only `decider.model` (Solar Decide, `upstage/solar-decide`, answered all three
questions through the unchanged parser). But every decider threshold (the gate's, the dedup pair's
and the risk check's) was fitted on `typesafe/jev-1.13`'s probabilities and has to be re-checked
first, and the hook's timeout is the other gate: a call that times out falls back to local, so a
model too slow from a hook is no use however accurate it is. Measure latency from a hook, not from
a vendor's table. Cloudflare's Clef and Clef-flash take the same request body but are not on
OpenRouter: Workers AI puts the account id in the URL path, wraps the answer in a `result` object
and reports tokens, not a cost. They would need a second backend here, and a cost worked out from
tokens for the `cost=` log line that `hobson monitor` sums.

**Permission risk** (`scripts/risk.py`): a destructive Bash permission request is announced as
"Careful — this one deletes files. It needs your approval." — a fixed sentence, never the model,
and never deduped. Three tiers: local rules (any `rm`, force-push, `reset --hard`, `DROP TABLE`
through a DB client, …) are instant and final; a strictly read-only command is cleared locally;
only the rest goes to the decider, as a `decider.Command` (redacted: no heredoc bodies or long
quoted text; URLs, hosts, IPs, emails, secrets, long tokens and every path but its last component
replaced), warning at P(destructive) ≥ `decider.permission_risk_min` (0.5). Rules match with quoted
text and heredoc bodies blanked, except code handed to an interpreter (`bash -c`, `python3 - <<EOF`)
and command substitutions, which run wherever they are written: blanking a double-quoted string
whole let `echo "$(rm -rf ~)"` pass as read-only, with no warning. `_code_only` keeps every
`$(…)` and backtick, in double quotes and unquoted heredocs too, and reads each heredoc body in its
own language, so a Python string's backticks are not taken for the shell's.
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
go silent just because it resembles something said earlier. Neither recap reads Hobson's own
talk as work (`_own_talk`): a playback of kind `answer`, `thinking`, `briefing`, `nudge` or
`stalled`, and any `[presence]`, `[face]`, `[ask]`, `[nudge]` or `[watchdog]` line, and the phrase
such a line carries wherever else it appears. Before, a briefing was retold as something a session
had done, and each recap would have summarised the last. `hobson stats`
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
have a formatter and a parser side by side: playback (`barked (source) [kind] -> 'phrase'`, the kind
absent on older lines), outcome
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

- **The server is the security boundary.** A `loopback.Server` on 127.0.0.1, random port, behind
  the guard it shares with the TTS daemons (`scripts/loopback.py`): a `127.0.0.1`/`localhost`
  Host header (DNS rebinding) and a same-server Origin on every request, and on every `/api` call
  the random token from the window's URL (`X-Hobson-Token`, compared in constant time); static
  files are an allowlist (`STATIC`, plus clips matching `CLIP`). It writes config, saves a key and
  runs installers, and any web page in a browser can send requests to localhost — do not loosen
  any of it. It exits with the window, or after `IDLE_EXIT_SECONDS` (1800) idle with no task
  running.
- **What reaches the config.** `clean_answers()` drops anything off-catalogue: engine, events and
  verbosity from fixed lists, a voice only as a catalogue id for that engine (`VOICES`, never a
  path; a custom voice already in the config is shown as YOURS and kept), a model only if it matches
  `MODEL_NAME` (any Ollama name — the brain screen takes any model, pulled on COMMIT), the phone
  only when the page answered it. `home.apply_settings()` writes only values that differ from
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
- **Its pictures** (`setup/ui/art.js`): each module is the part of the brain that does its job, and
  its picture is an old anatomical plate of that part: Broca's area (VOICE), the reflex arc
  (EVENTS), the cortex (BRAIN), the striatum (DECIDER), the visual pathway (PRESENCE), the
  cerebellum (PHONE), the hippocampus (COMMIT). All seven are public-domain engravings (Vesalius,
  Descartes, Gray's *Anatomy*; `setup/ui/art/`, credited in `art/LICENSES.md`), prepared once at
  560×370, inverted to white on black. The plate is the pane's header, with the title set over
  it. Every frame goes through one treatment: 280×185 greyscale, dithered to five tones of the
  module's tint (plates through interleaved gradient noise, drawn scenes through a Bayer
  matrix). The page draws a ring on the region, a leader and its name over the plate, at least
  2px wide, or the dither breaks the dashes up. The band fades to nothing at every edge, so the
  label points into the middle and a new plate needs its region inside it. COMMIT's ring fills
  as the tasks finish; PHONE shows a phone drawn in code (face up, face down or unread) once one is picked.
  Nothing from a film is shipped, however it is filtered: a dithered frame is still a copy of that
  frame. A new still goes in `STATIC`, or the server refuses it and the band stays black;
  `test_every_picture_art_js_shows_is_served` catches that.
- **Between screens** (`go()` in `wizard.js`, `City.arrive` in `city.js`): each module is a place
  down the street, and going to one is a flight, about 0.85s. The screen you leave is lifted out
  of the layout (`leave()`, a `.leaving` copy keeping its tint and a still of its plate) and rushes past
  the camera while the city surges (`City.fly`) and the new tint runs down the street from the
  horizon. The next screen is laid out at once, hidden, and comes up the street on `#fly` as a
  wireframe of itself: outline, a rule for each row, its title in outline on the page's own
  baseline, and its plate, whose detail (`Art.detail`) follows the distance, coarse blocks far
  off. It scales as 1/z about the vanishing point and is re-measured every frame, so a pane that
  changes on the way (COMMIT's plan) is still what lands. Landed, the outline cools into the pane
  while the rows print a whole row at a time (`PRINT`) and Hobson's line types. Back is the same
  flight reversed: the old screen falls away up the street, the new one settles from behind the
  camera, already sharp. Any key lands a flight at once; reduced motion has none of it. It
  replaced a cutscene (the plate full-screen, then shrunk into its band) that the user found
  amateurish: nothing takes the screen over now.
- **The look**: the neon city, the ANSI block logo with its RGB split, the rail and the typed
  `HOBSON>` line are the identity. The rest is kept quiet on purpose. One tint per screen, which
  recolours the city too (the hero and the finale keep every colour); red means a failure and
  nothing else. League Gothic (bundled, OFL) for the titles, the system mono at two sizes: 11px
  for labels, tracked 0.06–0.12em, and 14px for everything read, untracked. Options are rows
  between rules, not boxes: the choice is tint text with a bar on the left, the keyboard's cursor
  is inverse video like the current module in the rail, and hovering only brightens a row. Four
  glows (Hobson's line, the title, the finale's word, the logo), no frosted glass, corner
  brackets or window dots, plain words on buttons (TEST KEY, INSTALL). Check it at 960×640 as
  well as 1120×740: the logo shrinks with its column there, and the survey's dots give way
  before its values. The earlier version had every sci-fi-terminal trope switched on at once and
  read as generated.
- **Developing it**: the page runs against a mock backend (`setup/ui/mock.js`) at
  `index.html?mock`, `?mock=update` or `?mock=bare`, in any browser. A real run writes
  `~/.claude` and `settings.json`, and the hooks it installs point at the checkout it ran from, so
  run a real one in the sandbox: `scripts/sandbox.sh new` installs a snapshot of the working tree
  through `install-remote.sh` into a scratch HOME, in an empty environment, and the wizard opens;
  `shell` is a shell there (`hobson setup`, `hobson uninstall`), `check` compares your own install
  with the baseline `new` took, `destroy` removes it. A scratch HOME alone is not a sandbox: an
  uninstall run in one revoked the real camera grant (`tccutil` acts on the macOS user). So the
  sandbox's sensor is built as `local.hobson.presence.sandbox`, shims pass only its `tccutil`
  reset and Homebrew's read-only verbs, and `new` warns about the rest (Ollama's model store, the
  TTS daemons' ports).

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

**Cache key**: `bark_hash(text)` = SHA-256 first 16 hex chars. Defined once, in `bark_templates.py`; the engines and every cache-gen script import it.

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
  "presence": { "enabled": true, "mode": "signals|auto|continuous", "idle_seconds": 60, "away_after": 30, "greetings": true, "waves": true, "preview": false, "paused": false, "exit_after": 1800, "call_apps": [], "phone": null, "adb": null },
  "face": { "enabled": true, "commentary": false, "hold_waiting": true, "width": 280 },
  "push": { "backend": "off|ntfy", "url": "", "kinds": ["waiting", "broken", "stalled"], "per_hour": 6 },
  "focus": { "enabled": true, "min_tool_calls": 2, "min_seconds": 15.0, "cooldown": 4.0, "min_words": 2, "max_words": 6 },
  "decider": { "backend": "local|jev", "model": "typesafe/jev-1.13", "timeout_ms": 4000, "dedup_restates_max": 0.5 }
}
```

`commentary.repeat_window` is anomaly mode's lookback for "back on the same thing again".
`commentary.chattiness` (0–1) is read only with the decider on: a batch speaks at
P(worth) ≥ 1 − chattiness.

`mute_until` is an epoch timestamp (0 = not muted); `quiet_hours` is `[start_hour, end_hour]` and
wraps midnight. Both are read only through `silence_reason()`.

`load_config()` in `home.py` merges user config with `DEFAULT_CONFIG` (`with_defaults`: shallow top-level, deep merge for sub-dicts; `BaseEngine` applies the same merge to whatever config it is given, so no engine read carries a fallback of its own). It also migrates the deprecated `kokoro.realtime_events` key to the top-level `events` key.

Every write to `hobson.json` goes through `home.update_config({dotted: value})` (or
`write_config` for `hobson config reset`): under a lock (`hobson-config.lock`), written whole to a
temp file and `os.replace`d (through a symlink, keeping the mode), with a value equal to its default
removed rather than written, so the default keeps flowing. The CLI calls it as `home.py --set KEY
VALUE` or through `config_py`, its arguments in `sys.argv`, never pasted into Python source.
It reads the same way, `config_get KEY` (`home.py --get`, through `load_config`), so it keeps no
copy of a default: one left at its default is not in the file.
Every state file is written the same way, `home.write_atomic` / `write_json_atomic`: a temp
file, `os.replace`, the temp removed however it fails, and with a `mode` never wider than 600
before it has it (the face record, `hobson.env`). `loopback.issue_token` keeps its own `O_EXCL`.
The one exception is `install.sh`'s first write of a new install, which must leave a file behind
(the installer reads one as "installed before"), and copies an invalid one aside itself. Before,
13 inline writers truncated the file and rewrote it, and a hook reading in between took the
half-written file for no file at all: unmuted, every default. An unreadable file is kept aside as
`hobson.json.unreadable-<time>`, not written over.

## Key paths

Every path under `~/.claude` comes from `scripts/home.py`, resolved from `$HOME` each time it is
asked for (`state_dir()`), so a test isolates all of it by setting HOME. The shell scripts keep their
own copies; the TTS daemons take theirs from `home.py` through `tts_daemon.py`.

- Config: `~/.claude/hobson.json` (engine, personality, events, cooldown, volume, Ollama models, per-engine settings), written only through `home.update_config`/`write_config` under `hobson-config.lock`; an unreadable one is kept as `hobson.json.unreadable-<time>`
- Personalities: `scripts/personalities/<name>/personality.json` (templates)
- Presets: `scripts/presets.json` (one-shot config appliers)
- Hooks: injected into `~/.claude/settings.json` by `scripts/settings-merge.py`
- Lock: `~/.claude/hobson.lock` (file-based lock + cooldown timestamp)
- Commentary lock: `~/.claude/hobson-commentary.lock`; `hobson-focus.lock`, the same for watched flushes only
- Voice lock: `~/.claude/hobson-voice.lock` (held by the player for as long as a clip plays, `voice.py`); `hobson-reading.lock` (held by `hobson say` for a whole reading, and by `hobson coach`); `hobson-coaching.lock` (held by `hobson coach` for a whole run: others' announcements are held meanwhile)
- Nudge/watchdog locks: `~/.claude/hobson-nudge-<key>.lock`, `hobson-watchdog-<key>.lock` (`home.project_file`; `<key>` is `home.project_key`)
- Session state: `~/.claude/hobson-sessions/<hash>.json` (pending queue, fingerprints, recent phrases), with `<hash>.lock` beside it for `transaction()`
- Liveness: `~/.claude/hobson-alive-<key>`, one per project (last tool call or permission request: time, event, tool, timeout, and the terminal identity from that call -- the tmux pane, or outside tmux the tty -- never the call's contents; read by the watchdog and by the face's Take me there button, `scripts/terminal_jump.py`)
- Activity token: `~/.claude/hobson-activity-<key>`, one per project (written by the UserPromptSubmit hook; cancels that project's nudge)
- Plugin notes: `~/.claude/hobson-plugin-notes.json` (the notes already said, `{"said": [[session, shownAt], …]}`) and `hobson-plugin-notes.lock`; the source it reads is the plugin's store, `~/.claude/plugins/store/cc-plugin-you-should-know_builtin-*.json`, which Hobson never writes
- Presence: `~/.claude/hobson-presence.json` (the helper's state, rewritten every second; removed when it exits), `hobson-presence.lock` (one helper), `hobson-presence.spawn` (spawn throttle), `hobson-presence-lines.json` (the last greeting/farewell said, and when); the salver `hobson-held.json` + `hobson-held.lock`; the helper at `build/HobsonPresence.app` (gitignored)
- Face: `~/.claude/hobson-face.json` (the last line for the window, mode 600, read by the helper at SIGUSR2); the page at `presence/face/`, the window in `presence/Face.swift`, built into the same helper
- Setup record: `~/.claude/hobson-setup.json` (the wizard sections this install has seen); the wizard's page at `setup/ui/` (its voice clips in `setup/ui/voice/`), its window at `build/HobsonSetup.app` (gitignored)
- Decider key: `~/.claude/hobson.env` (`OPENROUTER_API_KEY=…`, mode 600; read by `decider.find_key()`, never logged); the phone push token is `NTFY_TOKEN=…` in the same file (`push.find_token()`); its rate limit is `hobson-push.json` + `hobson-push.lock`
- Log: `~/.claude/hobson.log` — `[YYYY-MM-DD HH:MM:SS] [project] [engine] msg` (engine lines) or `[…] [project] msg`; older lines have only `HH:MM:SS`. Written and read only through `scripts/log_record.py`
- Daemon pid/log/token: `~/.claude/{kokoro,pocket-tts}-daemon.{pid,log,token}` (the token mode 600, written by the daemon once it holds its port); playback scratch WAVs at `~/.claude/{kokoro,pocket-tts}-playback-*.wav`, one per phrase, removed when it has played (and by the next phrase after an hour, if its player was killed)
- Cache: `~/.claude/voice-cache-chatterbox/` (pre-gen); the daemon engines keep none
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
hobson voice [name]        # Switch the engine's voice, kokoro-realtime or pocket-tts (picker: the wizard's VOICES)
hobson test                # Play a test bark
hobson recap [minutes]     # Speak a summary of recent activity (default 10m)
hobson say [TEXT|-f FILE]  # Read text aloud (or standard input), a passage at a time; returns when done
hobson coach [-f FILE]     # Run spoken steps on Hobson's clock: say, wait, countdown, ready (NDJSON out)
hobson presence [status|on|off|mode [signals|auto|continuous]|greetings on|off|waves on|off|preview on|off|phone [auto|off|SERIAL]|look|setup|stop]
hobson face [status|on|off|test [LINE] [KIND]]  # the window where Hobson says each line
hobson push [status|on URL|off|test]  # send what is held while you are away to the phone (ntfy)
hobson ask [status|recap|failed]  # What needs me? / What have you been doing? / What failed?
hobson lines [category]    # Show voice lines (from active personality)
hobson monitor             # Watch bark activity in real time (and decider spend to date)
hobson stats               # Show what was spoken, queued, and suppressed
hobson doctor              # Run diagnostics (config, hooks, personality, deps, Ollama, daemon, decider, disk, performance tips)
hobson config show         # Pretty-print full config with defaults
hobson config reset        # Back up and reset config to defaults
hobson cache-gen [--force] # Generate voice cache for current engine
hobson setup               # The setup wizard: every choice in one window, written on COMMIT
hobson setup kokoro|pocket-tts|chatterbox  # Install one engine's venv + models, no window
hobson daemon start|stop|status # The engine's TTS daemon (kokoro-realtime, pocket-tts)
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

Coverage is report-only (no failing threshold). The kokoro / pocket-tts backends (model load and
synthesis) and the daemon's real rendering are intentionally uncovered — they need ML models; the
runner both backends share, its guard included, is tested over real HTTP with a stand-in. So is the presence helper (`presence/main.swift`, and the face's window, `presence/Face.swift`): it needs a camera and a desk, and is
checked by hand with `HobsonPresence --signals`, `hobson presence look` and a live run; the face's page is laid out in headless Chrome, where there is one (`tests/test_face_layout.py`, skipped without it). The suite is **1524 tests** and runs in about 40 seconds, three of them `tests/test_daemon_cli.py`
driving the real CLI against a stand-in daemon on loopback (two `hobson say` runs at once, with a
stand-in afplay that logs when each clip starts and ends) and `tests/test_voice.py` timing real players; if it takes much longer, something is
reaching the network.

A test must not depend on the terminal it runs in. `no_audio` patches `subprocess.Popen` on the shared
module, so `subprocess.run` is faked too, and outside tmux `nudge.terminal_info` walks the process tree
with `ps` through it. The fake once lacked the context-manager protocol, and the four tests that record
liveness passed in tmux (`$TMUX_PANE` short-circuits the walk) and failed in CI. Run the suite with
`env -u TMUX -u TMUX_PANE` as well.

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
- `bark_hash()` (in `bark_templates.py`, the only definition) -- changing it breaks all caches
- `settings-merge.py` identifies our hooks by their entrypoint, `…/scripts/hobson.py` in the command (plus the legacy `claude-bark` / `voice-bark` names) -- hook commands must keep that path. Not the bare word: a bare name claims the hooks of anyone whose home is named after it, and uninstall deletes them -- and Hobson is a surname. Ownership is per hook, not per matcher group (`_without_ours`): a foreign hook in the same entry as ours stays, where dropping the entry deleted it. Every cleanup of an old link checks for our `scripts/settings-merge.py` first, so another tool's command is never touched
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
- Changing personality invalidates chatterbox cache (different template text = different audio)
- `chatterbox.py` reference audio lookup: tries `<personality>-reference.wav`, then `hobson-reference.wav`, then `reference.wav`
