// Hobson setup — the pictures. Each module has one, shown twice: full-screen
// for a moment when you first arrive (the cutscene), then small in the rail
// for as long as you stay (the monitor).
//
// Every picture goes through the same tube: a 280×185 greyscale frame,
// ordered-dithered to five tones of the module's colour, the colour the city
// is tinted for that screen. The stills are public-domain photographs
// (art/LICENSES.md); the moving ones are drawn here. The tube is what makes
// them one set. Nothing from a film, however filtered: a dithered frame is
// still a copy of that frame.
//
//   Art.has(id)                        does this screen have a picture
//   Art.cutscene(id, n, title)         resolves when the pane may paint
//   Art.mount(slot, id)                put the monitor in the rail
//   Art.progress = 0…1                 COMMIT's tower follows the tasks
//   Art.skip()                         end a cutscene now (any key)
"use strict";

const Art = (() => {
  const W = 280, H = 185;
  const FPS_MS = 50; // 20 frames a second: a period picture, not a smooth one
  const BAYER = [0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21];
  const TINT = { voice: "#19e6ff", events: "#39ff88", brain: "#8c6bff", jev: "#ffc640", presence: "#ff2bd6", phone: "#19e6ff", commit: "#39ff88" };
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const mix = (a, b, k) => a.map((v, i) => Math.round(v + (b[i] - v) * k));
  function ramp(tint) {
    const t = hex(tint), bg = [2, 3, 5];
    return [bg, mix(bg, t, 0.22), mix(bg, t, 0.52), t, mix(t, [255, 255, 255], 0.62)];
  }

  // Greyscale in, five tones out.
  function tube(src, out, pal) {
    const s = src.getImageData(0, 0, W, H).data, d = out.data;
    for (let y = 0; y < H; y++) {
      const row = (y & 7) * 8;
      for (let x = 0; x < W; x++) {
        const i = (y * W + x) * 4;
        const l = (s[i] * 0.3 + s[i + 1] * 0.59 + s[i + 2] * 0.11) / 255;
        let k = Math.floor(l * 4 + (BAYER[row + (x & 7)] + 0.5) / 64);
        k = k < 0 ? 0 : k > 4 ? 4 : k;
        const c = pal[k];
        d[i] = c[0]; d[i + 1] = c[1]; d[i + 2] = c[2]; d[i + 3] = 255;
      }
    }
  }

  // ── Scenes: each draws greyscale into a W×H context ──────────────────

  // A photograph, drifting a little, with a bright band rolling down it.
  function still(src) {
    const img = new Image();
    const ready = new Promise((r) => { img.onload = img.onerror = r; });
    img.src = src;
    return {
      ready,
      draw(g, t) {
        g.fillStyle = "#000";
        g.fillRect(0, 0, W, H);
        if (img.naturalWidth) {
          const s = 1.07, dx = Math.sin(t * 0.17) * 6 - W * (s - 1) / 2, dy = Math.cos(t * 0.13) * 3 - H * (s - 1) / 2;
          g.drawImage(img, dx, dy, W * s, H * s);
        }
        const y = ((t * 26) % (H + 60)) - 30;
        g.fillStyle = "rgba(255,255,255,0.07)";
        g.fillRect(0, y, W, 14);
      },
    };
  }

  // EVENTS: a flight through glass slabs with Hobson's own words on them.
  function slabs() {
    const WORDS = ["FINISHED", "PERMISSION", "WAITING", "COMMENTARY", "DONE", "BROKEN", "A QUESTION", "APPROVAL"];
    const spawn = (z) => ({
      x: (Math.random() < 0.5 ? -1 : 1) * (1.6 + Math.random() * 6), y: Math.random() * 5 - 2.4, z,
      w: 3 + Math.random() * 4, h: 2 + Math.random() * 3.2, word: WORDS[(Math.random() * WORDS.length) | 0], rows: 2 + ((Math.random() * 4) | 0),
    });
    const items = Array.from({ length: 16 }, () => spawn(2 + Math.random() * 58));
    let last = 0;
    return {
      ready: Promise.resolve(),
      draw(g, t) {
        const dt = Math.min(0.1, Math.max(0, t - last)); last = t;
        const F = 140, cx = W / 2 + Math.sin(t * 0.3) * 12, cy = H * 0.44;
        g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
        g.strokeStyle = "rgba(255,255,255,0.3)"; g.lineWidth = 1;
        g.beginPath();
        for (let i = -9; i <= 9; i++) { g.moveTo(cx + (i * 2 * F) / 2, cy + (3.5 * F) / 2); g.lineTo(cx + (i * 2 * F) / 60, cy + (3.5 * F) / 60); }
        for (let z = 2 - ((t * 7) % 2); z < 60; z += 2) { const y = cy + (3.5 * F) / z; g.moveTo(0, y); g.lineTo(W, y); }
        g.stroke();
        for (const it of items) { it.z -= dt * 7; if (it.z < 1.1) Object.assign(it, spawn(60)); }
        items.sort((a, b) => b.z - a.z);
        for (const it of items) {
          const s = F / it.z;
          const x0 = cx + (it.x - it.w / 2) * s, x1 = cx + (it.x + it.w / 2) * s;
          const y0 = cy - (it.y + it.h) * s, y1 = cy - it.y * s;
          const a = Math.min(1, (60 - it.z) / 18) * Math.min(1, (it.z - 1.1) / 2.5);
          g.globalAlpha = a * 0.32; g.fillStyle = "#fff"; g.fillRect(x0, y0, x1 - x0, y1 - y0);
          g.globalAlpha = a; g.strokeStyle = "#fff"; g.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0, y1 - y0);
          const fs = ((y1 - y0) / (it.rows + 1)) * 0.78;
          if (fs >= 5) {
            g.save();
            g.beginPath(); g.rect(x0, y0, x1 - x0, y1 - y0); g.clip();
            g.font = `bold ${fs.toFixed(1)}px ui-monospace, Menlo, monospace`;
            g.fillStyle = "#fff";
            for (let r = 0; r < it.rows; r++) g.fillText(r ? it.word.split("").reverse().join("").slice(0, 3 + r * 2) + " ░░" : it.word + " ▸", x0 + fs * 0.5, y0 + ((r + 1) * (y1 - y0)) / (it.rows + 1) + fs * 0.35);
            g.restore();
          }
        }
        g.globalAlpha = 1;
      },
    };
  }

  // PRESENCE: someone in a scanner. Rings sweep, a line reads down the
  // figure, and a box settles on the face: only a face counts as a person.
  function scan() {
    const dots = Array.from({ length: 140 }, () => ({ a: Math.random() * Math.PI * 2, r: 50 + Math.random() * 70, y: 20 + Math.random() * 150, s: 0.2 + Math.random() * 0.6 }));
    function figure(g) {
      g.beginPath();
      g.ellipse(140, 74, 21, 26, 0, 0, Math.PI * 2);
      g.moveTo(58, H);
      g.bezierCurveTo(66, 132, 104, 120, 128, 112);
      g.lineTo(130, 98); g.lineTo(150, 98); g.lineTo(152, 112);
      g.bezierCurveTo(176, 120, 214, 132, 222, H);
      g.closePath();
    }
    return {
      ready: Promise.resolve(),
      draw(g, t) {
        g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
        for (const d of dots) {
          const a = d.a + t * d.s, z = Math.sin(a);
          g.fillStyle = `rgba(255,255,255,${0.25 + 0.45 * (z * 0.5 + 0.5)})`;
          g.fillRect(140 + Math.cos(a) * d.r, d.y, 1, 1);
        }
        g.save(); figure(g); g.clip();
        g.fillStyle = "rgba(255,255,255,0.28)"; g.fillRect(0, 0, W, H);
        g.fillStyle = "rgba(255,255,255,0.18)";
        for (let y = 0; y < H; y += 3) g.fillRect(0, y, W, 1);
        const sy = 40 + ((t * 45) % 150);
        g.fillStyle = "rgba(255,255,255,0.85)"; g.fillRect(0, sy, W, 2);
        g.restore();
        g.strokeStyle = "rgba(255,255,255,0.8)"; g.lineWidth = 1;
        figure(g); g.stroke();
        g.setLineDash([7, 4]);
        [[58, 0.9, 1], [112, 1.2, -1], [150, 0.8, 1]].forEach(([y, rx, dir]) => {
          g.lineDashOffset = t * 24 * dir;
          g.beginPath(); g.ellipse(140, y, 96 * rx, 12, 0, 0, Math.PI * 2); g.stroke();
        });
        g.setLineDash([]);
        const lock = Math.min(1, Math.max(0, (Math.sin(t * 0.8) + 0.3)));
        const pad = 6 + (1 - lock) * 16;
        g.strokeStyle = "#fff"; g.lineWidth = 2;
        const x0 = 140 - 22 - pad, y0 = 74 - 27 - pad, x1 = 140 + 22 + pad, y1 = 74 + 27 + pad, k = 8;
        g.beginPath();
        g.moveTo(x0, y0 + k); g.lineTo(x0, y0); g.lineTo(x0 + k, y0);
        g.moveTo(x1 - k, y0); g.lineTo(x1, y0); g.lineTo(x1, y0 + k);
        g.moveTo(x1, y1 - k); g.lineTo(x1, y1); g.lineTo(x1 - k, y1);
        g.moveTo(x0 + k, y1); g.lineTo(x0, y1); g.lineTo(x0, y1 - k);
        g.stroke();
      },
    };
  }

  // COMMIT: a tower you ride down, a floor per task. The readout is the
  // number of floors left; the ground floor is done.
  function tower() {
    const FOOT = [[-1, -1], [1, -1], [1, 1], [0.3, 1], [0, 0.4], [-0.3, 1], [-1, 1]];
    const FLOORS = 22, FH = 0.3, TOP = FLOORS * FH;
    const D = 12, CAMY = 9.5, P = 0.6, COS = Math.cos(P), SIN = Math.sin(P), F = 150, CX = 112, CY = 70;
    function proj(x, y, z, th) {
      const c = Math.cos(th), s = Math.sin(th);
      const X = x * c - z * s, Z = x * s + z * c + D, Y = y - CAMY;
      const y2 = Y * COS + Z * SIN, z2 = Z * COS - Y * SIN;
      return [CX + (F * X) / z2, CY - (F * y2) / z2];
    }
    let shown = 0;
    return {
      ready: Promise.resolve(),
      draw(g, t) {
        shown += (api.progress - shown) * 0.08;
        const th = 0.55 + Math.sin(t * 0.25) * 0.5;
        const lit = Math.round((1 - shown) * (FLOORS - 1));
        g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
        g.strokeStyle = "rgba(255,255,255,0.32)"; g.lineWidth = 1;
        g.beginPath();
        for (let i = -5; i <= 5; i++) {
          let a = proj(i * 0.9, 0, -4.5, th), b = proj(i * 0.9, 0, 4.5, th); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]);
          a = proj(-4.5, 0, i * 0.9, th); b = proj(4.5, 0, i * 0.9, th); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]);
        }
        g.stroke();
        for (let f = 0; f <= FLOORS; f++) {
          g.strokeStyle = f === lit ? "#fff" : `rgba(255,255,255,${0.35 + 0.35 * (f / FLOORS)})`;
          g.lineWidth = f === lit ? 2 : 1;
          g.beginPath();
          FOOT.forEach(([x, z], i) => { const p = proj(x, f * FH, z, th); i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]); });
          g.closePath(); g.stroke();
        }
        g.strokeStyle = "rgba(255,255,255,0.6)"; g.lineWidth = 1;
        g.beginPath();
        for (const [x, z] of FOOT) { const a = proj(x, 0, z, th), b = proj(x, TOP, z, th); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); }
        g.stroke();
        g.setLineDash([3, 3]); g.lineDashOffset = -t * 18;
        g.beginPath();
        for (const dx of [-0.12, 0.12]) { const a = proj(dx, 0, -0.3, th), b = proj(dx, TOP + 0.4, -0.3, th); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); }
        g.stroke(); g.setLineDash([]);
        const c0 = proj(-0.12, lit * FH, -0.3, th), c1 = proj(0.12, lit * FH + FH * 1.2, -0.3, th);
        g.fillStyle = "#fff";
        g.fillRect(Math.min(c0[0], c1[0]) - 1, Math.min(c0[1], c1[1]), Math.abs(c1[0] - c0[0]) + 3, Math.abs(c1[1] - c0[1]) + 1);
        let far = null;
        for (const [x, z] of FOOT) { const p = proj(x, lit * FH, z, th); if (!far || p[0] > far[0]) far = p; }
        g.strokeStyle = "#fff";
        g.beginPath(); g.moveTo(far[0] + 3, far[1]); g.lineTo(206, far[1]); g.stroke();
        g.font = "bold 30px ui-monospace, Menlo, monospace"; g.textBaseline = "middle";
        g.fillText(String(Math.round((1 - shown) * 7)).padStart(2, "0"), 212, far[1] + 1);
        g.textBaseline = "alphabetic";
      },
    };
  }

  const SCENES = {
    voice: still("art/voice.png"),
    events: slabs(),
    brain: still("art/brain.png"),
    jev: still("art/jev.png"),
    presence: scan(),
    phone: still("art/phone.png"),
    commit: tower(),
  };
  const ORDER = ["voice", "events", "brain", "jev", "presence", "phone", "commit"];

  // ── The monitor and the cutscene share one picture ──────────────────

  const src = document.createElement("canvas");
  src.width = W; src.height = H;
  const sctx = src.getContext("2d", { willReadFrequently: true });

  const monitor = document.createElement("div");
  monitor.className = "monitor off";
  monitor.innerHTML = `<canvas width="${W}" height="${H}" aria-hidden="true"></canvas><div class="cap"><span class="ch"></span><span class="nm"></span></div>`;
  const mctx = monitor.querySelector("canvas").getContext("2d");
  const out = mctx.createImageData(W, H);

  const cut = document.createElement("div");
  cut.id = "cut";
  cut.hidden = true;
  cut.innerHTML = `<canvas width="${W}" height="${H}" aria-hidden="true"></canvas><div class="card"><div class="n"></div><div class="t"><span class="c"></span><span class="m"></span><span class="main"></span></div></div>`;
  const cctx = cut.querySelector("canvas").getContext("2d");
  document.body.appendChild(cut);

  let current = null, t0 = performance.now(), lastFrame = 0, cutting = null;

  function frame(now) {
    requestAnimationFrame(frame);
    if (!current || document.hidden) return;
    if (now - lastFrame < FPS_MS) return;
    lastFrame = now;
    SCENES[current].draw(sctx, (now - t0) / 1000);
    tube(sctx, out, ramp(TINT[current]));
    mctx.putImageData(out, 0, 0);
    if (cutting) cctx.drawImage(mctx.canvas, 0, 0);
  }
  requestAnimationFrame(frame);

  function show(id) {
    const on = !!SCENES[id];
    monitor.classList.toggle("off", !on);
    if (!on) { current = null; return; }
    if (current !== id) { current = id; lastFrame = 0; }
    const n = ORDER.indexOf(id) + 1;
    monitor.querySelector(".ch").textContent = `CH ${String(n).padStart(2, "0")}`;
    monitor.querySelector(".nm").textContent = id === "jev" ? "DECIDER" : id.toUpperCase();
    if (reduce) { SCENES[id].ready.then(() => { SCENES[id].draw(sctx, 0); tube(sctx, out, ramp(TINT[id])); mctx.putImageData(out, 0, 0); }); }
  }

  const api = {
    progress: 0,
    has: (id) => !!SCENES[id],
    mount(slot, id) {
      if (slot) slot.appendChild(monitor);
      show(id);
    },
    skip() { if (cutting) cutting.finish(); },
    get cutting() { return !!cutting; },
    // About 0.9 s: the picture fills the window with the module's title,
    // then shrinks into the monitor. Resolves at the moment the pane can
    // paint underneath, so the handoff has something to land on.
    async cutscene(id, n, title) {
      if (reduce || !SCENES[id]) return;
      await SCENES[id].ready;
      show(id);
      const canvas = cut.querySelector("canvas");
      cut.querySelector(".n").textContent = `${String(n).padStart(2, "0")} / ${String(ORDER.length).padStart(2, "0")}`;
      cut.querySelectorAll(".t span").forEach((s) => (s.textContent = title));
      canvas.style.transition = "none";
      canvas.style.transform = "none";
      cut.classList.remove("out");
      cut.hidden = false;
      cctx.drawImage(mctx.canvas, 0, 0);
      return new Promise((resolve) => {
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          clearTimeout(hold);
          resolve();
          requestAnimationFrame(() => {
            const r = monitor.getBoundingClientRect();
            const ok = r.width > 0 && !monitor.classList.contains("off");
            canvas.style.transition = "";
            cut.classList.add("out");
            canvas.style.transform = ok ? `translate(${r.left}px, ${r.top}px) scale(${r.width / innerWidth}, ${r.height / innerHeight})` : "scale(0.2)";
            setTimeout(() => { cut.hidden = true; cutting = null; }, 260);
          });
        };
        cutting = { finish };
        const hold = setTimeout(finish, 650);
      });
    },
  };
  return api;
})();
