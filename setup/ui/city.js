// The city behind the wizard: a slow flight down a street of wireframe data
// towers, some with glyphs streaming up their faces. A 2D canvas with its
// own perspective projection, 30 frames a second, paused when hidden.
//
//   City.mode("warp" | "cruise" | "idle")   how fast the flight goes
//   City.tint("#19e6ff")                    a new colour, arriving down the street from the horizon
//   City.tint(null, { full: true })         every colour: the hero and the finale
//   City.burst()                            a moment of colour cycling
//   City.fly(1 | -1)                        a surge forward, or back, between modules
//   City.arrive(plan)                       the next module coming up the street to its pane
//   City.vp()                               the vanishing point, in page pixels
//
// TINTS is the one list of module colours; art.js and wizard.js read it.
"use strict";

window.TINTS = { voice: "#19e6ff", events: "#39ff88", brain: "#8c6bff", jev: "#ffc640", presence: "#ff2bd6", phone: "#19e6ff", commit: "#39ff88", loadout: "#19e6ff" };

(function () {
  const canvas = document.getElementById("city");
  const ctx = canvas.getContext("2d");
  const PALETTE = ["#19e6ff", "#ff2bd6", "#39ff88", "#8c6bff", "#19e6ff", "#ffc640"];
  const GLYPHS = "0123456789ABCDEF<>/\\|=+*#$%&@";
  const FAR = 1800;
  const STREET = 110;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let W = 0, H = 0, DPR = 1, F = 0, HORIZON = 0;
  let speed = 420, target = 420;
  // A surge between modules, on top of the cruise, gone in about half a second.
  let boost = 0, travelled = 0;
  const BOOST = 2600, BOOST_DECAY = 0.2;
  let lead = "#19e6ff";
  let full = true;
  const rgb = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)).join(",");
  // One screen, one light: the lead colour, or its dim tone.
  const hue = () => (full ? (Math.random() < 0.4 ? lead : PALETTE[(Math.random() * PALETTE.length) | 0]) : Math.random() < 0.6 ? lead : dim);
  let dim = lead;
  function dimOf(c) {
    const v = [1, 3, 5].map((i) => Math.round(parseInt(c.slice(i, i + 2), 16) * 0.55));
    return "#" + v.map((x) => x.toString(16).padStart(2, "0")).join("");
  }
  // A new colour starts at the horizon and runs down the street towards the
  // camera: nearer than `sweep`, a thing keeps the colour it had (`c0`).
  const SWEEP_SPEED = 4200;
  let sweep = -1, was = null;
  const col = (o) => (sweep > 0 && o.z < sweep && o.c0 ? o.c0 : o.c);
  let towers = [];
  let packets = [];
  let t = 0;
  let last = performance.now();
  let lastDraw = 0;

  const rand = (a, b) => a + Math.random() * (b - a);

  function spawn(z) {
    const side = Math.random() < 0.5 ? -1 : 1;
    const w = rand(50, 130);
    const d = rand(50, 140);
    const x = side * (STREET + rand(0, 620)) + (side < 0 ? -w : 0);
    const tall = Math.random() < 0.18;
    const h = tall ? rand(520, 1100) : rand(90, 460);
    return { x, z, w, d, h, c: hue(), c0: null, glyphs: Math.random() < 0.38, seed: Math.random() * 1000, floor: rand(22, 34) };
  }

  function reset() {
    towers = [];
    for (let i = 0; i < 64; i++) towers.push(spawn(rand(40, FAR)));
    packets = [];
    for (let i = 0; i < 26; i++) packets.push({ x: (Math.random() < 0.5 ? -1 : 1) * rand(0, STREET - 10), z: rand(0, FAR), c: full ? PALETTE[i % 3] : lead, c0: null });
  }

  function resize() {
    DPR = Math.min(2, window.devicePixelRatio || 1);
    W = canvas.clientWidth;
    H = canvas.clientHeight;
    canvas.width = Math.round(W * DPR);
    canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    F = H * 0.95;
    HORIZON = H * 0.46;
  }

  // Camera: low over the street, swaying a little.
  let camX = 0, camY = 70;
  const px = (x, z) => W / 2 + ((x - camX) * F) / z;
  const py = (y, z) => HORIZON - ((y - camY) * F) / z;

  function alphaFor(z) {
    const far = 1 - z / FAR;
    const near = Math.min(1, (z - 20) / 340); // close towers fade before they fill the screen
    return Math.max(0, Math.min(far * 1.1, near));
  }

  function drawGround() {
    ctx.lineWidth = 1;
    // Lines running away down the street: the stretch nearer than the sweep
    // still in the old colour.
    const near = sweep > 0 ? Math.min(FAR, Math.max(30, sweep)) : 30;
    const runs = (c, z0, z1) => {
      for (let i = -12; i <= 12; i++) {
        const x = i * 70;
        const a = 0.16 - Math.abs(i) * 0.008;
        if (a <= 0) continue;
        ctx.strokeStyle = `rgba(${c},${a})`;
        ctx.beginPath();
        ctx.moveTo(px(x, z0), py(0, z0));
        ctx.lineTo(px(x, z1), py(0, z1));
        ctx.stroke();
      }
    };
    if (near > 30) runs(was.full ? "25,230,255" : rgb(was.lead), 30, near);
    if (near < FAR) runs(full ? "25,230,255" : rgb(lead), near, FAR);
    // Cross lines, moving towards us.
    const step = 90;
    const off = ((travelled % step) + step) % step;
    for (let z = step - off; z < FAR; z += step) {
      if (z < 30) continue;
      const a = 0.14 * alphaFor(z);
      const old = sweep > 0 && z < sweep;
      ctx.strokeStyle = `rgba(${(old ? was.full : full) ? "255,43,214" : rgb(old ? was.dim : dim)},${a})`;
      ctx.beginPath();
      ctx.moveTo(px(-900, z), py(0, z));
      ctx.lineTo(px(900, z), py(0, z));
      ctx.stroke();
    }
    // Packets of light running down the street.
    for (const p of packets) {
      if (p.z < 30) continue;
      const a = alphaFor(p.z);
      const r = Math.min(5, Math.max(1, (3 * F) / p.z));
      const c = col(p);
      ctx.fillStyle = c;
      ctx.globalAlpha = a;
      ctx.fillRect(px(p.x, p.z) - r / 2, py(0, p.z) - r / 2, r, r);
      ctx.globalAlpha = a * 0.35;
      const tail = Math.min(p.z + 60, FAR);
      ctx.strokeStyle = c;
      ctx.beginPath();
      ctx.moveTo(px(p.x, p.z), py(0, p.z));
      ctx.lineTo(px(p.x, tail), py(0, tail));
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
  }

  function drawTower(b) {
    const z0 = b.z, z1 = b.z + b.d;
    if (z0 < 22) return;
    const a = alphaFor(z0);
    if (a <= 0.01) return;
    const c = col(b);
    const xl = b.x, xr = b.x + b.w;
    // Front face.
    const fx0 = px(xl, z0), fx1 = px(xr, z0);
    const fy0 = py(0, z0), fy1 = py(b.h, z0);
    // The side facing the street (the one the camera can see).
    const inner = xr < camX ? xr : xl > camX ? xl : null;
    ctx.globalAlpha = a;

    if (inner !== null) {
      const sx0 = px(inner, z0), sx1 = px(inner, z1);
      ctx.fillStyle = "rgba(2,4,8,0.88)";
      ctx.beginPath();
      ctx.moveTo(sx0, py(0, z0));
      ctx.lineTo(sx1, py(0, z1));
      ctx.lineTo(sx1, py(b.h, z1));
      ctx.lineTo(sx0, py(b.h, z0));
      ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = c;
      ctx.lineWidth = 0.6;
      ctx.globalAlpha = a * 0.45;
      ctx.beginPath();
      for (let y = 0; y <= b.h; y += b.floor) {
        ctx.moveTo(sx0, py(y, z0));
        ctx.lineTo(sx1, py(y, z1));
      }
      ctx.stroke();
      ctx.globalAlpha = a;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(sx1, py(0, z1));
      ctx.lineTo(sx1, py(b.h, z1));
      ctx.lineTo(sx0, py(b.h, z0));
      ctx.stroke();
    }

    ctx.fillStyle = "rgba(2,5,9,0.9)";
    ctx.fillRect(fx0, fy1, fx1 - fx0, fy0 - fy1);
    ctx.strokeStyle = c;
    ctx.globalAlpha = a * 0.5;
    ctx.lineWidth = 0.6;
    ctx.beginPath();
    for (let y = b.floor; y < b.h; y += b.floor) {
      const yy = py(y, z0);
      ctx.moveTo(fx0, yy);
      ctx.lineTo(fx1, yy);
    }
    const cols = Math.max(2, Math.round(b.w / 26));
    for (let i = 1; i < cols; i++) {
      const xx = fx0 + ((fx1 - fx0) * i) / cols;
      ctx.moveTo(xx, fy0);
      ctx.lineTo(xx, fy1);
    }
    ctx.stroke();
    ctx.globalAlpha = a;
    ctx.lineWidth = 1.2;
    ctx.shadowColor = c;
    ctx.shadowBlur = z0 < 500 ? 8 : 0;
    ctx.strokeRect(fx0, fy1, fx1 - fx0, fy0 - fy1);
    ctx.shadowBlur = 0;

    // Glyphs streaming up the face.
    if (b.glyphs && z0 < 900) {
      const size = Math.max(5, (13 * F) / z0 / 1.6);
      if (size > 5.5) {
        ctx.font = `${size.toFixed(1)}px ui-monospace, Menlo, monospace`;
        ctx.fillStyle = c;
        const colsN = Math.min(6, Math.floor((fx1 - fx0) / (size * 1.4)));
        const rowsN = Math.min(14, Math.floor((fy0 - fy1) / (size * 1.25)));
        for (let c = 0; c < colsN; c++) {
          const cx = fx0 + (c + 0.5) * ((fx1 - fx0) / colsN) - size * 0.3;
          const run = (t * (1.2 + ((b.seed + c * 7) % 1.5)) + c * 3.1) % (rowsN + 6);
          for (let r = 0; r < rowsN; r++) {
            const dist = run - r;
            if (dist < 0 || dist > 6) continue;
            ctx.globalAlpha = a * (1 - dist / 6) * 0.95;
            const ch = GLYPHS[(Math.floor(b.seed + c * 13 + r * 7 + t * 4) % GLYPHS.length + GLYPHS.length) % GLYPHS.length];
            ctx.fillText(ch, cx, fy0 - (r + 0.6) * size * 1.25);
          }
        }
      }
    }
    ctx.globalAlpha = 1;
  }

  function frame(now) {
    requestAnimationFrame(frame);
    if (document.hidden) { last = now; return; }
    if (now - lastDraw < 32) return; // ~30 fps is plenty for a backdrop
    lastDraw = now;
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    t += dt;
    speed += (target - speed) * Math.min(1, dt * 1.6);
    boost *= Math.exp(-dt / BOOST_DECAY);
    const v = speed + boost;
    travelled += v * dt;
    camX = Math.sin(t * 0.13) * 38;
    camY = 70 + Math.sin(t * 0.21) * 10;
    if (sweep > 0) {
      sweep -= SWEEP_SPEED * dt;
      if (sweep <= 0) { sweep = -1; was = null; }
    }

    for (const b of towers) {
      b.z -= v * dt;
      // Flying back, towers leave behind the horizon and come in again close
      // by, where the near fade hides them until they have moved off.
      if (b.z + b.d < 22) Object.assign(b, spawn(FAR + rand(0, 200)));
      else if (b.z > FAR + 260) Object.assign(b, spawn(rand(24, 90)));
    }
    for (const p of packets) {
      p.z -= v * dt * 2.2;
      if (p.z < 20 || p.z > FAR + 60) { p.z = p.z < 20 ? FAR : 30; p.x = (Math.random() < 0.5 ? -1 : 1) * rand(0, STREET - 10); }
    }

    ctx.clearRect(0, 0, W, H);
    // Sky: a faint glow at the horizon, where a new colour arrives first.
    const g = ctx.createLinearGradient(0, HORIZON - H * 0.3, 0, HORIZON + 30);
    g.addColorStop(0, "rgba(0,0,0,0)");
    g.addColorStop(1, `rgba(${full ? "140,107,255" : rgb(lead)},0.10)`);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, HORIZON + 30);

    drawGround();
    towers.sort((a, b) => b.z - a.z);
    for (const b of towers) drawTower(b);
  }

  // ── Arriving at a module ────────────────────────────────────────────
  // The next screen comes up the street as a wireframe of itself, from the
  // vanishing point to its pane: its outline, a rule where each row will
  // sit, its title in outline like a sign, and its picture, coarse while it
  // is far off and sharper as it nears. It is a thing in the city, so it
  // scales as 1/z: far away for most of the trip, rushing in at the end.
  // Once there it cools, the outline and rules fading into the pane's own
  // while the screen fills in underneath.
  const fly = document.getElementById("fly");
  const fctx = fly && fly.getContext("2d");
  const pic = document.createElement("canvas");
  const pctx = pic.getContext("2d");
  let arriving = null;
  const DEPTH = 11; // how much farther the next module starts than where it stops
  const easeOut = (k) => 1 - Math.pow(1 - k, 3);

  // The band's own vignette (wizard.css .band-art), so the picture lands as it will look.
  function vignetted(src) {
    if (pic.width !== src.width) { pic.width = src.width; pic.height = src.height; }
    const w = pic.width, h = pic.height;
    pctx.globalCompositeOperation = "copy";
    pctx.drawImage(src, 0, 0);
    pctx.globalCompositeOperation = "destination-in";
    pctx.save();
    pctx.translate(w / 2, h / 2);
    pctx.scale(w / 2, h / 2);
    const g = pctx.createRadialGradient(0, 0, 0, 0, 0, 1);
    g.addColorStop(0.45, "#000");
    g.addColorStop(1, "rgba(0,0,0,0)");
    pctx.fillStyle = g;
    pctx.fillRect(-1, -1, 2, 2);
    pctx.restore();
    pctx.globalCompositeOperation = "source-over";
    return pic;
  }

  function arriveFrame(now, a) {
    if (a !== arriving) return; // a later arrival took over
    const k = Math.min(1, Math.max(0, (now - a.t0) / a.ms));
    const cool = a.landed ? Math.min(1, (now - a.landed) / a.coolMs) : 0;
    const w = innerWidth, h = innerHeight;
    if (fly.width !== Math.round(w * DPR) || fly.height !== Math.round(h * DPR)) { fly.width = Math.round(w * DPR); fly.height = Math.round(h * DPR); }
    fctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    fctx.clearRect(0, 0, w, h);
    const s = 1 / (1 + (DEPTH - 1) * (1 - easeOut(k)));
    if (a.frame && !a.landed) a.frame(s);
    if (now >= a.t0 && cool < 1) {
      // Measured every frame: a pane that changes on the way (COMMIT's plan
      // arriving) is still what lands.
      const m = a.measure();
      const vp = api.vp();
      const X = (x) => vp.x + (x - vp.x) * s, Y = (y) => vp.y + (y - vp.y) * s;
      const r = m.to;
      const x0 = X(r.x), y0 = Y(r.y), x1 = X(r.x + r.w), y1 = Y(r.y + r.h);
      // Out of the haze at the horizon, the way a tower is.
      const alpha = Math.min(1, k * 5) * (1 - cool);
      fctx.globalAlpha = alpha;
      if (!a.landed) {
        fctx.fillStyle = "rgba(2,5,9,0.9)";
        fctx.fillRect(x0, y0, x1 - x0, y1 - y0);
        if (m.picture) {
          const p = m.picture;
          fctx.imageSmoothingEnabled = false;
          fctx.drawImage(vignetted(p.canvas), X(r.x + p.x), Y(r.y + p.y), p.w * s, p.h * s);
          fctx.imageSmoothingEnabled = true;
        }
      }
      fctx.strokeStyle = a.color;
      fctx.lineWidth = 1;
      fctx.beginPath();
      for (const rule of m.rules) {
        const yy = Math.round(Y(r.y + rule.y)) + 0.5;
        fctx.moveTo(X(r.x + rule.x), yy);
        fctx.lineTo(X(r.x + rule.x + rule.w), yy);
      }
      fctx.globalAlpha = alpha * 0.55;
      fctx.stroke();
      fctx.globalAlpha = alpha;
      fctx.shadowColor = a.color;
      fctx.shadowBlur = 10;
      if (m.title) {
        const tt = m.title;
        fctx.font = `${(tt.size * s).toFixed(2)}px ${tt.font}`;
        fctx.lineWidth = 1.2;
        fctx.strokeText(tt.text, X(r.x + tt.x), Y(r.y + tt.y));
      }
      fctx.lineWidth = 1.2;
      fctx.strokeRect(Math.round(x0) + 0.5, Math.round(y0) + 0.5, Math.round(x1 - x0) - 1, Math.round(y1 - y0) - 1);
      fctx.shadowBlur = 0;
      fctx.globalAlpha = 1;
    }
    if (k >= 1 && !a.landed) { a.landed = now; if (a.frame) a.frame(1); a.done(); }
    if (cool >= 1) { arriving = null; fctx.clearRect(0, 0, w, h); return; }
    requestAnimationFrame((t) => arriveFrame(t, a));
  }

  window.City = {
    mode(m) { target = m === "warp" ? 1500 : m === "idle" ? 90 : 300; },
    fly(dir) { if (!reduce) boost = dir * BOOST; },
    vp: () => ({ x: innerWidth / 2, y: innerHeight * 0.46 }),
    // plan: { measure() -> { to: {x, y, w, h}, and relative to it rules: [{x, y, w}],
    //         title: {text, x, y (baseline), size, font}, picture: {canvas, x, y, w, h} },
    //         color, delay, ms, coolMs, frame(scale) }. Resolves the moment it is in place.
    arrive(plan) {
      if (arriving) { arriving.done(); arriving = null; }
      if (reduce || !fctx) return Promise.resolve();
      return new Promise((done) => {
        const a = { coolMs: 260, ...plan, t0: performance.now() + (plan.delay || 0), landed: 0, done };
        arriving = a;
        requestAnimationFrame((t) => arriveFrame(t, a));
      });
    },
    // A key while it is on its way: in place now.
    land() { if (arriving) arriving.t0 = performance.now() - arriving.ms; },
    // Recoloured from the horizon in: waiting for towers to respawn left the
    // old colours on screen for most of a module.
    tint(c, opts) {
      const fresh = sweep <= 0;
      for (const b of towers) b.c0 = col(b);
      for (const p of packets) p.c0 = col(p);
      if (fresh) was = { lead, dim, full };
      full = !!(opts && opts.full);
      if (c) lead = c;
      dim = dimOf(lead);
      for (const b of towers) b.c = hue();
      packets.forEach((p, i) => { p.c = full ? PALETTE[i % 3] : lead; });
      sweep = reduce ? -1 : FAR + 300;
      if (reduce) was = null;
    },
    burst() {
      document.body.classList.remove("hack");
      void document.body.offsetWidth;
      document.body.classList.add("hack");
      target = 2200;
      setTimeout(() => { document.body.classList.remove("hack"); target = 300; }, 3600);
    },
  };
  const api = window.City;

  window.addEventListener("resize", resize);
  resize();
  reset();
  // Start mid-flight rather than with every tower in the same place.
  for (let i = 0; i < 40; i++) { for (const b of towers) b.z -= 8; }
  requestAnimationFrame(frame);
})();
