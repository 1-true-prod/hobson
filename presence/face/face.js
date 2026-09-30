// Hobson's face: Valet, an ASCII bust (parted hair, wing collar, a glowing
// bow tie) that says each line as it is spoken, over a slightly spotty
// signal. Drawn in code; nothing here is from a film or a game.
//
// The presence helper (presence/Face.swift) loads this page in a
// transparent panel and drives it through three calls:
//
//   Face.say({text, kind, project, start, env, frame, hold})  -> duration (s)
//       start: epoch seconds the audio began. env: loudness 0..1, one value
//       per `frame` seconds, from the file afplay is playing. Without env
//       (no audio, or it was gone), the mouth and caption follow syllables
//       estimated from the text.
//   Face.gaze(x, y)   where you are, -1..1 from his point of view; null = no one
//   Face.hide()       CRT off, then {type: "hidden"} to the helper
//
// It posts {type: "size", w, h} when its size changes, so the panel fits.
// The caption is set through textContent only; data comes in as arguments,
// never as script text. ?mock runs it alone in a browser.
(() => {
"use strict";

const TINT = [25, 230, 255], WARN = [255, 59, 92], MAGENTA = [255, 43, 214], INK = [230, 239, 233];
const MONO = '"SF Mono", SFMono-Regular, ui-monospace, Menlo, Monaco, monospace';
const REDUCE = matchMedia("(prefers-reduced-motion: reduce)").matches;
const FRAME = REDUCE ? 1 / 8 : 1 / 30;
const LINGER = 2.6;          // after the line: nod, then rest
const GAZE_FRESH = 1.5;      // a sighting older than this is no one

// What each kind of line is, on his face. Red is a failure and nothing else:
// a watchdog stall waits on you, it has not failed.
const MOODS = {
  done: { chip: "finished", pose: "done" },
  waiting: { chip: "needs you", pose: "waiting" },
  nudge: { chip: "needs you", pose: "waiting" },
  stalled: { chip: "stalled", pose: "waiting" },
  broken: { chip: "failed", pose: "broken" },
  briefing: { chip: "welcome back", pose: "briefing" },
  commentary: { chip: "working", pose: "idle" },
  answer: { chip: "at your service", pose: "briefing" },   // you asked (ask.py)
  thinking: { chip: "one moment", pose: "thinking" },      // until the answer replaces it
  info: { chip: "", pose: "idle" },
};
const BROWS = { waiting: [1, 0.3], briefing: [0.5, 0], broken: [-0.4, 1], thinking: [0.45, -0.2] };

const clamp = (x, a, b) => (x < a ? a : x > b ? b : x);
const smooth = (e0, e1, x) => { const t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); };
const g2 = (x, y, sx, sy) => Math.exp(-((x * x) / (sx * sx) + (y * y) / (sy * sy)));
const nowS = () => performance.now() / 1000;
const post = (msg) => { try { window.webkit.messageHandlers.face.postMessage(msg); } catch (e) { /* a browser */ } };

// ── Speech ─────────────────────────────────────────────────────────────
// Syllables estimated from the text: each word's vowel groups, with pauses
// at punctuation. The fallback when there is no audio to follow.
function makePlan(text) {
  const syl = [], words = [];
  const re = /\S+/g;
  let m, t = 0.08;
  while ((m = re.exec(text))) {
    const w = m[0];
    const letters = w.toLowerCase().replace(/[^a-z]/g, "");
    const digits = w.replace(/\D/g, "");
    let groups = letters.match(/[aeiouy]+/g) || [];
    if (groups.length > 1 && /[^aeiouy]e$/.test(letters)) groups = groups.slice(0, -1);
    if (!groups.length && digits) groups = Array(Math.min(6, digits.length + 1)).fill("a");
    if (!groups.length && letters) groups = ["a"];
    const start = t;
    for (const g of groups) {
      const d = 0.13 + Math.random() * 0.06;
      syl.push({ t0: t, d, a: 0.55 + Math.random() * 0.45, round: /[ouw]/.test(g) });
      t += d;
    }
    words.push({ start, end: t, charEnd: m.index + w.length });
    if (groups.length) t += 0.045;
    if (/[,;:]$/.test(w) || !groups.length) t += 0.2;
    if (/[.!?]$/.test(w)) t += 0.38;
  }
  return { syl, words, dur: t };
}

function planEnv(plan, t) {
  let v = 0, round = false;
  for (const s of plan.syl) {
    if (s.t0 > t) break;
    const ph = (t - s.t0) / s.d;
    if (ph <= 1) {
      const k = Math.pow(Math.sin(Math.PI * ph), 0.7) * s.a;
      if (k > v) { v = k; round = s.round; }
    }
  }
  return { v: v * (0.88 + 0.12 * Math.sin(t * 41)), round };
}

// Word ends, for revealing the caption a word at a time.
function wordEnds(text) {
  const ends = [], re = /\S+/g;
  let m;
  while ((m = re.exec(text))) ends.push(m.index + m[0].length);
  return ends;
}

// One line being said: the loudness and the caption at time t into it.
class Utterance {
  constructor(u) {
    this.text = String(u.text || "");
    this.kind = MOODS[u.kind] ? u.kind : "info";
    this.project = u.project ? String(u.project) : "";
    this.hold = !!u.hold;
    this.ends = wordEnds(this.text);
    const env = Array.isArray(u.env) && u.env.length > 1 ? u.env.map((x) => clamp(+x || 0, 0, 1)) : null;
    if (this.kind === "thinking") {
      // Said by no one: shown whole at once, the mouth still.
      this.plan = { syl: [], words: [], dur: 0 };
      this.dur = 0;
    } else if (env) {
      this.env = env;
      this.frame = +u.frame > 0 ? +u.frame : 0.016;
      this.dur = env.length * this.frame;
      // The caption follows cumulative loudness, so a pause does not move it.
      this.cum = new Float32Array(env.length + 1);
      for (let i = 0; i < env.length; i++) this.cum[i + 1] = this.cum[i] + env[i];
    } else {
      this.plan = makePlan(this.text);
      this.dur = this.plan.dur;
    }
    // start is wall-clock (epoch seconds); translate it onto this page's clock.
    const epoch = +u.start > 0 ? +u.start : Date.now() / 1000;
    this.start = nowS() - (Date.now() / 1000 - epoch);
  }

  loudness(t) {
    if (t < 0 || t >= this.dur) return { v: 0, round: false };
    if (!this.env) return planEnv(this.plan, t);
    return { v: this.env[Math.min(this.env.length - 1, Math.floor(t / this.frame))], round: false };
  }

  shown(t) {
    if (t >= this.dur) return this.text;
    if (t <= 0) return "";
    let n = 0;
    if (this.env) {
      const total = this.cum[this.cum.length - 1] || 1;
      const k = this.cum[Math.min(this.cum.length - 1, Math.floor(t / this.frame))] / total;
      const upto = k * this.text.length;
      for (const e of this.ends) { if (e - 1 <= upto) n = e; else break; }
    } else {
      for (const w of this.plan.words) { if (w.start <= t) n = w.charEnd; else break; }
    }
    return this.text.slice(0, n);
  }
}

// ── The head: ellipsoids, lit from the left, a height field for features ─
const LD = (() => { const v = [-0.38, 0.3, 0.87], l = Math.hypot(...v); return v.map((x) => x / l); })();
const HALF = (() => { const v = [LD[0], LD[1], LD[2] + 1], l = Math.hypot(...v); return v.map((x) => x / l); })();

function mul(A, B) {
  const C = new Array(9);
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) C[i * 3 + j] = A[i * 3] * B[j] + A[i * 3 + 1] * B[3 + j] + A[i * 3 + 2] * B[6 + j];
  return C;
}
function rotation(yaw, pitch, roll) {
  const cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch), cr = Math.cos(roll), sr = Math.sin(roll);
  const Ry = [cy, 0, sy, 0, 1, 0, -sy, 0, cy];
  const Rx = [1, 0, 0, 0, cp, sp, 0, -sp, cp];
  const Rz = [cr, -sr, 0, sr, cr, 0, 0, 0, 1];
  return mul(Ry, mul(Rx, Rz));
}
function toWorld(R, x, y, z) { return [R[0] * x + R[1] * y + R[2] * z, R[3] * x + R[4] * y + R[5] * z, R[6] * x + R[7] * y + R[8] * z]; }

// The lit surface: the normal perturbed by the height field, in world space.
function surface(h, s, hf) {
  let nx = h.nx, ny = h.ny, nz = h.nz, H = 0;
  if (h.z > 0) {
    const e = 0.012, w = clamp(h.nz * 1.4, 0, 1);
    H = hf(h.u, h.v) * w;
    const hu = (hf(h.u + e, h.v) - hf(h.u - e, h.v)) / (2 * e);
    const hv = (hf(h.u, h.v + e) - hf(h.u, h.v - e)) / (2 * e);
    nx -= hu * w; ny -= hv * w;
    const inv = 1 / Math.hypot(nx, ny, nz);
    nx *= inv; ny *= inv; nz *= inv;
  }
  const [wx, wy, wz] = toWorld(s.R, nx, ny, nz);
  const diff = Math.max(0, wx * LD[0] + wy * LD[1] + wz * LD[2]);
  return { nx: wx, ny: wy, nz: wz, H, diff, ao: 1 + 4 * Math.min(0, H), rim: Math.pow(1 - Math.max(0, wz), 3) };
}

const HEAD = [
  { c: [0, 0.18, 0], r: [0.62, 0.78, 0.7], part: "skin" },          // cranium
  { c: [0, -0.25, 0.06], r: [0.52, 0.54, 0.6], part: "skin" },      // jaw
  { c: [-0.33, -0.44, -0.02], r: [0.2, 0.2, 0.42], part: "skin" },  // the angles of the jaw
  { c: [0.33, -0.44, -0.02], r: [0.2, 0.2, 0.42], part: "skin" },
  { c: [-0.61, 0.0, -0.04], r: [0.075, 0.17, 0.12], part: "ear" },
  { c: [0.61, 0.0, -0.04], r: [0.075, 0.17, 0.12], part: "ear" },
];
const CAP = { c: [0, 0.2, -0.02], r: [0.66, 0.815, 0.735], part: "hair" };
const PARTING = 0.2;

// A ray straight into the screen at world (wx, wy), in head space.
function localRay(wx, wy, s) {
  const R = s.R, oy = wy - s.bob;
  return { ox: R[0] * wx + R[3] * oy + R[6] * 3, oy: R[1] * wx + R[4] * oy + R[7] * 3, oz: R[2] * wx + R[5] * oy + R[8] * 3, dx: -R[6], dy: -R[7], dz: -R[8] };
}
function hitE(ray, e) {
  const qx = (ray.ox - e.c[0]) / e.r[0], qy = (ray.oy - e.c[1]) / e.r[1], qz = (ray.oz - e.c[2]) / e.r[2];
  const ex = ray.dx / e.r[0], ey = ray.dy / e.r[1], ez = ray.dz / e.r[2];
  const a = ex * ex + ey * ey + ez * ez, b = 2 * (qx * ex + qy * ey + qz * ez), c = qx * qx + qy * qy + qz * qz - 1;
  const disc = b * b - 4 * a * c;
  if (disc < 0) return Infinity;
  const t = (-b - Math.sqrt(disc)) / (2 * a);
  return t > 0 ? t : Infinity;
}
function hitAt(ray, t, e) {
  const u = ray.ox + ray.dx * t, v = ray.oy + ray.dy * t, z = ray.oz + ray.dz * t;
  const nx = (u - e.c[0]) / (e.r[0] * e.r[0]), ny = (v - e.c[1]) / (e.r[1] * e.r[1]), nz = (z - e.c[2]) / (e.r[2] * e.r[2]);
  const inv = 1 / Math.hypot(nx, ny, nz);
  return { u, v, z, nx: nx * inv, ny: ny * inv, nz: nz * inv, part: e.part };
}

// Above the hairline: a high forehead in front, dropping to just over the ears.
function isHair(u, v, z) {
  const front = smooth(0.05, 0.5, z);
  const au = Math.abs(u);
  const line = front * (0.54 - 0.1 * (au / 0.3) ** 2 - 0.12 * smooth(0.22, 0.5, au)) + (1 - front) * 0.14;
  return v > line;
}

function faceHeight(u, v) {
  const au = Math.abs(u);
  const bandNose = smooth(-0.27, -0.2, v) * (1 - smooth(0.04, 0.12, v));
  return -0.06 * g2(au - 0.235, v - 0.08, 0.12, 0.08)          // eye sockets
    + 0.035 * g2(au - 0.22, v - 0.19, 0.16, 0.045)              // brow ridge
    + 0.09 * Math.exp(-(u * u) / 0.0018) * bandNose             // a long, narrow nose
    + 0.045 * g2(u, v + 0.225, 0.055, 0.045)                    // its tip
    + 0.02 * g2(au - 0.055, v + 0.235, 0.035, 0.03)             // its wings
    + 0.032 * g2(au - 0.31, v + 0.1, 0.1, 0.07)                 // cheekbones
    - 0.02 * g2(au - 0.27, v + 0.3, 0.08, 0.1)                  // the hollow under them
    + 0.018 * g2(u, v + 0.395, 0.13, 0.025)                     // upper lip
    + 0.022 * g2(u, v + 0.455, 0.12, 0.03)                      // lower lip
    - 0.012 * g2(u, v + 0.52, 0.12, 0.025)                      // the groove under it
    + 0.035 * g2(u, v + 0.66, 0.14, 0.08);                      // chin
}

// A stroke along a direction on screen.
function along(x, y) {
  let a = Math.atan2(y, x);
  a = ((a % Math.PI) + Math.PI) % Math.PI;
  if (a < Math.PI / 8 || a >= (7 * Math.PI) / 8) return "-";
  if (a < (3 * Math.PI) / 8) return "/";
  if (a < (5 * Math.PI) / 8) return "|";
  return "\\";
}

function browArch(au, s) {
  const x = clamp((au - 0.075) / 0.295, 0, 1);
  return 0.205 + 0.03 * Math.sin(Math.PI * Math.min(1, x * 1.2)) + 0.035 * s.brow + 0.032 * s.browTilt * (1 - x);
}
function browAt(u, v, s) {
  const au = Math.abs(u);
  if (au < 0.075 || au > 0.37) return null;
  const x = (au - 0.075) / 0.295, arch = browArch(au, s);
  if (Math.abs(v - arch) > 0.03 * (1 - 0.35 * x)) return null;
  const slope = ((browArch(au + 0.01, s) - arch) / 0.01) * Math.sign(u);
  return Math.abs(slope) < 0.3 ? (x < 0.45 ? "~" : "-") : slope > 0 ? "/" : "\\";
}

// The eye: an almond between lids, a glowing iris that looks at you.
function eyeAt(u, v, s) {
  const side = u < 0 ? -1 : 1, cx = side * 0.235, cy = 0.085;
  const ex = (u - cx) / 0.088;
  if (Math.abs(ex) > 1.1) return null;
  const halfH = 0.044 * s.open * Math.pow(Math.max(0, 1 - ex * ex), 0.6);
  const dy = v - cy;
  if (dy >= halfH && dy < halfH + 0.03) return { ch: halfH < 0.008 ? "-" : Math.abs(ex) > 0.8 ? (ex * side > 0 ? "\\" : "/") : "-", L: 0.3, E: 0 };
  if (Math.abs(dy) >= halfH) return null;
  const ix = cx + s.gx * 0.034, iy = cy + s.gy * 0.012;
  const d = Math.hypot((u - ix) / 0.038, (v - iy) / 0.034);
  if (d < 0.45) return { ch: "@", L: 0.2, E: 1 };
  if (d < 1) return { ch: "O", L: 0.25, E: 0.85 };
  return { ch: "=", L: 0.46 - 0.16 * Math.abs(ex), E: 0 };
}

// The mouth: a straight line with the corners turned a little down, which
// opens on his voice and shows the light inside.
function mouthAt(u, v, s) {
  const mw = 0.14 * (s.round ? 0.78 : 1) * (0.94 + 0.12 * s.env);
  const ex = u / mw;
  if (Math.abs(ex) > 1.08) return null;
  const my = -0.42 - 0.014 * ex * ex;
  const half = (0.005 + 0.085 * s.env * (s.round ? 1.15 : 1)) * Math.sqrt(Math.max(0, 1 - ex * ex));
  const dy = v - my;
  if (half > 0.012 && Math.abs(dy) < half) return { inside: Math.sqrt(1 - (dy / half) ** 2) };
  if (Math.abs(dy) < 0.028 + half * 0.3) return { line: true, end: Math.abs(ex) > 0.85 };
  return null;
}

function hairAt(h, s) {
  if (Math.abs(h.u - PARTING) < 0.02 && h.z > 0.15) return { ch: " ", L: 0, E: 0 };
  const big = h.u < PARTING;
  const k = big ? clamp((PARTING - h.u) / 0.6, 0, 1) : clamp((h.u - PARTING) / 0.42, 0, 1);
  const du = big ? -1 : 1, dv = big ? -0.12 - 1.1 * k : -0.3 - 1.2 * k;
  const [sx, sy] = toWorld(s.R, du, dv, 0);
  const [nx, ny, nz] = toWorld(s.R, h.nx, h.ny, h.nz);
  const diff = Math.max(0, nx * LD[0] + ny * LD[1] + nz * LD[2]);
  const spec = Math.pow(Math.max(0, nx * HALF[0] + ny * HALF[1] + nz * HALF[2]), 18);
  const across = (h.u * -dv + h.v * du) / Math.hypot(du, dv);
  const stripe = 0.5 + 0.5 * Math.sin(across * 70 + (big ? 0 : 1.3));
  const L = 0.1 + 0.28 * diff + 0.12 * stripe + 0.62 * spec * (0.35 + 0.65 * stripe);
  return { ch: L < 0.09 ? "." : along(sx, sy), L, E: Math.pow(1 - Math.max(0, nz), 3) * 0.38 };
}

const SKIN = " .:-=+*#%";
function skinAt(h, s) {
  const sf = surface(h, s, faceHeight);
  let L = (0.05 + 0.6 * Math.pow(sf.diff, 1.1) + 0.12 * Math.max(0, sf.nz)) * sf.ao;
  let E = sf.rim * 0.4, ch = null;
  if (h.part === "ear") return { ch: null, L: L * 0.8, E };
  if (h.z > 0.1) {
    const u = h.u, v = h.v, au = Math.abs(u);
    L *= 1 - 0.55 * g2(au - 0.235, v - 0.085, 0.13, 0.075);  // deep-set eyes
    L *= 1 - 0.45 * g2(u - 0.075, v + 0.13, 0.035, 0.11);   // the nose's shadow, away from the light
    L *= 1 - 0.6 * g2(au - 0.045, v + 0.25, 0.02, 0.014);   // nostrils
    L *= 1 - 0.3 * g2(u, v + 0.31, 0.03, 0.035);            // under the nose
    if (v < -0.24 && v > -0.47) {                           // from the nose to the corners of the mouth
      const fx = au - (0.085 - (v + 0.25) * 0.45);
      L *= 1 - 0.32 * Math.exp(-(fx * fx) / 0.0003);
    }
    const brow = browAt(u, v, s);
    if (brow) return { ch: brow, L: 0.26 + 0.08 * L, E: 0 };
    const eye = eyeAt(u, v, s);
    if (eye) return eye;
    const m = mouthAt(u, v, s);
    if (m && m.inside) { L *= 0.08; E = Math.max(E, s.env * 0.8 * m.inside); ch = E > 0.18 ? "=" : " "; }
    else if (m && m.line) { ch = m.end ? "." : "-"; L = 0.24; }
  }
  // Flat planes read as a face; a tone per pixel reads as texture.
  L = Math.round(clamp(L, 0, 0.9) * 8) / 8;
  return { ch: ch === null ? SKIN[Math.round((L / 0.9) * (SKIN.length - 1))] : ch, L, E };
}

const RAMP = " .,-~:;=+*#%@";
const rampChar = (L) => RAMP[Math.round(clamp(L, 0, 1) * (RAMP.length - 1))];

// The bust, in world units (the head is 1.8 tall): a tailcoat with peaked
// lapels, a waistcoat, a pleated shirt, a wing collar, the bow tie, and a
// pocket square. Lit from the left like the head.
const lapelOuter = (d, wc) => wc + 0.2 + 0.17 * Math.exp(-(((d - 0.55) / 0.2) ** 2));
function bodyAt(wx, wy, col, row) {
  const ax = Math.abs(wx), left = wx < 0;
  const d = -0.95 - wy;                                     // depth below the collar
  const t = clamp((ax - 0.25) / 1.2, 0, 1);
  const top = -0.88 - 0.12 * t - 0.42 * Math.pow(t, 2.4);   // the line of the shoulders
  const shade = left ? 1.15 : 0.8;

  // the bow tie, then the wing collar
  if (ax < 0.2 && Math.abs(wy + 1.03) < 0.045 + 0.075 * (ax / 0.2)) return { ch: ax < 0.04 ? "o" : left ? ">" : "<", L: 0.3, E: 0.95 };
  if (ax > 0.05 && ax < 0.22 && wy < -0.96 && wy > -0.97 - 0.12 * (1 - (ax - 0.05) / 0.17)) return { ch: left ? "\\" : "/", L: 0.88, E: 0 };
  if (ax < 0.28 && wy <= -0.86 && wy >= -0.99) return { ch: "=", L: 0.8 * (left ? 1 : 0.85), E: 0 };
  if (ax < 0.26 && wy > -0.86 && wy < -0.5) {
    const n = wx / 0.26;
    const L = (0.05 + 0.45 * Math.max(0, -0.42 * n + 0.76 * Math.sqrt(1 - n * n))) * (0.3 + 0.7 * smooth(-0.62, -0.86, wy));
    return { ch: rampChar(L), L, E: 0 };
  }
  if (wy >= top || ax > 1.7 || d < 0) return null;

  const shirt = 0.3 - 0.2 * d, wc = 0.56 - 0.06 * d, lap = lapelOuter(d, wc);
  if (ax < shirt) {
    const pleat = Math.abs((((wx / 0.07) % 1) + 1) % 1 - 0.5) < 0.2;
    return { ch: pleat ? "|" : ":", L: (0.66 - 0.1 * d) * (left ? 1 : 0.88), E: 0 };
  }
  if (Math.abs(ax - shirt) < 0.04 && shirt > 0) return { ch: left ? "\\" : "/", L: 0.5, E: 0 };
  if (ax < wc) {
    for (const by of [-2.36, -2.64]) if (ax < 0.035 && Math.abs(wy - by) < 0.04) return { ch: "o", L: 0.85, E: 0 };
    return { ch: (col + row) & 1 ? "+" : ".", L: 0.34 * shade, E: 0 };   // a figured waistcoat
  }
  if (ax < lap) {
    if (d > 0.36 && d < 0.44 && ax > lap - 0.09) return { ch: " ", L: 0, E: 0 };   // the notch
    const edge = ax > lap - 0.04;
    const slope = (lapelOuter(d + 0.02, 0.56 - 0.06 * (d + 0.02)) - lap) / 0.02;
    const ch = edge ? (Math.abs(slope) < 0.5 ? "|" : (slope > 0) === left ? "/" : "\\") : ":";
    return { ch, L: edge ? 0.55 * shade : 0.3 * shade, E: edge ? 0.1 : 0 };
  }
  // the jacket, with a pocket square on the far breast
  if (!left && wy < -1.83 && wy > -1.97 && Math.abs(wx - 0.86) < 0.11 * (1 - (wy + 1.97) / 0.14) + 0.01) return { ch: wx < 0.86 ? "/" : "\\", L: 0.82, E: 0 };
  if (!left && Math.abs(wy + 1.98) < 0.025 && wx > 0.7 && wx < 1.02) return { ch: "_", L: 0.3, E: 0 };
  if (Math.abs(ax - (1.12 + 0.04 * d)) < 0.025 && d > 0.5) return { ch: "|", L: 0.13, E: 0 };
  const n = clamp(wx / 1.7, -1, 1);
  const L = (0.09 + 0.12 * Math.sqrt(1 - n * n)) * shade;
  const rim = wy > top - 0.06 ? 0.3 : 0;
  return { ch: rim ? "-" : L > 0.17 ? ":" : ".", L: L + 0.05, E: rim };   // black cloth
}

// ── The window ─────────────────────────────────────────────────────────
// 70 x 46 characters, cropped at mid-chest: the prototype's scale, 54 rows
// of it with the bottom 8 cut.
const COLS = 70, ROWS = 46, SPAN = 3.9 * ROWS / 54, TOP = -0.85 + 3.9 / 2, CY = TOP - SPAN / 2;

class Window {
  constructor(win) {
    this.win = win;
    this.canvas = win.querySelector("canvas");
    this.ctx = this.canvas.getContext("2d");
    this.capEl = win.querySelector(".cap");
    this.said = win.querySelector(".said");
    this.chip = win.querySelector(".chip");
    this.proj = win.querySelector(".proj");
    this.bars = win.querySelector(".bars");
    this.seed = Math.random() * 10;
    this.pose = { yaw: 0, pitch: 0, roll: 0, ex: 0, ey: 0, brow: 0, browTilt: 0 };
    this.tint = TINT.slice();
    this.env = 0;
    this.nextBlink = nowS() + 1 + Math.random() * 3;
    this.blinkAt = -9;
    this.glance = { x: 0, y: 0, next: 0 };
    this.seen = { x: 0, y: 0, t: -99 };
    this.u = null;
    this.running = false;
    this.last = 0;
    this.shownKey = "";
    const n = COLS * ROWS;
    this.ch = new Array(n).fill(" ");
    this.L = new Float32Array(n);
    this.E = new Float32Array(n);
    this.shift = new Float32Array(ROWS);
    this.sig = { next: nowS() + 3 + Math.random() * 9, dipUntil: 0, events: [], lvl: 4, alpha: 1 };
    this.fit();
    // The canvas's own column, not only the window: its width can change
    // while the window's size does not, and a stale fit squeezes the bust.
    const ro = new ResizeObserver(() => { this.fit(); this.report(); });
    ro.observe(this.win);
    ro.observe(this.canvas.parentElement);
    this.win.addEventListener("animationend", (e) => {
      if (e.animationName === "crt-off") this.hidden();
    });
  }

  fit() {
    const W = this.canvas.parentElement.clientWidth;
    if (!W || W === this.fitW) return;
    this.fitW = W;
    this.ctx.font = `400 100px ${MONO}`;
    const adv = this.ctx.measureText("M").width / 100 || 0.6;
    this.fs = W / (COLS * adv);
    this.cw = this.fs * adv;
    this.lh = this.fs * 1.18;
    this.W = COLS * this.cw;
    this.H = ROWS * this.lh;
    this.dpr = Math.min(2, window.devicePixelRatio || 1);
    this.canvas.style.width = this.W + "px";
    this.canvas.style.height = this.H + "px";
    this.canvas.width = Math.round(this.W * this.dpr);
    this.canvas.height = Math.round(this.H * this.dpr);
    this.scale = SPAN / this.H;
  }

  report() {
    // offsetWidth/Height ignore the CRT's transform: the size it will have.
    const w = this.win.offsetWidth, h = this.win.offsetHeight;
    if (w && h && (w !== this.sentW || h !== this.sentH)) {
      this.sentW = w; this.sentH = h;
      post({ type: "size", w, h });
    }
  }

  world(c, r) {
    const px = (c + 0.5) * this.cw - this.W / 2, py = (r + 0.5) * this.lh - this.H / 2;
    return [px * this.scale, CY - py * this.scale];
  }

  say(u) {
    this.u = new Utterance(u);
    this.hideAt = 0;
    this.proj.textContent = this.u.project;
    const state = this.win.dataset.state;
    if (state !== "on") {
      this.win.dataset.state = "hidden";
      void this.win.offsetWidth;   // restart the CRT
      this.win.dataset.state = "on";
    }
    this.start();
    return this.u.dur;
  }

  hide() {
    if (this.win.dataset.state === "off" || this.win.dataset.state === "hidden") return;
    this.win.dataset.state = "off";
    if (REDUCE) setTimeout(() => this.hidden(), 220);
  }

  hidden() {
    if (this.win.dataset.state !== "off") return;
    this.win.dataset.state = "hidden";
    this.running = false;
    this.u = null;
    post({ type: "hidden" });
  }

  gaze(x, y) {
    if (x == null || y == null) { this.seen.t = -99; return; }
    this.seen = { x: clamp(+x || 0, -1, 1), y: clamp(+y || 0, -1, 1), t: nowS() };
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.last = 0;
    const loop = () => {
      if (!this.running) return;
      const now = nowS();
      if (now - this.last >= FRAME) {
        const dt = Math.min(0.1, this.last ? now - this.last : FRAME);
        this.last = now;
        this.frame(now, dt);
      }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }

  frame(now, dt) {
    const u = this.u;
    let st = -1, after = 99, speaking = false, env = { v: 0, round: false };
    if (u) {
      st = now - u.start;
      if (st >= 0 && st < u.dur) { speaking = true; env = u.loudness(st); }
      else if (st >= u.dur) after = st - u.dur;
    }
    // On from the moment it is said until LINGER after it ends (or, held, until hidden).
    const active = !!u && (u.hold || st < u.dur + LINGER);
    const kind = u ? u.kind : "info";
    const pose = active ? MOODS[kind].pose : "idle";

    let gx, gy;
    if (pose === "thinking") {
      // Up and aside, as someone looking something up.
      gx = -0.55; gy = 0.5;
    } else if (now - this.seen.t < GAZE_FRESH) {
      gx = this.seen.x; gy = this.seen.y;
    } else {
      if (now > this.glance.next) {
        const home = Math.random() < 0.5 || speaking;
        this.glance.x = home ? 0 : (Math.random() - 0.5) * 0.75;
        this.glance.y = home ? 0 : (Math.random() - 0.5) * 0.4;
        this.glance.next = now + 1.2 + Math.random() * 2.8;
      }
      gx = this.glance.x; gy = this.glance.y;
    }

    if (now > this.nextBlink) {
      this.blinkAt = now;
      this.nextBlink = now + (Math.random() < 0.15 ? 0.3 : 2.2 + Math.random() * 4.5);
    }
    const bt = (now - this.blinkAt) / 0.16;
    let open = bt >= 0 && bt < 1 ? Math.abs(bt * 2 - 1) : 1;
    if (pose === "done" && after > 0.2 && after < 0.9) open = Math.min(open, 0.35 + 0.65 * Math.abs((after - 0.55) / 0.35));

    const p = this.pose, k = 1 - Math.exp(-dt * 4.5), ke = 1 - Math.exp(-dt * 14);
    const nod = pose === "done" && after >= 0 && after < 0.9 ? Math.sin((Math.PI * after) / 0.9) : 0;
    const bow = pose === "briefing" && st >= -0.2 && st < 1.1 ? Math.sin((Math.PI * clamp(st + 0.2, 0, 1.3)) / 1.3) : 0;
    const yawT = gx * 0.34 + 0.05 * Math.sin(now * 0.37 + this.seed);
    const pitchT = gy * 0.2 + (pose === "broken" ? -0.15 : 0) + this.env * 0.05 - nod * 0.17 - bow * 0.2;
    const rollT = (pose === "waiting" ? 0.13 : 0) + 0.025 * Math.sin(now * 0.23 + this.seed);
    p.yaw += (yawT - p.yaw) * k;
    p.pitch += (pitchT - p.pitch) * k;
    p.roll += (rollT - p.roll) * k;
    p.ex += (gx - p.ex) * ke;
    p.ey += (gy - p.ey) * ke;
    const [liftT, tiltT] = BROWS[pose] || [0, 0];
    p.brow += (liftT - p.brow) * k;
    p.browTilt += (tiltT - p.browTilt) * k;
    const target = u && kind === "broken" ? WARN : TINT;
    for (let j = 0; j < 3; j++) this.tint[j] += (target[j] - this.tint[j]) * k;
    this.env += (env.v - this.env) * (1 - Math.exp(-dt * 30));

    const s = {
      t: now, env: this.env, round: env.round, open,
      gx: p.ex, gy: p.ey, brow: p.brow, browTilt: p.browTilt,
      R: rotation(p.yaw, p.pitch, p.roll),
      bob: Math.sin(now * 1.3 + this.seed) * 0.012,
      tint: this.tint,
    };
    this.shift.fill(0);
    this.render(s);
    const chroma = this.transmission(now, st);
    this.draw(s, chroma);
    this.caption(u, st, active, kind);
  }

  render(s) {
    for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
      const i = r * COLS + c, [wx, wy] = this.world(c, r);
      const ray = localRay(wx, wy, s);
      let best = Infinity, be = null, h = null;
      for (const e of HEAD) { const t = hitE(ray, e); if (t < best) { best = t; be = e; } }
      const tc = hitE(ray, CAP);
      if (tc < best) { const hc = hitAt(ray, tc, CAP); if (isHair(hc.u, hc.v, hc.z)) h = hc; }
      if (!h && be) h = hitAt(ray, best, be);
      let cell = null;
      if (h) {
        cell = h.part === "hair" ? hairAt(h, s) : skinAt(h, s);
        if (cell.ch === null) cell.ch = rampChar(cell.L);
      } else {
        cell = bodyAt(wx, wy - s.bob * 0.4, c, r);
      }
      if (cell) { this.ch[i] = cell.ch; this.L[i] = cell.L; this.E[i] = cell.E; }
      else { this.ch[i] = " "; this.L[i] = 0; this.E[i] = 0; }
    }
  }

  // The signal: faint all the time (scanlines, a little snow, a tracking
  // band, colour fringing), and now and then a short bad patch: rows tear
  // sideways, dropouts streak across, the colour slips, the bars fall.
  // A bad patch comes every 8-22s and lasts under a second and a half; a
  // line starting gets a brief one, as the link comes up.
  transmission(now, st) {
    const g = this.sig;
    if (REDUCE) { g.alpha = 1; this.setBars(4); return 0; }
    if (now > g.next) {
      g.dipUntil = now + 0.35 + Math.random() * 1.1;
      g.next = now + 8 + Math.random() * 14;
      g.lvl = 1 + Math.floor(Math.random() * 2);
    }
    const dip = now < g.dipUntil, onset = st >= 0 && st < 0.2;
    if ((dip && Math.random() < 0.22) || (onset && Math.random() < 0.35)) {
      const r = Math.random(), type = r < 0.55 ? "tear" : r < 0.82 ? "dropout" : "chroma";
      g.events.push({
        type, r0: Math.floor(Math.random() * ROWS), h: 1 + Math.floor(Math.random() * (type === "tear" ? 6 : 1)),
        amt: (Math.random() < 0.5 ? -1 : 1) * (0.6 + Math.random() * 2.2),
        c0: Math.floor(Math.random() * COLS), len: 4 + Math.floor(Math.random() * 14),
        until: now + 0.05 + Math.random() * 0.12,
      });
    }
    g.events = g.events.filter((e) => e.until > now);
    let chroma = 0.6;
    for (const e of g.events) {
      if (e.type === "tear") {
        for (let r = e.r0; r < Math.min(ROWS, e.r0 + e.h); r++) this.shift[r] += e.amt * (1 - (r - e.r0) / (e.h + 1));
      } else if (e.type === "dropout") {
        for (let c = e.c0; c < Math.min(COLS, e.c0 + e.len); c++) {
          const i = e.r0 * COLS + c;
          this.ch[i] = Math.random() < 0.7 ? "-" : "="; this.L[i] = 0.78; this.E[i] = 0;
        }
      } else chroma = 2.6;
    }
    // the tracking band, rolling down every dozen seconds or so
    const band = (((now * 0.075 + this.seed) % 1.4) - 0.2) * ROWS, bh = Math.max(2, Math.round(ROWS * 0.05));
    for (let r = Math.max(0, Math.floor(band)); r < Math.min(ROWS, Math.floor(band) + bh); r++) {
      if (dip) this.shift[r] += (Math.random() - 0.5) * 0.8;
      for (let c = 0; c < COLS; c++) { const i = r * COLS + c; if (this.ch[i] !== " ") this.L[i] = Math.min(1, this.L[i] * 1.18 + 0.03); }
    }
    // snow
    const flakes = Math.round(COLS * ROWS * (dip ? 0.02 : 0.003));
    for (let k = 0; k < flakes; k++) {
      const i = (Math.random() * COLS * ROWS) | 0;
      if (this.ch[i] === " ") { this.ch[i] = Math.random() < 0.6 ? "." : "'"; this.L[i] = 0.14 + Math.random() * 0.14; this.E[i] = 0; }
    }
    g.alpha = dip && Math.random() < 0.12 ? 0.78 : 0.95 + Math.random() * 0.05;
    this.setBars(dip ? g.lvl : 4);
    return chroma;
  }

  setBars(lvl) {
    if (this.bars.dataset.lvl !== String(lvl)) this.bars.dataset.lvl = String(lvl);
  }

  draw(s, split) {
    const { ctx, cw, lh, dpr } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.W, this.H);
    ctx.globalAlpha = this.sig.alpha;
    ctx.font = `400 ${this.fs}px ${MONO}`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    const tint = s.tint;
    // Brightness and glow are quantised into groups, one fillStyle each.
    const groups = new Map();
    const glow = [];
    for (let i = 0; i < COLS * ROWS; i++) {
      const ch = this.ch[i];
      if (ch === " ") continue;
      const L = this.L[i], E = this.E[i];
      if (L < 0.03 && E < 0.03) continue;
      const qL = Math.min(10, Math.round(clamp(L, 0, 1) * 10)), qE = Math.min(8, Math.round(clamp(E, 0, 1) * 8));
      const key = qL * 9 + qE;
      let g = groups.get(key);
      if (!g) groups.set(key, (g = []));
      g.push(i);
      if (E > 0.5) glow.push(i);
    }
    const x = (i) => ((i % COLS) + 0.5 + this.shift[(i / COLS) | 0]) * cw;
    const y = (i) => (((i / COLS) | 0) + 0.5) * lh;
    const rgb = (a) => `rgb(${a[0] | 0},${a[1] | 0},${a[2] | 0})`;

    if (split) {
      // Colour fringing, like the logo's RGB split.
      ctx.globalCompositeOperation = "lighter";
      const a = split > 1 ? 0.3 : 0.18;
      for (const [col, dx] of [[MAGENTA, -split], [TINT, split]]) {
        ctx.fillStyle = `rgba(${col[0]},${col[1]},${col[2]},${a})`;
        for (const idx of groups.values()) for (const i of idx) if (this.L[i] > 0.4 || this.E[i] > 0.2) ctx.fillText(this.ch[i], x(i) + dx, y(i));
      }
      ctx.globalCompositeOperation = "source-over";
    }
    for (const [key, idx] of groups) {
      const qL = Math.floor(key / 9) / 10, qE = (key % 9) / 8;
      ctx.fillStyle = rgb([0, 1, 2].map((j) => INK[j] * qL * (1 - qE) + tint[j] * qE));
      for (const i of idx) ctx.fillText(this.ch[i], x(i), y(i));
    }
    if (glow.length) {
      ctx.shadowColor = `rgba(${tint[0] | 0},${tint[1] | 0},${tint[2] | 0},0.9)`;
      ctx.shadowBlur = Math.max(4, this.fs * 0.9);
      ctx.fillStyle = rgb(tint);
      for (const i of glow) ctx.fillText(this.ch[i], x(i), y(i));
      ctx.shadowBlur = 0;
      ctx.shadowColor = "transparent";
    }
    ctx.globalAlpha = 1;
  }

  caption(u, st, active, kind) {
    const text = u ? u.shown(st) : "";
    const past = !!u && !active;
    const chip = u && active ? MOODS[kind].chip : "";
    const key = `${text}|${past}|${chip}|${kind}`;
    if (key === this.shownKey) return;
    this.shownKey = key;
    this.said.textContent = text;
    this.capEl.classList.toggle("past", past);
    this.chip.textContent = chip;
    this.win.dataset.kind = u ? kind : "idle";
    this.report();
  }
}

const MOCK = /[?&]mock\b/.test(location.search);
if (MOCK) document.documentElement.classList.add("mock");
const face = new Window(document.querySelector(".win"));
window.Face = {
  say: (u) => face.say(u || {}),
  gaze: (x, y) => face.gaze(x, y),
  hide: () => face.hide(),
};
face.report();
post({ type: "ready" });

// ── ?mock: alone in a browser, sample lines on a loop ──────────────────
if (MOCK) {
  const LINES = [
    ["done", "The migration is finished. All 1,001 tests pass.", "hobson"],
    ["waiting", "A question is waiting for you in hobson.", "hobson"],
    ["broken", "The build has failed. The error is on your screen.", "webapp"],
    ["briefing", "Welcome back. Two sessions finished while you were away, and one is waiting on you.", ""],
    ["stalled", "Nothing has moved in webapp for ten minutes.", "webapp"],
    ["thinking", "One moment.", ""],
    ["answer", "On webapp: fixed the login redirect. On hobson: shipped the face.", ""],
  ];
  // Every other line with a made-up loudness track, to exercise that path.
  const synth = (text) => {
    const plan = makePlan(text), frame = 0.016, env = [];
    for (let t = 0; t < plan.dur; t += frame) env.push(planEnv(plan, t).v);
    return { env, frame };
  };
  let i = 0;
  const next = () => {
    const [kind, text, project] = LINES[i % LINES.length];
    const audio = i % 2 ? synth(text) : {};
    i++;
    const dur = face.say({ text, kind, project, start: Date.now() / 1000 + 0.3, ...audio });
    setTimeout(() => face.hide(), (dur + 0.3 + 1.5) * 1000);
    setTimeout(next, (dur + 0.3 + 1.5 + 1.8) * 1000);
  };
  setTimeout(next, 500);
  addEventListener("pointermove", (e) => {
    const r = face.canvas.getBoundingClientRect();
    face.gaze((e.clientX - (r.left + r.width / 2)) / 520, -(e.clientY - (r.top + r.height * 0.42)) / 420);
  }, { passive: true });
}
})();
