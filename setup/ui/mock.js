// A stand-in for setup_wizard.py's /api, for working on the look without a
// backend: open index.html?mock (or ?mock=update, ?mock=bare). Same shapes
// as the real server; nothing here touches the machine.
"use strict";

window.MockApi = function (kind) {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const bare = kind === "bare";
  const update = kind === "update";

  const probe = {
    host: "HOBSON-MAC",
    arch: "arm64",
    macos: "26.7",
    ram_gb: 48,
    disk_free_gb: bare ? 11 : 58,
    python: "3.14.6",
    uv: !bare,
    brew: true,
    hooks: update ? "current" : "missing",
    existing: update
      ? { engine: "pocket-tts", voice: null, voice_custom: "butler-voice.safetensors", personality: "hobson", events: ["stop", "permission", "notification"], verbosity: "normal", model: "llama3.2:3b", decider: "local", presence: "auto", greetings: true, phone: null }
      : null,
    seen: update ? ["voice", "events", "brain"] : [],
    engines: [
      { id: "say", ready: true },
      { id: "kokoro-realtime", ready: update, needs: bare ? "uv (installed for you)" : null },
      { id: "pocket-tts", ready: false },
      { id: "chatterbox", ready: false },
    ],
    personalities: [
      { id: "hobson", name: "Hobson", desc: "A courteous machine intelligence with a butler's manners. Precise, unflappable, faintly uncanny.", sample: "All sorted, sir. Tidied up nicely." },
      { id: "minimal", name: "Minimal", desc: "Terse, no-nonsense notifications. Quick and quiet.", sample: "Done. Ready for you." },
      { id: "pirate", name: "Pirate Captain", desc: "A salty sea captain narrating your code adventures. Arrr!", sample: "Land ho! Smooth sailin' that was." },
      { id: "snarky-dev", name: "Snarky Dev", desc: "A jaded senior developer who's seen it all. Dry, sarcastic, secretly helpful.", sample: "Oh look, it works. Ship it before someone notices." },
    ],
    ollama: bare
      ? { installed: false, running: false, version: null, models: [] }
      : { installed: true, running: true, version: "0.32.5", models: ["llama3.2:3b", "gemma4:e4b", "qwen3.5:4b", "mistral:7b"] },
    voices: {
      "kokoro-realtime": [
        { id: "am_puck", name: "Puck", sex: "male", accent: "American" },
        { id: "bm_george", name: "George", sex: "male", accent: "British" },
        { id: "bm_daniel", name: "Daniel", sex: "male", accent: "British" },
        { id: "bm_fable", name: "Fable", sex: "male", accent: "British" },
        { id: "bf_emma", name: "Emma", sex: "female", accent: "British" },
        { id: "bf_isabella", name: "Isabella", sex: "female", accent: "British" },
        { id: "af_heart", name: "Heart", sex: "female", accent: "American" },
      ],
      "pocket-tts": [
        { id: "charles", name: "Charles", sex: "male", accent: "British" },
        { id: "paul", name: "Paul", sex: "male", accent: "British" },
        { id: "anna", name: "Anna", sex: "female", accent: "British" },
        { id: "vera", name: "Vera", sex: "female", accent: "British" },
        { id: "fantine", name: "Fantine", sex: "female", accent: "British" },
        { id: "eponine", name: "Eponine", sex: "female", accent: "British" },
      ],
    },
    models: [
      { id: "llama3.2:3b", disk_gb: 2.0, note: "What Hobson is tuned on: reads a turn well and speaks naturally as himself. Light enough for any recent Mac.", default: true },
      { id: "gemma4:e4b", disk_gb: 9.6, note: "The best at telling done from broken from a question, but a 9.6 GB download and heavier to run." },
      { id: "qwen3.5:4b", disk_gb: 3.4, note: "Apache-2.0 licensed, but more likely to miss a question for you, and often narrates instead of speaking as Hobson." },
      { id: "llama3.2:1b", disk_gb: 1.3, note: "The smallest and quickest, for a Mac short on memory. Not measured with Hobson: expect more misreads.", untested: true },
    ],
    jev: { key: bare ? null : { source: "env", tail: "a1f3" }, model: "typesafe/jev-1.13", cost_per_call: 0.000017 },
    presence: { built: !bare, swiftc: !bare, camera: bare ? "" : "authorized", mode: "auto", greetings: true },
    phone: bare
      ? { adb: null, devices: [] }
      : { adb: "/opt/homebrew/bin/adb", devices: [{ serial: "adb-00A024RZ-tcp", model: "Nothing A024", transport: "wireless" }] },
    recommend: {
      engine: bare ? "say" : "kokoro-realtime",
      voice: null,
      personality: "hobson",
      events: ["stop", "permission", "notification"],
      verbosity: "normal",
      model: bare ? "llama3.2:1b" : "llama3.2:3b",
      presence: bare ? "signals" : "auto",
      greetings: true,
      phone: null,
      why: {
        engine: bare ? "no uv on this Mac: the built-in voice needs nothing" : "uv is here, so about 340 MB buys a natural voice",
        personality: "courteous, precise, faintly synthetic",
        events: "finishes, approvals and waits; no running commentary",
        model: bare ? "11 GB free: the smallest model" : "the tested default; fits 48 GB with room to spare",
        presence: bare ? "no sensor without the Command Line Tools" : "the camera is already allowed; one glance, only before speaking",
        phone: bare ? "adb not found" : "a phone is connected: set it up in Custom",
      },
    },
  };

  let flipAt = null;
  const defaults = { engine: "say", personality: "hobson", events: "stop,permission,notification", verbosity: "normal", model: "llama3.2:3b", presence: "auto", greetings: true };
  const keyOf = { engine: "engine", personality: "personality", events: "events", verbosity: "commentary.verbosity", model: "ollama.model", presence: "presence.mode", greetings: "presence.greetings" };

  function plan(a) {
    const writes = [];
    let kept = 0;
    for (const k of Object.keys(keyOf)) {
      const v = Array.isArray(a[k]) ? a[k].join(",") : a[k];
      if (v === undefined || v === null || v === "none") continue;
      if (String(v) === String(defaults[k])) kept++;
      else writes.push({ key: keyOf[k], value: Array.isArray(a[k]) ? a[k] : a[k] });
    }
    if (a.model === "none") writes.push({ key: "ollama", value: "not used", note: true });
    if (a.decider === "jev" && a.jev_verified) writes.push({ key: "decider.backend", value: "jev" });
    else kept++;
    if (a.phone) writes.push({ key: "presence.phone", value: a.phone });
    const tasks = [];
    if (a.engine === "kokoro-realtime" && !probe.engines[1].ready) tasks.push({ id: "kokoro", label: "Install Kokoro", detail: "a venv with kokoro-onnx, then 120 MB of model and voices: about 340 MB" });
    if (a.model !== "none" && !probe.ollama.installed) tasks.push({ id: "ollama", label: "Install Ollama", detail: "brew install ollama · brew services start ollama" });
    if (a.model !== "none" && !probe.ollama.models.includes(a.model)) tasks.push({ id: "pull", label: `Pull ${a.model}`, detail: `${probe.models.find((m) => m.id === a.model).disk_gb} GB from ollama.com` });
    if (a.key_typed && a.jev_verified) tasks.push({ id: "key", label: "Save the OpenRouter key", detail: "~/.claude/hobson.env · mode 600" });
    tasks.push({ id: "config", label: "Write settings", detail: "~/.claude/hobson.json" });
    tasks.push({ id: "hooks", label: "Refresh Claude Code hooks", detail: "~/.claude/settings.json" });
    tasks.push({ id: "hello", label: "Say hello", detail: "" });
    return { writes, defaults_kept: kept + 38, secrets: a.key_typed && a.jev_verified ? ["OPENROUTER_API_KEY → ~/.claude/hobson.env"] : [], tasks };
  }

  return {
    async get(path) {
      await sleep(path === "probe" ? 900 : 350);
      if (path === "probe") return JSON.parse(JSON.stringify(probe));
      if (path.startsWith("phone")) {
        if (!flipAt) return { phone: "up", serial: probe.phone.devices[0]?.serial };
        const dt = Date.now() - flipAt;
        return { phone: dt < 2500 ? "up" : dt < 6500 ? "down" : "up", serial: probe.phone.devices[0]?.serial };
      }
      throw new Error("404");
    },
    async post(path, body) {
      if (path === "speak") {
        try {
          const u = new SpeechSynthesisUtterance(body.text || "");
          u.rate = 1.05;
          speechSynthesis.cancel();
          speechSynthesis.speak(u);
        } catch (_) { /* no voice in this browser */ }
        await sleep(300);
        return { ok: true };
      }
      if (path === "jev/test") {
        await sleep(1400);
        if ((body.key || "").includes("bad")) return { ok: false, reason: "401 · the key was refused" };
        return { ok: true, ms: 612, cost: 0.000017 };
      }
      if (path === "presence/look") { await sleep(2100); return { camera: "authorized", people: 1, confident: true }; }
      if (path === "phone/test") { flipAt = Date.now(); return { ok: true }; }
      if (path === "plan") { await sleep(500); return plan(body.answers); }
      if (path === "done") return { ok: true };
      throw new Error("404");
    },
    async stream(path, body, onEvent) {
      if (path !== "apply") throw new Error("404");
      const tasks = plan(body.answers).tasks;
      for (const t of tasks) {
        onEvent({ task: t.id, state: "run" });
        if (t.id === "pull" || t.id === "kokoro") {
          for (let p = 0; p <= 100; p += 4) { await sleep(90); onEvent({ task: t.id, state: "run", progress: p / 100, detail: t.id === "pull" ? `${((p / 100) * 2.0).toFixed(2)} / 2.00 GB` : "downloading models" }); }
        } else {
          await sleep(500);
        }
        onEvent({ task: t.id, state: "ok" });
      }
      onEvent({ done: true, ok: true });
    },
  };
};
