// Hobson setup — the wizard's screens. Everything it learns about this Mac
// comes from /api/probe, and everything it does goes through /api (served by
// scripts/setup_wizard.py, which holds all the rules). This file only asks
// and shows: it decides nothing about what gets written.
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const Q = new URLSearchParams(location.search);

// ── Transport ───────────────────────────────────────────────────────────

function realApi(token) {
  const headers = { "Content-Type": "application/json", "X-Hobson-Token": token };
  async function check(r) {
    if (r.ok) return r;
    let msg = String(r.status);
    try { msg = (await r.json()).error || msg; } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  return {
    async get(path) { return (await check(await fetch("/api/" + path, { headers }))).json(); },
    async post(path, body) {
      return (await check(await fetch("/api/" + path, { method: "POST", headers, body: JSON.stringify(body || {}) }))).json();
    },
    // Newline-delimited JSON, one event per line, until the server closes.
    async stream(path, body, onEvent) {
      const r = await check(await fetch("/api/" + path, { method: "POST", headers, body: JSON.stringify(body || {}) }));
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (value) buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (line) onEvent(JSON.parse(line));
        }
        if (done) break;
      }
    },
  };
}

let api = null;

// ── Sound ───────────────────────────────────────────────────────────────
// Tiny square-wave blips, the way a 1995 terminal would. M toggles them;
// the choice is remembered per browser when storage allows it.

const Sfx = {
  on: true,
  ac: null,
  init() {
    try { this.on = localStorage.getItem("hobson.sfx") !== "off"; } catch (_) { /* storage blocked */ }
  },
  toggle() {
    this.on = !this.on;
    try { localStorage.setItem("hobson.sfx", this.on ? "on" : "off"); } catch (_) { /* storage blocked */ }
    paintTop();
  },
  tone(f0, f1, dur, type, vol) {
    if (!this.on) return;
    try {
      this.ac = this.ac || new (window.AudioContext || window.webkitAudioContext)();
      if (this.ac.state === "suspended") this.ac.resume();
      const t = this.ac.currentTime;
      const o = this.ac.createOscillator();
      const g = this.ac.createGain();
      o.type = type || "square";
      o.frequency.setValueAtTime(f0, t);
      if (f1) o.frequency.exponentialRampToValueAtTime(f1, t + dur);
      g.gain.setValueAtTime(vol || 0.02, t);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      o.connect(g).connect(this.ac.destination);
      o.start(t);
      o.stop(t + dur + 0.02);
    } catch (_) { /* no audio */ }
  },
  move() { this.tone(1500, 0, 0.016, "square", 0.01); },
  pick() { this.tone(520, 1560, 0.08, "square", 0.018); },
  back() { this.tone(900, 280, 0.08, "square", 0.016); },
  err() { this.tone(170, 80, 0.22, "sawtooth", 0.028); },
  tick() { this.tone(2600, 0, 0.005, "square", 0.005); },
  ok() { this.tone(660, 0, 0.06, "square", 0.018); setTimeout(() => this.tone(990, 0, 0.09, "square", 0.018), 70); },
};

// ── Copy ────────────────────────────────────────────────────────────────

// Each module is the part of the brain that does its job, and its picture
// is that part (art.js).
const MODULES = [
  { id: "voice", n: "01", title: "VOICE", region: "Broca's area" },
  { id: "events", n: "02", title: "EVENTS", region: "reflex arc" },
  { id: "brain", n: "03", title: "BRAIN", region: "cortex" },
  { id: "jev", n: "04", title: "DECIDER", region: "striatum" },
  { id: "presence", n: "05", title: "PRESENCE", region: "visual pathway" },
  { id: "phone", n: "06", title: "PHONE", region: "cerebellum" },
  { id: "commit", n: "07", title: "COMMIT", region: "hippocampus" },
];

const ENGINES = {
  "say": { name: "SAY", desc: "Built into macOS. Instant, nothing to download, a bit robotic.", q: 2, size: "0 MB", how: "instant, built into macOS" },
  "kokoro-realtime": { name: "KOKORO", desc: "A neural voice on this Mac. Each phrase is written live for what just happened.", q: 4, size: "~340 MB", how: "live, written by the brain" },
  "pocket-tts": { name: "POCKET", desc: "Kyutai's Pocket TTS. Warmer than Kokoro, heavier, and slow to wake after a pause.", q: 5, size: "~1 GB", how: "live, warmer, slow to wake" },
  "chatterbox": { name: "CHATTERBOX", desc: "Clones a voice from a 10-second sample, and speaks a fixed set of phrases.", q: 5, size: "~2 GB", how: "fixed phrases, cloned voice" },
};

const EVENTS = [
  ["stop", "FINISHED", "When Claude finishes a turn, fails, or asks you something."],
  ["permission", "PERMISSION", "When Claude needs your approval."],
  ["notification", "WAITING", "When Claude is waiting on you, with a few reminders."],
  ["commentary", "COMMENTARY", "A word now and then on what Claude is doing."],
];

const VERBOSITY = {
  terse: "Only the notable moments.",
  normal: "Meaningful actions only.",
  chatty: "A fixed line per tool call.",
  anomaly: "Only the surprises, like repeats and first touches.",
};

const PRESENCE = {
  signals: { name: "SIGNALS", desc: "Keyboard, screen lock, display sleep and calls. No camera." },
  auto: { name: "AUTO", desc: "A two-second camera look before speaking." },
  continuous: { name: "CONTINUOUS", desc: "A frame a second. Notices when you leave and when you return. Try waving!" },
  off: { name: "OFF", desc: "No sensing. Hobson speaks whether anyone is there or not." },
};

const LOGO = (() => {
  const L = {
    H: ["██╗  ██╗", "██║  ██║", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    O: [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    B: ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██████╔╝", "╚═════╝ "],
    S: ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
    N: ["███╗   ██╗", "████╗  ██║", "██╔██╗ ██║", "██║╚██╗██║", "██║ ╚████║", "╚═╝  ╚═══╝"],
  };
  return [0, 1, 2, 3, 4, 5].map((i) => "HOBSON".split("").map((c) => L[c][i]).join("")).join("\n");
})();

// ── State ───────────────────────────────────────────────────────────────

const S = {
  probe: null,
  flow: null, // "express" | "custom" | "update"
  route: [],
  screen: "boot",
  visited: new Set(),
  a: {}, // the answers
  jev: { state: "idle", msg: "", other: false },
  camera: { busy: false, msg: "" },
  phone: { pos: "unknown", test: null },
  plan: null,
  run: null, // { tasks: {id: {state, progress, detail}}, done, ok }
  typing: 0,
};

function initAnswers() {
  const p = S.probe;
  const base = p.existing || p.recommend;
  S.a = {
    engine: base.engine,
    voice: base.voice || null,
    personality: base.personality,
    events: base.events.slice(),
    verbosity: base.verbosity || "normal",
    model: base.model,
    // Asked, never assumed: no preference until you pick one, unless you
    // already picked on an earlier run.
    decider: p.seen.includes("jev") && p.existing ? p.existing.decider : null,
    jev_verified: false,
    key_typed: false,
    presence: base.presence,
    greetings: base.greetings !== false,
    phone: base.phone || null,
  };
}

// ── Little renderers ────────────────────────────────────────────────────

const meter = (n, of) => `<span class="meter">${"<i>▰</i>".repeat(n)}${"<u>▱</u>".repeat((of || 5) - n)}</span>`;
function bar(p, w) {
  w = w || 26;
  const f = Math.max(0, Math.min(w, Math.round(p * w)));
  return `<span class="bar">[<i>${"█".repeat(f)}</i><u>${"░".repeat(w - f)}</u>] ${String(Math.round(p * 100)).padStart(3, " ")}%</span>`;
}
const tag = (text, cls) => `<span class="tag ${cls || ""}">${esc(text)}</span>`;
const persona = (id) => S.probe.personalities.find((x) => x.id === id) || { id, name: id, desc: "", sample: "" };
const model = (id) => S.probe.models.find((m) => m.id === id);
const installed = (id) => S.probe.ollama.models.includes(id) || (!id.includes(":") && S.probe.ollama.models.includes(id + ":latest"));
// What Ollama accepts as a model name: library (mistral:7b), namespaced
// (user/model), or Hugging Face (hf.co/user/repo:Q4_K_M). Checked again by the server.
const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:\/@+-]{0,199}$/;
const inCatalogue = (id) => S.probe.models.some((m) => m.id === id || m.id + ":latest" === id);
const fmtGB = (g) => (g >= 1 ? `${g.toFixed(1)} GB` : `${Math.round(g * 1024)} MB`);

function log(msg) { console.debug(`[setup] ${msg}`); }

// One light per screen: the module's colour leads the page and the city.
// The hero and the finale have no module, so they get every colour.
function tint(screen) {
  const c = TINTS[screen] || (screen === "final" ? TINTS.commit : TINTS.voice);
  const root = document.documentElement.style;
  root.setProperty("--tint", c);
  root.setProperty("--tint-rgb", [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)).join(", "));
  if (TINTS[screen]) City.tint(c);
  else City.tint(null, { full: true });
}

function toast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// Every line the wizard says, by key (lines.json, loaded before boot).
let LINES = {};
const L = (key) => LINES[key] || "";

// The wizard's own voice: recorded clips (voice/manifest.json, made by
// scripts/cache-gen/setup_voice_gen.py), played as they are. A line with no
// clip goes to macOS `say` through the server. V toggles it.
const Voice = {
  on: true,
  manifest: { lines: {}, auditions: {} },
  audio: null,
  init() {
    try { this.on = localStorage.getItem("hobson.voice") !== "off"; } catch (_) { /* storage blocked */ }
  },
  async load() {
    try { this.manifest = await (await fetch("voice/manifest.json", { cache: "no-store" })).json(); } catch (_) { /* no clips: say it is */ }
  },
  toggle() {
    this.on = !this.on;
    if (!this.on) this.stop();
    try { localStorage.setItem("hobson.voice", this.on ? "on" : "off"); } catch (_) { /* storage blocked */ }
    paintTop();
  },
  stop() { if (this.audio) { this.audio.pause(); this.audio = null; } },
  play(src) {
    this.stop();
    const a = new Audio(src);
    this.audio = a;
    return new Promise((resolve) => {
      a.onended = a.onerror = () => resolve();
      a.play().catch(() => resolve());
    });
  },
  // Hobson narrating the wizard.
  say(text) {
    if (!this.on || !text) return Promise.resolve();
    const clip = (this.manifest.lines || {})[text];
    if (clip) return this.play(clip);
    return api.post("speak", { text, personality: "hobson" }).catch(() => {});
  },
  // What the chosen engine would sound like: Kokoro from its clips (it isn't
  // installed yet), the rest through `say` in the personality's voice.
  audition(text, engine, voice, personality) {
    const byVoice = (this.manifest.auditions || {})[engine] || {};
    const fallback = ((S.probe.voices || {})[engine] || [])[0];
    const clip = (byVoice[voice] || {})[text] || (fallback && (byVoice[fallback.id] || {})[text]);
    if (clip) return this.play(clip);
    return api.post("speak", { text, personality }).catch(() => {});
  },
};

// ── Top bar, rail ───────────────────────────────────────────────────────

function paintTop() {
  $("#t-sfx").textContent = Sfx.on ? "[M] BLIPS ON" : "[M] BLIPS OFF";
  $("#t-voice").textContent = Voice.on ? "[V] VOICE ON" : "[V] VOICE OFF";
}

// Three states: current (inverse video), done (■), still to come (dim).
// Express has settled every module but the Decider; Update keeps the rest.
function moduleState(id) {
  if (S.screen === id) return ["on", ""];
  const settled = S.flow === "express" ? id !== "commit" && (id !== "jev" || S.a.decider)
    : S.flow === "update" ? !S.route.includes(id) : false;
  return settled || S.visited.has(id) ? ["done", "■"] : ["", ""];
}

// Only the keys that do something on this screen.
function paintKeys() {
  const k = (key, what) => `<span><kbd>${key}</kbd>${what}</span>`;
  const keys = [k("↑↓←→", "MOVE"), k("⏎", "SELECT")];
  if (S.screen === "events") keys.push(k("SPACE", "TOGGLE"));
  if (S.screen !== "boot" && S.screen !== "final") keys.push(k("ESC", "BACK"));
  if (S.screen === "voice") keys.push(k("P", "PREVIEW"));
  $("#keys").innerHTML = keys.join("");
}

function paintRail() {
  const rail = $("#rail");
  const items = MODULES.map((m) => {
    const [cls, st] = moduleState(m.id);
    const locked = S.run && !S.run.done ? " locked" : "";
    return `<div class="mod ${cls}${locked}" data-rail="${m.id}"><span class="n">${m.n}</span><span>${m.title}</span><span class="st">${st}</span></div>`;
  }).join("");
  rail.innerHTML = `<div class="rail-h">MODULES</div>${items}
    <div class="rail-foot">Nothing is written until COMMIT.</div>`;
  paintKeys();
}

// ── Screens ─────────────────────────────────────────────────────────────
// Each returns { path, say, body, foot }. `say` is typed out once, when the
// screen is entered; body and foot are repainted on every change.

function footNav(opts) {
  opts = opts || {};
  const back = opts.noBack ? "" : `<button class="btn ghost fx" data-act="back"><span class="k">ESC</span> BACK</button>`;
  const note = opts.note ? `<span class="note">${opts.note}</span>` : "";
  const label = opts.label || "NEXT";
  const dis = opts.disabled ? " disabled" : "";
  return `${back}<span class="sp"></span>${note}<button class="btn go fx primary" data-act="${opts.act || "next"}"${dis}>${label} <span class="k">⏎</span></button>`;
}

const SCREENS = {};

SCREENS.voice = () => {
  const cards = `<div class="eng-h"><span></span><span>ENGINE</span><span>QUALITY</span><span>HOW</span><span class="sz">DOWNLOAD</span><span></span></div>` + Object.keys(ENGINES).map((id) => {
    const e = ENGINES[id];
    const info = S.probe.engines.find((x) => x.id === id) || {};
    const on = S.a.engine === id;
    return `<button class="opt row eng fx${on ? " sel" : ""}" data-act="engine" data-arg="${id}" data-group="engine">
      <span class="box">${on ? "(●)" : "( )"}</span><span class="t">${e.name}</span><span>${meter(e.q)}</span>
      <span class="how">${e.how}</span><span class="sz">${e.size === "0 MB" ? "—" : e.size}</span>
      <span class="r">${info.ready ? '<span class="ok">installed</span>' : ""}</span>
    </button>`;
  }).join("") + `<p class="eng-desc">${ENGINES[S.a.engine].desc}</p>`;
  const chips = S.probe.personalities.map((p) => {
    const sel = S.a.personality === p.id ? " sel" : "";
    return `<button class="opt chip fx${sel}" data-act="personality" data-arg="${p.id}" data-group="personality">
      <div class="name">${esc(p.name.toUpperCase())}</div>
      <div class="desc">“${esc(p.sample)}”</div>
      <div class="play">▶ P to preview</div>
    </button>`;
  }).join("");
  const kokoroNote = (S.a.engine === "kokoro-realtime" || S.a.engine === "pocket-tts") && !S.probe.uv
    ? `<div class="readout warn">${ENGINES[S.a.engine].name} needs uv, a Python package manager</div>` : "";
  const voices = (S.probe.voices || {})[S.a.engine] || [];
  const ex = S.probe.existing;
  const yours = ex && ex.voice_custom && ex.engine === S.a.engine
    ? `<button class="opt chip fx${!S.a.voice ? " sel" : ""}" data-act="voice" data-arg="" data-group="voice">
        <div class="name">YOURS</div><div class="desc">${esc(ex.voice_custom)}. Your own voice, kept as it is.</div>
        <div class="play">${tag("CURRENT", "cy")}</div></button>` : "";
  const voicePicker = voices.length ? `<h2 class="sec">VOICE <span class="hint">click to hear a sample</span></h2>
    <div class="chips">${yours}${voices.map((v, i) => {
      const sel = S.a.voice === v.id || (!S.a.voice && !yours && i === 0) ? " sel" : "";
      return `<button class="opt chip fx${sel}" data-act="voice" data-arg="${v.id}" data-group="voice">
        <div class="name">${esc(v.name.toUpperCase())}</div>
        <div class="desc">${esc(v.accent)} ${esc(v.sex)}${i === 0 ? ", the default" : ""}</div>
        <div class="play">▶ sample</div></button>`;
    }).join("")}</div>` : "";
  const cbNote = S.a.engine === "chatterbox"
    ? `<div class="readout warn">Chatterbox needs a 10–30 second voice sample. Run <b>hobson setup chatterbox</b> when you are ready. The built-in voice speaks until then.</div>` : "";
  return {
    path: "01 :: VOICE",
    say: L("screen.voice"),
    body: `<h2 class="sec">ENGINE</h2>${cards}${kokoroNote}${cbNote}${voicePicker}
      <h2 class="sec">PERSONALITY</h2><div class="chips">${chips}</div>`,
    foot: footNav(),
  };
};

SCREENS.events = () => {
  const rows = EVENTS.map(([id, name, desc]) => {
    const on = S.a.events.includes(id);
    const needs = id === "commentary" && S.a.model === "none" && S.a.verbosity !== "chatty" ? tag("NEEDS A BRAIN", "warn") : "";
    return `<button class="opt row fx${on ? " sel" : ""}" data-act="event" data-arg="${id}">
      <span class="box">${on ? "[x]" : "[ ]"}</span>
      <span><div class="t">${name}</div><div class="d">${desc}</div></span>
      <span class="r">${needs}</span>
    </button>`;
  }).join("");
  let verb = "";
  if (S.a.events.includes("commentary")) {
    const seg = Object.keys(VERBOSITY).map((v) =>
      `<button class="fx${S.a.verbosity === v ? " sel" : ""}" data-act="verbosity" data-arg="${v}">${v.toUpperCase()}</button>`).join("");
    verb = `<h2 class="sec">COMMENTARY LEVEL</h2><div class="seg">${seg}</div>
      <p class="lede" style="margin-top:10px">${esc(VERBOSITY[S.a.verbosity])}</p>`;
  }
  return {
    path: "02 :: EVENTS",
    say: L("screen.events"),
    body: `<h2 class="sec">SPEAK WHEN <span class="hint">space to toggle</span></h2>${rows}${verb}`,
    foot: footNav({ disabled: !S.a.events.length, note: S.a.events.length ? "" : "Pick at least one." }),
  };
};

SCREENS.brain = () => {
  const o = S.probe.ollama;
  let status;
  if (!o.installed) {
    status = S.probe.brew
      ? `<div class="readout warn">Ollama is not installed. Homebrew installs and starts it at the end.</div>`
      : `<div class="readout bad">Ollama is not installed, and there is no Homebrew to install it. Get it from ollama.com, then run <b>hobson setup</b> again.</div>`;
  } else if (!o.running) {
    status = `<div class="readout warn">Ollama is installed but not running. Open the Ollama app, or run <b>ollama serve</b>.</div>`;
  } else {
    status = `<div class="readout ok">Ollama ${esc(o.version)} is running, with ${o.models.length} model${o.models.length === 1 ? "" : "s"} on this Mac.</div>`;
  }
  const free = S.probe.disk_free_gb;
  const rows = S.probe.models.map((m) => {
    const sel = S.a.model === m.id ? " sel" : "";
    const have = installed(m.id);
    const tooBig = !have && m.disk_gb > free - 2;
    const tags = [
      have ? tag("INSTALLED", "ok") : tag(`PULL ${fmtGB(m.disk_gb)}`, tooBig ? "bad" : "cy"),
      m.default ? tag("RECOMMENDED", "fill") : "",
      m.untested ? tag("UNTESTED", "warn") : "",
      // Only when this Mac is better off with another: a smaller disk or memory.
      !m.default && S.probe.recommend.model === m.id ? tag("FITS THIS MAC", "mg") : "",
    ].join(" ");
    return `<button class="opt row fx${sel}${tooBig ? " off" : ""}" data-act="model" data-arg="${m.id}" data-group="model"${tooBig ? " disabled" : ""}>
      <span class="box">${sel ? "(●)" : "( )"}</span>
      <span><div class="t">${esc(m.id.toUpperCase())} <span class="faint">· ${fmtGB(m.disk_gb)}</span></div><div class="d">${esc(m.note)}</div></span>
      <span class="r">${tags}</span>
    </button>`;
  }).join("");
  // With Ollama installed and running Hobson uses it whatever this says, so
  // "no brain" is only offered where it means "don't install one".
  // Any other model: the ones already pulled, and a name typed in.
  const local = o.models.filter((n) => !inCatalogue(n));
  const localRows = local.map((n) => `<button class="opt row fx${S.a.model === n ? " sel" : ""}" data-act="model" data-arg="${esc(n)}" data-group="model">
      <span class="box">${S.a.model === n ? "(●)" : "( )"}</span>
      <span><div class="t">${esc(n.toUpperCase())}</div><div class="d">Untested.</div></span>
      <span class="r">${tag("INSTALLED", "ok")} ${tag("UNTESTED", "warn")}</span>
    </button>`).join("");
  const custom = S.a.model && S.a.model !== "none" && !inCatalogue(S.a.model) && !local.includes(S.a.model) ? S.a.model : "";
  const other = `<div class="opt row${custom ? " sel" : ""}">
      <span class="box">${custom ? "(●)" : "( )"}</span>
      <span><div class="t">ANY OTHER OLLAMA MODEL</div>
        <div class="field" style="margin-top:8px"><span class="p">PULL&gt;</span>
          <input class="fx" id="custommodel" type="text" autocomplete="off" spellcheck="false" value="${esc(custom)}" placeholder="mistral:7b · qwen3:8b · hf.co/user/repo:Q4_K_M" data-enter="custommodel">
          <button class="btn fx" data-act="custommodel">USE</button></div>
        <div class="d" style="margin-top:6px">${S.modelMsg ? S.modelMsg : "Pulled at the end. Untested."}</div></span>
      <span class="r">${custom ? tag("UNTESTED", "warn") : ""}</span>
    </div>`;
  const none = o.installed && S.a.model !== "none" ? "" : `<button class="opt row fx${S.a.model === "none" ? " sel" : ""}" data-act="model" data-arg="none" data-group="model">
      <span class="box">${S.a.model === "none" ? "(●)" : "( )"}</span>
      <span><div class="t">NO BRAIN</div><div class="d">Fixed phrases only.</div></span>
      <span class="r">${tag("0 MB")}</span>
    </button>`;
  return {
    path: "03 :: BRAIN",
    say: L("screen.brain"),
    body: `${status}<h2 class="sec" style="margin-top:20px">MODEL <span class="hint">${free} GB free on disk · ${S.probe.ram_gb} GB memory</span></h2>${rows}${localRows}${other}${none}
      <p class="lede faint" style="margin-top:14px">The model reads how each turn ended and writes what Hobson says. It runs on this Mac, and nothing leaves it.</p>`,
    foot: footNav(),
  };
};

function jevBlock() {
  const a = S.a;
  const j = S.probe.jev;
  const opt = (id, name, desc, stats) =>
    `<button class="opt row fx${a.decider === id ? " sel" : ""}" data-act="decider" data-arg="${id}" data-group="decider">
      <span class="box">${a.decider === id ? "(●)" : "( )"}</span>
      <span><div class="t">${name}</div><div class="d">${desc}</div></span>
      <span class="r">${stats}</span>
    </button>`;
  let body = `<div class="rows">
    ${opt("local", "LOCAL", "Everything stays on this Mac. The brain and local rules decide.", "<span>NETWORK <b>NONE</b></span><span>COST <b>$0</b></span>")}
    ${opt("jev", "JEV", `A small remote model on OpenRouter. It filters chatter, repeats and risky commands.`, `<span>COST <b>~$${j.cost_per_call.toFixed(6)} / call</b></span><span>NEEDS <b>OPENROUTER KEY</b></span>`)}
  </div>`;
  if (a.decider === "jev") {
    body += `<div class="box-note" style="margin-top:14px"><b>WHAT LEAVES THIS MAC</b>
      <ul>
        <li>The phrase about to be spoken.</li>
        <li>A short, redacted summary of recent tool calls.</li>
        <li>Shell commands the local rules can't place, without paths, hosts, secrets or quoted text.</li>
      </ul>
      Never code, file contents or your conversation. The key is saved in ~/.claude/hobson.env, mode 600.</div>`;
    const readout = S.jev.state === "idle" ? "" :
      `<div class="readout ${S.jev.state === "ok" ? "ok" : S.jev.state === "bad" ? "bad" : "busy"}">${S.jev.msg}</div>`;
    if (j.key && !S.jev.other) {
      body += `<h2 class="sec" style="margin-top:18px">KEY</h2>
        <div class="field"><span class="p">KEY&gt;</span><span class="ok">FOUND IN ${j.key.source === "env" ? "YOUR ENVIRONMENT" : "~/.claude/hobson.env"}</span>
        <span class="faint">sk-or-…${esc(j.key.tail)}</span><span class="sp" style="flex:1"></span>
        <button class="btn ghost fx" data-act="jevother">USE ANOTHER</button>
        <button class="btn fx" data-act="jevtest">TEST KEY</button></div>${readout}${j.key.source === "env" ? `
        <button class="opt row fx${a.save_env_key !== false ? " sel" : ""}" data-act="saveenv" style="margin-top:10px">
          <span class="box">${a.save_env_key !== false ? "[x]" : "[ ]"}</span>
          <span><div class="t">ALSO SAVE IT TO ~/.claude/hobson.env</div><div class="d">Claude Code started from the desktop app or an IDE can't see your shell's environment.</div></span><span class="r"></span>
        </button>` : ""}`;
    } else {
      body += `<h2 class="sec" style="margin-top:18px">KEY <span class="hint">openrouter.ai/keys</span></h2>
        <div class="field"><span class="p">KEY&gt;</span>
        <input class="fx" id="jevkey" type="password" autocomplete="off" spellcheck="false" placeholder="sk-or-v1-…  paste, then ⏎" data-enter="jevtest">
        <button class="btn fx" data-act="jevtest">TEST KEY</button></div>${readout}
        <p class="lede faint" style="margin-top:10px">No key? Carry on and Hobson stays local. <b>hobson setup</b> can add one later.</p>`;
    }
  }
  return body;
}

SCREENS.jev = () => ({
  path: "04 :: DECIDER",
  say: L("screen.jev"),
  body: `<p class="lede">Three calls can get outside help. Is this worth saying, was it just said, is this command risky. Or they can all be made on this Mac.</p>${jevBlock()}`,
  foot: footNav({ disabled: !S.a.decider, note: S.a.decider ? jevNote() : "Pick one to continue." }),
});

function jevNote() {
  if (S.a.decider !== "jev") return "";
  if (S.a.jev_verified) return '<span class="ok">key verified</span>';
  return '<span class="warn">no verified key · staying local</span>';
}

SCREENS.presence = () => {
  const pr = S.probe.presence;
  const cards = Object.keys(PRESENCE).map((id) => {
    const m = PRESENCE[id];
    const needsCam = id === "auto" || id === "continuous";
    const noSensor = id !== "off" && !pr.built && !pr.swiftc;
    const dis = noSensor ? " off" : "";
    const on = S.a.presence === id;
    return `<button class="opt row fx${on ? " sel" : ""}${dis}" data-act="presence" data-arg="${id}" data-group="presence"${noSensor ? " disabled" : ""}>
      <span class="box">${on ? "(●)" : "( )"}</span>
      <span><div class="t">${m.name}</div><div class="d">${m.desc}</div></span>
      <span class="r">${id === "auto" ? tag("DEFAULT") : ""}<span>CAMERA <b>${needsCam ? "YES" : "NO"}</b></span></span>
    </button>`;
  }).join("");
  let cam = "";
  if (S.a.presence === "auto" || S.a.presence === "continuous") {
    const status = pr.camera || "unknown";
    const label = { authorized: "ALLOWED", "not-determined": "NOT ASKED YET", denied: "DENIED", restricted: "RESTRICTED" }[status] || "UNKNOWN";
    const cls = status === "authorized" ? "ok" : status === "not-determined" ? "warn" : "bad";
    const buttons = status === "authorized"
      ? `<button class="btn fx" data-act="look">LOOK NOW</button>`
      : status === "not-determined"
        ? `<button class="btn go fx" data-act="camera">ALLOW THE CAMERA</button>`
        : `<span class="faint">System Settings › Privacy & Security › Camera › Hobson Presence</span>`;
    cam = `<h2 class="sec">CAMERA</h2>
      <div class="field"><span class="p">CAM&gt;</span><span class="${cls}">${label}</span><span style="flex:1"></span>${buttons}</div>
      ${S.camera.msg ? `<div class="readout ${S.camera.cls || "busy"}">${S.camera.msg}</div>` : ""}
      <p class="lede faint" style="margin-top:10px">Camera frames never leave this Mac.</p>`;
  }
  const greet = S.a.presence === "off" ? "" : `<h2 class="sec">MANNERS</h2>
    <button class="opt row fx${S.a.greetings ? " sel" : ""}" data-act="greetings">
      <span class="box">${S.a.greetings ? "[x]" : "[ ]"}</span>
      <span><div class="t">GREETINGS</div><div class="d">A word when you leave, and a welcome when you return.</div></span><span class="r"></span>
    </button>`;
  const noSensor = !pr.built && !pr.swiftc
    ? `<div class="readout warn">The presence sensor needs the Xcode Command Line Tools (xcode-select --install). Signals only until then.</div>` : "";
  return {
    path: "05 :: PRESENCE",
    say: L("screen.presence"),
    body: `${noSensor}<h2 class="sec">MODE</h2><div class="rows">${cards}</div>${cam}${greet}`,
    foot: footNav(),
  };
};

SCREENS.phone = () => {
  const ph = S.probe.phone;
  const pres = S.a.presence;
  if (pres !== "auto" && pres !== "continuous") {
    return {
      path: "06 :: PHONE (EXPERIMENTAL)",
      say: L("screen.phone.nocamera"),
      body: `<div class="readout">Presence is ${esc(PRESENCE[pres].name.toLowerCase())}, so there is no camera to switch. Pick Auto or Continuous under Presence to use one.</div>`,
      foot: footNav(),
    };
  }
  const notes = `<div class="box-note" style="margin-top:16px"><b>HOW IT WORKS</b>
    <ul>
      <li>Android only, over adb. Turn on USB or Wireless debugging in Developer options.</li>
      <li>Face up, the camera may look. Face down, it stays off.</li>
      <li>If the phone can't be read, the camera stays off.</li>
      <li>For Wireless debugging, start adb from a terminal first, or you'll see "No route to host".</li>
    </ul></div>`;
  if (!ph.adb) {
    return {
      path: "06 :: PHONE (EXPERIMENTAL)",
      say: L("screen.phone"),
      body: `<div class="readout warn">adb not found. Run brew install android-platform-tools, then rescan.</div>${notes}`,
      foot: `<button class="btn ghost fx" data-act="back"><span class="k">ESC</span> BACK</button><button class="btn fx" data-act="rescan">RESCAN</button><span class="sp"></span><button class="btn go fx primary" data-act="next">SKIP <span class="k">⏎</span></button>`,
    };
  }
  const devs = ph.devices.map((d) =>
    `<button class="opt row fx${S.a.phone === d.serial ? " sel" : ""}" data-act="phone" data-arg="${esc(d.serial)}" data-group="phone">
      <span class="box">${S.a.phone === d.serial ? "(●)" : "( )"}</span>
      <span><div class="t">${esc((d.model || "ANDROID").toUpperCase())}</div><div class="d">${esc(d.serial)} · ${esc(d.transport || "usb")}</div></span>
      <span class="r">${tag("CONNECTED", "ok")}</span>
    </button>`).join("");
  const off = `<button class="opt row fx${!S.a.phone ? " sel" : ""}" data-act="phone" data-arg="" data-group="phone">
      <span class="box">${!S.a.phone ? "(●)" : "( )"}</span>
      <span><div class="t">NO SWITCH</div><div class="d">The camera follows the presence mode alone.</div></span><span class="r"></span>
    </button>`;
  const none = !ph.devices.length ? `<div class="readout warn">No phone found. Plug it in or pair Wireless debugging, accept the prompt on the phone, then rescan.</div>` : "";
  let stage = "";
  if (S.a.phone) {
    const t = S.phone.test;
    const pos = S.phone.pos;
    const step = (i, text, line) => {
      const st = !t ? "" : t.done[i] ? "ok" : t.step === i ? (t.timeout ? "" : "now") : "";
      const s = !t ? "" : t.done[i] ? `<span class="s ok">"${line}"</span>` : t.step === i ? (t.timeout ? '<span class="s bad">TIMED OUT</span>' : '<span class="s cy">WAITING…</span>') : "";
      return `<div class="step ${st}"><span class="i">${t && t.done[i] ? "✓" : i + 1}</span><span>${text}</span>${s}</div>`;
    };
    const passed = t && t.done[0] && t.done[1];
    stage = `<h2 class="sec">FLIP TEST</h2>
      <div class="phone-stage">
        <div class="steps">
          ${step(0, "Lay the phone face down", "Camera off.")}
          ${step(1, "Now turn it face up", "Camera on.")}
          <div style="display:flex;gap:10px;margin-top:6px">
            <button class="btn ${passed ? "ghost" : "go"} fx" data-act="fliptest"${t && !t.finished ? " disabled" : ""}>${passed ? "RUN AGAIN" : t && t.finished ? "TRY AGAIN" : t ? "TESTING…" : "START FLIP TEST"}</button>
            <span class="faint" style="align-self:center">READING <span class="${pos === "up" || pos === "down" ? "ok" : "warn"}">${esc(pos.toUpperCase())}</span></span>
          </div>
        </div>
      </div>`;
  }
  return {
    path: "06 :: PHONE (EXPERIMENTAL)",
    say: L("screen.phone"),
    body: `${none}<h2 class="sec">SWITCH</h2>${devs}${off}${stage}${notes}`,
    foot: `<button class="btn ghost fx" data-act="back"><span class="k">ESC</span> BACK</button><button class="btn ghost fx" data-act="rescan">RESCAN</button><span class="sp"></span><button class="btn go fx primary" data-act="next">NEXT <span class="k">⏎</span></button>`,
  };
};

SCREENS.loadout = () => {
  const a = S.a;
  const why = S.probe.recommend.why || {};
  const e = ENGINES[a.engine];
  const eng = S.probe.engines.find((x) => x.id === a.engine) || {};
  const m = a.model === "none" ? null : model(a.model);
  const row = (k, v, small, x) => `<div class="k">${k}</div><div class="v">${v}<small>${esc(small || "")}</small></div><div class="x">${x || ""}</div>`;
  const rows = [
    row("VOICE", `${e.name}${voiceName() ? " · " + esc(voiceName()) : ""} · ${esc(persona(a.personality).name)}`, why.engine, eng.ready ? tag("INSTALLED", "ok") : tag(`DOWNLOADS ${e.size}`, "warn")),
    row("EVENTS", a.events.map((x) => x.toUpperCase()).join(" · "), why.events),
    row("BRAIN", a.model === "none" ? "NONE" : esc(a.model.toUpperCase()), why.model,
        a.model === "none" ? "" : installed(a.model) ? tag("INSTALLED", "ok") : tag(m ? `PULL ${fmtGB(m.disk_gb)}` : "PULL", "cy")),
    row("PRESENCE", PRESENCE[a.presence].name, why.presence),
    row("PHONE", a.phone ? "ON" : "OFF", why.phone),
  ].join("");
  return {
    path: "EXPRESS :: LOADOUT",
    say: L("screen.loadout"),
    body: `<div class="loadout">${rows}</div><h2 class="sec" style="margin-top:26px">DECIDER</h2>${jevBlock()}`,
    foot: `<button class="btn ghost fx" data-act="back"><span class="k">ESC</span> BACK</button><button class="btn ghost fx" data-act="customize">CUSTOMIZE</button><span class="sp"></span><span class="note">${a.decider ? jevNote() : "Pick Local or Jev to continue."}</span><button class="btn go fx primary" data-act="next"${a.decider ? "" : " disabled"}>REVIEW <span class="k">⏎</span></button>`,
  };
};

function fmtValue(v) {
  if (Array.isArray(v)) return `[${v.map((x) => `"${esc(x)}"`).join(", ")}]`;
  if (typeof v === "string") return `"${esc(v)}"`;
  return esc(JSON.stringify(v));
}

function taskRow(t) {
  const r = (S.run && S.run.tasks[t.id]) || { state: "wait" };
  const label = { wait: "PENDING", run: "RUNNING", ok: "  OK  ", fail: "FAILED", skip: "SKIPPED" }[r.state];
  const spin = r.state === "run" ? "|/-\\"[Math.floor(Date.now() / 120) % 4] + " " : "";
  const detail = r.detail || t.detail || "";
  return `<div class="task ${r.state}" id="task-${t.id}"><span class="s">[${spin}${label}]</span>
    <span class="lbl">${esc(t.label)}<small>${esc(detail)}</small></span>
    <span>${r.progress != null && r.state === "run" ? bar(r.progress, 22) : ""}</span></div>`;
}

SCREENS.commit = () => {
  const p = S.plan;
  if (!p) return { path: "07 :: COMMIT", say: L("screen.commit.loading"), body: `<div class="readout busy">Working out the changes…</div>`, foot: "" };
  const writes = p.writes.length
    ? p.writes.map((w) => (w.note ? `<div class="n"><span class="kk">${esc(w.key)}</span>${esc(w.value)}</div>` : `<div class="a"><span class="kk">${esc(w.key)}</span>${fmtValue(w.value)}</div>`)).join("")
    : `<div class="n">nothing to write, every choice is the default</div>`;
  const secrets = (p.secrets || []).map((s) => `<div class="s">${esc(s)}</div>`).join("");
  const running = S.run && !S.run.done;
  const failed = S.run && S.run.done && !S.run.ok;
  return {
    path: "07 :: COMMIT",
    say: L(running ? "screen.commit.running" : "screen.commit"),
    body: `<h2 class="sec">WRITES <span class="hint">~/.claude/hobson.json</span></h2>
      <div class="diff">${writes}${secrets}<div class="n">${p.defaults_kept} settings unchanged</div></div>
      <h2 class="sec">TASKS</h2><div class="tasks">${p.tasks.map(taskRow).join("")}</div>
      ${failed ? `<div class="readout bad">A task failed. Run <b>hobson doctor</b> to troubleshoot, or <b>hobson setup</b> to try again.</div>` : ""}`,
    foot: running
      ? `<span class="sp"></span><span class="note">please wait</span><button class="btn go primary" disabled>INSTALLING…</button>`
      : S.run && S.run.done
        ? `<span class="sp"></span><button class="btn go fx primary" data-act="finish">CONTINUE <span class="k">⏎</span></button>`
        : `<button class="btn ghost fx" data-act="back"><span class="k">ESC</span> BACK</button><span class="sp"></span><button class="btn go fx primary" data-act="execute">INSTALL <span class="k">⏎</span></button>`,
  };
};

// ── Painting ────────────────────────────────────────────────────────────

let focused = null;

function focusables() {
  return $$("#main .fx").filter((el) => !el.disabled && el.offsetParent !== null);
}

function focusKey(el) {
  if (!el) return null;
  return el.id ? "#" + el.id : `${el.dataset.act || ""}|${el.dataset.arg || ""}`;
}

function findByKey(key) {
  if (!key) return null;
  if (key[0] === "#") return document.getElementById(key.slice(1));
  return focusables().find((el) => focusKey(el) === key) || null;
}

// The keyboard's cursor. Nothing has it until a key is pressed, and the
// pointer never moves it: hover only brightens (.hover), and moving the
// mouse puts the cursor away.
function setFocus(el, sound) {
  if (focused) focused.classList.remove("focus");
  focused = el;
  if (!el) return;
  setHover(null);
  anchor = pointer.slice();
  el.classList.add("focus");
  if (el.tagName === "INPUT") el.focus();
  else if (document.activeElement && document.activeElement.tagName === "INPUT") document.activeElement.blur();
  el.scrollIntoView({ block: "nearest" });
  if (sound) Sfx.move();
}

let hovered = null;
// Where the pointer was when the keyboard last moved the cursor: a nudge of
// the mouse on the desk is not a decision to use it.
let pointer = [0, 0], anchor = [0, 0];
function setHover(el) {
  if (hovered) hovered.classList.remove("hover");
  hovered = el;
  if (el) el.classList.add("hover");
}

function spatial(dir) {
  const list = focusables();
  if (!list.length) return;
  if (!focused || !list.includes(focused)) return setFocus(list[0], true);
  const r0 = focused.getBoundingClientRect();
  const x0 = r0.left + r0.width / 2, y0 = r0.top + r0.height / 2;
  let best = null, bestScore = Infinity;
  for (const el of list) {
    if (el === focused) continue;
    const r = el.getBoundingClientRect();
    const dx = r.left + r.width / 2 - x0, dy = r.top + r.height / 2 - y0;
    let main, cross;
    if (dir === "left") { if (dx > -4) continue; main = -dx; cross = Math.abs(dy); }
    else if (dir === "right") { if (dx < 4) continue; main = dx; cross = Math.abs(dy); }
    else if (dir === "up") { if (dy > -4) continue; main = -dy; cross = Math.abs(dx); }
    else { if (dy < 4) continue; main = dy; cross = Math.abs(dx); }
    const score = main + cross * 2.4;
    if (score < bestScore) { bestScore = score; best = el; }
  }
  if (best) setFocus(best, true);
}

function linear(step) {
  const list = focusables();
  if (!list.length) return;
  const i = list.indexOf(focused);
  setFocus(list[(i + step + list.length) % list.length], true);
}

// After a choice made from the keyboard, move on to the next group of
// choices, or to the primary button after the last one.
function advanceFrom(group) {
  const list = focusables();
  let last = -1;
  list.forEach((el, i) => { if (el.dataset.group === group) last = i; });
  const next = list.slice(last + 1).find((el) => el.dataset.group !== group && !el.closest(".pane-f")) || $("#main .primary:not([disabled])");
  if (next) setFocus(next, false);
}

function typeInto(el, text) {
  const token = ++S.typing;
  el.textContent = "";
  let i = 0;
  (function step() {
    if (token !== S.typing) return;
    el.textContent = text.slice(0, i);
    if (i % 3 === 0 && i > 0) Sfx.tick();
    if (i++ < text.length) setTimeout(step, 16);
  })();
}

function paint(full) {
  const scr = SCREENS[S.screen]();
  const main = $("#main");
  const key = focusKey(focused);
  const hoverKey = focusKey(hovered);
  hovered = null;
  if (full || !$(".pane", main)) {
    // "03 :: BRAIN" reads as BRAIN, 3 of 7; "EXPRESS :: LOADOUT" as LOADOUT, express.
    const [where, name] = scr.path.split(" :: ");
    const mod = MODULES.find((m) => m.id === S.screen);
    const of = /^\d+$/.test(where) ? `${+where} of ${MODULES.length}${mod ? " · " + mod.region : ""}` : where.toLowerCase();
    // The band: the module's picture, with its title set over it.
    main.innerHTML = `<section class="pane glitch-in">
      <header class="pane-h${Art.has(S.screen) ? " band" : ""}"><div class="band-slot"></div><div class="of">${of}</div><h1 class="title">${name}</h1></header>
      <div class="pane-b"><div class="say"><span class="who">HOBSON&gt;</span><span class="txt"></span><span class="cur"></span></div><div class="body"></div></div>
      <div class="pane-f"></div></section>`;
    typeInto($(".say .txt", main), scr.say);
    Voice.say(scr.say);
    focused = null;
    Art.mount($(".band-slot", main), S.screen);
  }
  const scroll = $(".pane-b", main).scrollTop;
  $(".body", main).innerHTML = scr.body;
  $(".pane-f", main).innerHTML = scr.foot;
  $(".pane-b", main).scrollTop = scroll;
  const again = full ? null : findByKey(key);
  if (again) setFocus(again, false);
  else if (!full) setHover(findByKey(hoverKey));
  paintRail();
}

// Retype Hobson's line when the same screen now says something else.
function resay() {
  const el = $("#main .say .txt");
  const text = SCREENS[S.screen]().say;
  if (el && el.textContent !== text) { typeInto(el, text); Voice.say(text); }
}

async function go(screen) {
  const first = !S.visited.has(screen) && S.flow !== "update";
  const pane = $("#main .pane");
  if (pane && !(first && Art.has(screen))) {
    pane.classList.add("glitch-out");
    await sleep(120);
  }
  if (first && Art.has(screen)) {
    await Art.cutscene(screen, MODULES.find((x) => x.id === screen).title);
  }
  S.screen = screen;
  if (MODULES.some((m) => m.id === screen)) S.visited.add(screen);
  tint(screen);
  if (screen === "phone") { showPhone(); if (S.a.phone) pollPhoneOnce(); }
  if (screen === "commit") {
    S.plan = null;
    S.run = null;
    Art.progress = 0;
    paint(true);
    try {
      S.plan = await api.post("plan", { answers: S.a });
    } catch (e) {
      S.plan = { writes: [], defaults_kept: 0, tasks: [], secrets: [] };
      log(`plan failed: ${e.message}`);
    }
    Art.tasks = S.plan.tasks.length;
    paint(false);
    resay();
    return;
  }
  paint(true);
}

// ── Actions ─────────────────────────────────────────────────────────────

const ACT = {
  mode(arg) {
    Sfx.pick();
    S.flow = arg;
    document.body.classList.remove("booting");
    if (arg === "express") S.route = ["loadout", "commit"];
    else if (arg === "update") S.route = MODULES.map((m) => m.id).filter((id) => !S.probe.seen.includes(id) || id === "commit");
    else S.route = MODULES.map((m) => m.id);
    log(`${arg} mode`);
    go(S.route[0]);
  },
  customize() {
    Sfx.pick();
    S.flow = "custom";
    S.route = MODULES.map((m) => m.id);
    go("voice");
  },
  next() {
    const i = S.route.indexOf(S.screen);
    if (i < 0 || i + 1 >= S.route.length) return;
    Sfx.pick();
    go(S.route[i + 1]);
  },
  back() {
    if (S.run && !S.run.done) return;
    Sfx.back();
    const i = S.route.indexOf(S.screen);
    if (i > 0) go(S.route[i - 1]);
    else if (S.screen !== "boot" && S.screen !== "final") { S.flow = null; bootHero(true); }
  },
  engine(arg) {
    const ex = S.probe.existing;
    const voices = (S.probe.voices || {})[arg] || [];
    // Back on the engine you have: your voice. A new one: its default.
    S.a.voice = ex && ex.engine === arg ? ex.voice : voices.length ? voices[0].id : null;
    S.a.engine = arg;
    Sfx.pick();
    log(`engine ${arg}`);
    paint(false);
    if (arg !== "chatterbox") ACT.audition(S.a.personality);
    return "engine";
  },
  personality(arg) {
    S.a.personality = arg;
    Sfx.pick();
    paint(false);
    ACT.audition(arg);
    return "personality";
  },
  voice(arg) {
    S.a.voice = arg || null;
    Sfx.pick();
    paint(false);
    const v = currentVoice();
    Voice.audition(L("audition.voice"), S.a.engine, v, S.a.personality);
    return "voice";
  },
  audition(arg) {
    const p = persona(arg || S.a.personality);
    const chip = findByKey(`personality|${p.id}`);
    if (chip) chip.classList.add("speaking");
    log(`preview ${p.id}`);
    Voice.audition(p.sample, S.a.engine, currentVoice(), p.id).then(() => setTimeout(() => { const c = findByKey(`personality|${p.id}`); if (c) c.classList.remove("speaking"); }, 400));
  },
  event(arg) {
    const on = S.a.events.includes(arg);
    S.a.events = on ? S.a.events.filter((x) => x !== arg) : S.a.events.concat(arg);
    Sfx.pick();
    paint(false);
  },
  verbosity(arg) { S.a.verbosity = arg; Sfx.pick(); paint(false); },
  model(arg) { S.a.model = arg; S.modelMsg = ""; Sfx.pick(); log(`brain ${arg}`); paint(false); return "model"; },
  custommodel() {
    const input = $("#custommodel");
    const name = input ? input.value.trim() : "";
    if (!MODEL_NAME.test(name)) {
      Sfx.err();
      S.modelMsg = '<span class="bad">Not an Ollama model name. Try something like mistral:7b.</span>';
      paint(false);
      return;
    }
    S.a.model = name;
    S.modelMsg = "";
    Sfx.pick();
    log(`brain ${name}`);
    paint(false);
  },
  decider(arg) {
    S.a.decider = arg;
    Sfx.pick();
    log(`decider ${arg}`);
    paint(false);
    // Picking Jev with a key already on this Mac is saying "use it": test it now.
    if (arg === "jev" && S.probe.jev.key && !S.jev.other && S.jev.state === "idle") ACT.jevtest();
    if (arg === "jev" && (!S.probe.jev.key || S.jev.other)) { const k = $("#jevkey"); if (k) setFocus(k, false); return null; }
    return "decider";
  },
  saveenv() { S.a.save_env_key = S.a.save_env_key === false; Sfx.pick(); paint(false); },
  jevother() {
    S.jev = { state: "idle", msg: "", other: true };
    S.a.jev_verified = false;
    paint(false);
    const k = $("#jevkey");
    if (k) setFocus(k, false);
  },
  async jevtest() {
    const input = $("#jevkey");
    const typed = input ? input.value.trim() : "";
    if (input && !typed) { Sfx.err(); S.jev = { ...S.jev, state: "bad", msg: "Paste a key first, or pick Local." }; paint(false); return; }
    S.jev = { ...S.jev, state: "busy", msg: "Testing the key with one call to " + esc(S.probe.jev.model) + "…" };
    paint(false);
    let r;
    try { r = await api.post("jev/test", typed ? { key: typed } : {}); } catch (e) { r = { ok: false, reason: e.message }; }
    if (input) input.value = ""; // the server holds a verified key; the page keeps none
    if (r.ok) {
      Sfx.ok();
      S.a.jev_verified = true;
      S.a.key_typed = !!typed;
      S.jev = { ...S.jev, state: "ok", msg: `Key works. ${r.ms} ms, $${Number(r.cost).toFixed(6)} for that call.` };
      log("jev key works");
    } else {
      Sfx.err();
      S.a.jev_verified = false;
      S.jev = { ...S.jev, state: "bad", msg: `Key refused (${esc(r.reason)}). Hobson stays local unless another key works.` };
      log("jev key refused");
    }
    paint(false);
    const out = $("#main .readout.ok, #main .readout.bad");
    if (out) out.scrollIntoView({ block: "nearest", behavior: "smooth" });
  },
  presence(arg) { S.a.presence = arg; S.camera.msg = ""; Sfx.pick(); log(`presence ${arg}`); paint(false); return "presence"; },
  greetings() { S.a.greetings = !S.a.greetings; Sfx.pick(); paint(false); },
  async camera() {
    S.camera = { busy: true, msg: "Answer the macOS dialog from Hobson Presence.", cls: "busy" };
    paint(false);
    const r = await api.post("presence/look", { request_permission: true }).catch(() => ({}));
    S.probe.presence.camera = r.camera || S.probe.presence.camera;
    if (r.camera === "authorized") {
      Sfx.ok();
      S.camera = { msg: lookMsg(r), cls: r.people ? "ok" : "warn" };
    } else {
      Sfx.err();
      S.camera = { msg: "No camera access.", cls: "bad" };
      S.a.presence = "signals";
    }
    paint(false);
  },
  async look() {
    S.camera = { busy: true, msg: "Looking for two seconds…", cls: "busy" };
    paint(false);
    const r = await api.post("presence/look", {}).catch(() => ({}));
    const cls = r.people ? "ok" : "warn";
    S.camera = { msg: lookMsg(r), cls };
    if (r.people) { Sfx.ok(); Voice.say(L("look.seen")); } else Sfx.err();
    paint(false);
  },
  phone(arg) {
    S.a.phone = arg || null;
    S.phone.test = null;
    showPhone();
    Sfx.pick();
    paint(false);
    if (arg) pollPhoneOnce();
    return "phone";
  },
  async rescan() {
    Sfx.pick();
    log("rescan adb");
    try { S.probe.phone = await api.get("phone/scan"); } catch (e) { log(`rescan failed: ${e.message}`); }
    paint(false);
  },
  async fliptest() {
    Sfx.pick();
    // Choosing another phone, or none, replaces S.phone.test (it used to be
    // written through as null, and threw); leaving the screen abandons it.
    // Either way this run stops, quietly.
    const test = { step: 0, done: [false, false], finished: false, timeout: false };
    S.phone.test = test;
    paint(false);
    await api.post("phone/test", { serial: S.a.phone }).catch(() => {});
    const over = () => S.phone.test !== test || S.screen !== "phone";
    const want = ["down", "up"];
    const lines = [L("flip.down"), L("flip.up")];
    for (let i = 0; i < 2; i++) {
      if (over()) { test.finished = true; return; }
      test.step = i;
      paint(false);
      const deadline = Date.now() + 30000;
      let hit = false;
      while (Date.now() < deadline && S.screen === "phone" && S.a.phone) {
        await pollPhoneOnce();
        if (S.phone.pos === want[i]) { hit = true; break; }
        await sleep(900);
      }
      if (over()) { test.finished = true; return; }
      if (!hit) { test.timeout = true; test.finished = true; Sfx.err(); paint(false); return; }
      test.done[i] = true;
      Sfx.ok();
      Voice.say(lines[i]);
      paint(false);
    }
    test.finished = true;
    log("flip test passed");
    paint(false);
  },
  async execute() {
    Sfx.pick();
    S.run = { tasks: {}, done: false, ok: true };
    City.mode("warp");
    paint(false);
    resay();
    const spinner = setInterval(() => { if (S.screen === "commit") repaintTasks(); }, 120);
    try {
      await api.stream("apply", { answers: S.a }, (ev) => {
        if (ev.task) {
          S.run.tasks[ev.task] = { ...(S.run.tasks[ev.task] || {}), ...ev };
          if (ev.state === "fail") S.run.ok = false;
          if (ev.state === "ok") { Sfx.tick(); log(`${ev.task} ok`); }
          repaintTasks();
        }
        if (ev.done) { S.run.done = true; S.run.ok = S.run.ok && ev.ok !== false; }
      });
    } catch (e) {
      S.run.ok = false;
      log(`apply failed: ${e.message}`);
    }
    clearInterval(spinner);
    S.run.done = true;
    City.mode("cruise");
    paint(false);
    if (S.run.ok) { await sleep(700); ACT.finish(); }
    else Sfx.err();
  },
  finish() { finale(); },
  async close() {
    Sfx.back();
    await api.post("done", {}).catch(() => {});
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.hobson) {
      window.webkit.messageHandlers.hobson.postMessage({ close: true });
    } else {
      window.close();
      $("#main").innerHTML = `<div class="final"><div><div class="sub">You can close this tab.</div></div></div>`;
    }
  },
};

// The voice the engine will use: the one picked, else its default. None for
// a voice you made yourself: there is no recording of it to play.
function currentVoice() {
  const voices = (S.probe.voices || {})[S.a.engine] || [];
  if (S.a.voice) return S.a.voice;
  const ex = S.probe.existing;
  if (ex && ex.voice_custom && ex.engine === S.a.engine) return null;
  return voices.length ? voices[0].id : null;
}

function voiceName() {
  const id = currentVoice();
  const v = ((S.probe.voices || {})[S.a.engine] || []).find((x) => x.id === id);
  if (v) return v.name;
  const ex = S.probe.existing;
  return ex && ex.voice_custom && ex.engine === S.a.engine ? "yours" : "";
}

function lookMsg(r) {
  if (!r || r.camera !== "authorized") return "The camera didn't answer.";
  if (r.people == null) return "The camera saw nothing. The room is dark or the lens is covered.";
  if (r.people === 0) return "Nobody in frame. Sit where the camera can see your face.";
  return `${r.people} face${r.people > 1 ? "s" : ""} in frame.`;
}

// The band on PHONE shows the phone as the switch reads it, once one is picked.
function showPhone() {
  Art.flip = S.a.phone ? (S.phone.pos === "up" || S.phone.pos === "down" ? S.phone.pos : "unknown") : null;
}

async function pollPhoneOnce() {
  const before = S.phone.pos;
  try {
    const r = await api.get("phone?serial=" + encodeURIComponent(S.a.phone || ""));
    S.phone.pos = r.phone || "unknown";
  } catch (_) {
    S.phone.pos = "unreachable";
  }
  showPhone();
  if (S.screen === "phone" && S.phone.pos !== before) paint(false);
}

function repaintTasks() {
  if (!S.plan) return;
  const n = S.plan.tasks.length || 1;
  Art.tasks = S.plan.tasks.length;
  Art.progress = S.plan.tasks.filter((t) => ["ok", "fail", "skip"].includes(((S.run && S.run.tasks[t.id]) || {}).state)).length / n;
  for (const t of S.plan.tasks) {
    const el = document.getElementById(`task-${t.id}`);
    if (el) el.outerHTML = taskRow(t);
  }
}

// ── Boot, hero, finale ──────────────────────────────────────────────────

function bootLines(p) {
  const L = [];
  const add = (k, v, cls) => L.push([k, v, cls]);
  add("HOST", String(p.host || "").toUpperCase(), "ok");
  add("MEMORY", `${p.ram_gb} GB`, "ok");
  add("CPU", `${p.arch.toUpperCase()} · macOS ${p.macos}`, "ok");
  add("DISK FREE", `${p.disk_free_gb} GB`, p.disk_free_gb < 12 ? "warn" : "ok");
  add("PYTHON", p.python, "ok");
  add("UV", p.uv ? "FOUND" : "MISSING", p.uv ? "ok" : "warn");
  const o = p.ollama;
  add("OLLAMA", !o.installed ? "NOT INSTALLED" : !o.running ? "NOT RUNNING" : `ONLINE · ${o.models.length} MODELS`, !o.installed ? "warn" : !o.running ? "warn" : "ok");
  add("OPENROUTER KEY", p.jev.key ? `FOUND (${p.jev.key.source.toUpperCase()})` : "NONE", p.jev.key ? "ok" : "dim");
  const cam = p.presence.camera;
  add("CAMERA", !p.presence.built && !p.presence.swiftc ? "NO SENSOR" : ({ authorized: "ALLOWED", "not-determined": "NOT ASKED", denied: "DENIED" }[cam] || "UNKNOWN"), cam === "authorized" ? "ok" : "warn");
  add("ADB", !p.phone.adb ? "NOT INSTALLED" : `${p.phone.devices.length} DEVICE${p.phone.devices.length === 1 ? "" : "S"}`, p.phone.adb && p.phone.devices.length ? "ok" : "dim");
  add("CLAUDE CODE HOOKS", p.hooks === "current" ? "CURRENT" : p.hooks === "stale" ? "OUT OF DATE" : "NOT INSTALLED", p.hooks === "current" ? "ok" : "warn");
  add("SETTINGS", p.existing ? "FOUND · KEPT" : "NEW INSTALL", "ok");
  return L;
}

async function bootLine(k, v, cls) {
  const el = document.createElement("div");
  el.className = "l";
  $("#bootlog").appendChild(el);
  for (let i = 1; i <= k.length; i++) {
    el.innerHTML = `<span class="k">${esc(k.slice(0, i))}</span>`;
    if (i % 2) Sfx.tick();
    await sleep(9);
  }
  // More dots than any row needs: the row is a flex line, and the leader is cut to fit.
  el.innerHTML = `<span class="k">${esc(k)}</span><span class="faint">${".".repeat(80)}</span><span class="${cls}">${esc(v)}</span>`;
}

function partOfDay() {
  const h = new Date().getHours();
  return h < 5 ? "night" : h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
}

async function boot() {
  document.body.classList.add("booting");
  City.mode("warp");
  $("#main").innerHTML = `<div class="boot"><div class="bootlog"><div class="head">SURVEYING THIS MAC</div><div id="bootlog"></div></div><div class="hero" id="hero"></div></div>`;
  paintRail();
  const probing = api.get("probe");
  await bootLine("SETUP SERVER", "127.0.0.1", "cy");
  try {
    S.probe = await probing;
  } catch (e) {
    await bootLine("PROBE", "FAILED", "bad");
    $("#hero").innerHTML = `<div class="readout bad">The setup server didn't answer (${esc(e.message)}). Close this window and run <b>hobson setup</b> again.</div>`;
    $("#hero").classList.add("in");
    return;
  }
  initAnswers();
  paintTop();
  for (const [k, v, cls] of bootLines(S.probe)) { await bootLine(k, v, cls); await sleep(60); }
  log(`probe ok · ${S.probe.ollama.models.length} models · camera ${S.probe.presence.camera || "n/a"}`);
  City.mode("cruise");
  bootHero(false);
}

function bootHero(returning) {
  S.screen = "boot";
  Art.mount(null, "boot");
  tint("boot");
  document.body.classList.add("booting");
  if (returning) {
    $("#main").innerHTML = `<div class="boot"><div class="bootlog"><div class="head">SURVEYING THIS MAC</div><div id="bootlog"></div></div><div class="hero" id="hero"></div></div>`;
    for (const [k, v, cls] of bootLines(S.probe)) {
      const dots = ".".repeat(Math.max(2, 46 - k.length - v.length - 2));
      $("#bootlog").insertAdjacentHTML("beforeend", `<div class="l"><span class="k">${esc(k)}</span> <span class="faint">${dots}</span> <span class="${cls}">${esc(v)}</span></div>`);
    }
  }
  const p = S.probe;
  const unseen = MODULES.filter((m) => m.id !== "commit" && !p.seen.includes(m.id));
  const update = p.existing && unseen.length && unseen.length < MODULES.length - 1;
  const modes = update
    ? `<button class="opt fx primary" data-act="mode" data-arg="update"><div class="name">UPDATE</div><div class="desc">New since your last setup, ${unseen.map((m) => m.title).join(", ")}. Everything else is kept.</div></button>
       <button class="opt fx" data-act="mode" data-arg="custom"><div class="name">RECONFIGURE</div><div class="desc">Go through all six modules again, from your current settings.</div></button>`
    : `<button class="opt fx primary" data-act="mode" data-arg="express"><div class="name">EXPRESS</div><div class="desc">Hobson picks what suits this Mac</div></button>
       <button class="opt fx" data-act="mode" data-arg="custom"><div class="name">CUSTOM</div><div class="desc">You pick what is best for Hobson</div></button>`;
  $("#hero").innerHTML = `
    <div class="logo"><pre class="c">${LOGO}</pre><pre class="m">${LOGO}</pre><pre class="main">${LOGO}</pre></div>
    <div class="tagline">A synthetic butler for <b>Claude Code</b>. I speak when needed, and otherwise keep a discreet silence.</div>
    <div class="say" style="margin:0"><span class="who">HOBSON&gt;</span><span class="txt"></span><span class="cur"></span></div>
    <div class="modes">${modes}</div>`;
  requestAnimationFrame(() => $("#hero").classList.add("in"));
  const line = L(`hero.${update ? "update" : "setup"}.${partOfDay()}`);
  typeInto($("#hero .say .txt"), line);
  if (!returning) Voice.say(line);
  paintRail();
}

async function finale() {
  S.screen = "final";
  tint("final");
  Art.mount(null, "final");
  const a = S.a;
  const failed = S.run && !S.run.ok;
  document.body.classList.add("booting");
  City.burst();
  const row = (k, v) => `<div class="k">${k}</div><div>${esc(v)}</div>`;
  const decider = a.decider === "jev" && a.jev_verified ? "JEV" : "LOCAL";
  $("#main").innerHTML = `<div class="final"><div>
    <div class="big">${failed ? "ONLINE*" : "ONLINE"}</div>
    <div class="sub">${failed ? "Installed, but a task failed. Run hobson doctor to see which." : "Hobson is installed. Start a new Claude Code session to hear him."}</div>
    <div class="sum">
      ${row("VOICE", `${ENGINES[a.engine].name}${voiceName() ? " · " + voiceName() : ""} · ${persona(a.personality).name}`)}
      ${row("EVENTS", a.events.join(" · "))}
      ${row("BRAIN", a.model === "none" ? "none" : a.model)}
      ${row("DECIDER", decider)}
      ${row("PRESENCE", a.presence)}
      ${row("PHONE", a.phone ? "switch on" : "off")}
    </div>
    <div style="margin-top:30px"><button class="btn go fx primary" data-act="close">CLOSE <span class="k">⏎</span></button></div>
    <div class="faint" style="margin-top:18px;font-size:11px;letter-spacing:.12em">hobson status · hobson doctor · hobson setup</div>
  </div></div>`;
  paintRail();
}

// ── Input ───────────────────────────────────────────────────────────────

let hack = "";
function listenForHack(key) {
  if (key.length !== 1) return;
  hack = (hack + key.toLowerCase()).slice(-15);
  if (hack === "hack the planet") {
    hack = "";
    City.burst();
    toast("HACK THE PLANET");
    Sfx.ok();
  }
}

document.addEventListener("click", (e) => {
  const rail = e.target.closest("[data-rail]");
  if (rail && S.probe && S.screen !== "boot" && S.screen !== "final" && !(S.run && !S.run.done)) {
    const id = rail.dataset.rail;
    if (S.flow !== "custom") { S.flow = "custom"; S.route = MODULES.map((m) => m.id); }
    document.body.classList.remove("booting");
    Sfx.pick();
    go(id);
    return;
  }
  const el = e.target.closest("[data-act]");
  if (!el || el.disabled) return;
  const fn = ACT[el.dataset.act];
  if (!fn) return;
  const group = fn(el.dataset.arg, el);
  if (e.detail === 0 && typeof group === "string") advanceFrom(group); // keyboard, not mouse
});

// mousemove, not mouseover: a new screen appearing under a resting pointer
// is not a hover.
document.addEventListener("mousemove", (e) => {
  pointer = [e.clientX, e.clientY];
  const el = e.target.closest("#main .fx");
  if (focused && Math.hypot(pointer[0] - anchor[0], pointer[1] - anchor[1]) < 8) return;
  if (focused && focused.tagName !== "INPUT") setFocus(null);
  setHover(el && !el.disabled && el.tagName !== "INPUT" ? el : null);
});
document.addEventListener("mouseleave", () => setHover(null));

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (Art.cutting) { e.preventDefault(); Art.skip(); return; }
  const inInput = document.activeElement && document.activeElement.tagName === "INPUT";
  const k = e.key;
  if (!inInput) listenForHack(k);
  if (k === "ArrowUp" || k === "ArrowDown" || (!inInput && (k === "ArrowLeft" || k === "ArrowRight"))) {
    e.preventDefault();
    spatial(k.slice(5).toLowerCase());
    return;
  }
  if (k === "Tab") { e.preventDefault(); linear(e.shiftKey ? -1 : 1); return; }
  if (k === "Enter") {
    e.preventDefault();
    if (inInput) { const act = document.activeElement.dataset.enter; if (act && ACT[act]) ACT[act](); return; }
    // Nothing under the cursor yet: Enter means the screen's main button.
    const target = focused && !focused.disabled ? focused : $("#main .primary:not([disabled])");
    if (target) target.click();
    return;
  }
  if (k === "Escape") {
    e.preventDefault();
    if (inInput) { document.activeElement.blur(); return; }
    ACT.back();
    return;
  }
  if (inInput) return;
  if (k === " ") { e.preventDefault(); if (focused) focused.click(); return; }
  if (k === "m" || k === "M") { Sfx.toggle(); return; }
  if (k === "v" || k === "V") { Voice.toggle(); return; }
  if ((k === "p" || k === "P") && S.screen === "voice") {
    const arg = focused && focused.dataset.act === "personality" ? focused.dataset.arg : S.a.personality;
    ACT.audition(arg);
  }
});

$("#t-sfx").addEventListener("click", () => Sfx.toggle());
$("#t-voice").addEventListener("click", () => Voice.toggle());

// ── Start ───────────────────────────────────────────────────────────────

(function start() {
  Sfx.init();
  Voice.init();
  paintTop();
  const go = async () => {
    api = Q.has("mock") ? window.MockApi(Q.get("mock")) : realApi(Q.get("t") || "");
    try { LINES = await (await fetch("lines.json", { cache: "no-store" })).json(); } catch (_) { /* typed blank, spoken never */ }
    await Voice.load();
    boot();
  };
  if (Q.has("mock") && !window.MockApi) {
    const s = document.createElement("script");
    s.src = "mock.js";
    s.onload = go;
    document.head.appendChild(s);
  } else go();
})();
