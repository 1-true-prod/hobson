// Hobson setup — the pictures. Each module has one, shown twice: full-screen
// for a moment when you first arrive (the cutscene), then as the band behind
// the screen's title for as long as you stay.
//
// Every picture goes through the same tube: a 280×185 greyscale frame,
// dithered to five tones of the module's colour, the colour the city is
// tinted for that screen. The stills are public-domain photographs
// (art/LICENSES.md); the moving ones are drawn here. The tube is what makes
// them one set, so nothing on screen is a picture unless it came through it.
// Nothing from a film, however filtered: a dithered frame is still a copy of
// that frame.
//
//   Art.has(id)                        does this screen have a picture
//   Art.cutscene(id, title)            resolves when the pane may paint
//   Art.mount(slot, id)                put the picture in the pane's band
//   Art.progress = 0…1, Art.tasks = n  COMMIT's tower follows the tasks
//   Art.flip = "up" | "down" | "unknown" | null
//                                      PHONE shows the phone as read, not the still
//   Art.skip()                         end a cutscene now (any key)
"use strict";

const Art = (() => {
  const W = 280, H = 185;
  const FPS_MS = 50; // 20 frames a second: a period picture, not a smooth one
  const BAYER = [0, 32, 8, 40, 2, 34, 10, 42, 48, 16, 56, 24, 50, 18, 58, 26, 12, 44, 4, 36, 14, 46, 6, 38, 60, 28, 52, 20, 62, 30, 54, 22, 3, 35, 11, 43, 1, 33, 9, 41, 51, 19, 59, 27, 49, 17, 57, 25, 15, 47, 7, 39, 13, 45, 5, 37, 63, 31, 55, 23, 61, 29, 53, 21];
  const TINT = window.TINTS; // city.js
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
  const mix = (a, b, k) => a.map((v, i) => Math.round(v + (b[i] - v) * k));
  function ramp(tint) {
    const t = hex(tint), bg = [2, 3, 5];
    return [bg, mix(bg, t, 0.22), mix(bg, t, 0.52), t, mix(t, [255, 255, 255], 0.62)];
  }

  // Greyscale in, five tones out. Drawn scenes take Bayer's cross-hatch,
  // which reads as intent on lines and flat fills; photographs take
  // interleaved gradient noise (Jimenez), whose finer, less regular grain
  // keeps a face from turning into a pattern.
  const IGN = new Float32Array(W * H);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const v = 52.9829189 * ((0.06711056 * x + 0.00583715 * y) % 1);
    IGN[y * W + x] = v - Math.floor(v);
  }
  function tube(src, out, pal, photo) {
    const s = src.getImageData(0, 0, W, H).data, d = out.data;
    for (let y = 0; y < H; y++) {
      const row = (y & 7) * 8;
      for (let x = 0; x < W; x++) {
        const i = (y * W + x) * 4;
        const l = (s[i] * 0.3 + s[i + 1] * 0.59 + s[i + 2] * 0.11) / 255;
        const n = photo ? IGN[y * W + x] : (BAYER[row + (x & 7)] + 0.5) / 64;
        let k = Math.floor(l * 4 + n);
        k = k < 0 ? 0 : k > 4 ? 4 : k;
        const c = pal[k];
        d[i] = c[0]; d[i + 1] = c[1]; d[i + 2] = c[2]; d[i + 3] = 255;
      }
    }
  }

  // ── Scenes: each draws greyscale into a W×H context ──────────────────

  // An anatomical plate, drifting a little, with a bright band rolling down
  // it and the module's part of the brain ringed and named, the way an
  // atlas would. The plates are inverted engravings (art/LICENSES.md): the
  // lines come out as phosphor on black, like the city.
  function still(src, at) {
    const img = new Image();
    const ready = new Promise((r) => { img.onload = img.onerror = r; });
    img.src = src;
    return {
      ready,
      photo: true,
      draw(g, t) {
        g.fillStyle = "#000";
        g.fillRect(0, 0, W, H);
        const s = 1.07, dx = Math.sin(t * 0.17) * 6 - W * (s - 1) / 2, dy = Math.cos(t * 0.13) * 3 - H * (s - 1) / 2;
        if (img.naturalWidth) g.drawImage(img, dx, dy, W * s, H * s);
        // The band only brightens lines: over bare black it dithered into a strip.
        const y = ((t * 26) % (H + 60)) - 30;
        g.globalCompositeOperation = "overlay";
        g.fillStyle = "rgba(255,255,255,0.35)";
        g.fillRect(0, y, W, 14);
        g.globalCompositeOperation = "source-over";
        callout(g, t, dx + at.x * s, dy + at.y * s, at);
      },
    };
  }

  // A ring on the region, a leader line and its name. On COMMIT the ring
  // fills as the tasks finish, and the name counts them.
  function callout(g, t, x, y, at) {
    const r = at.r + Math.sin(t * 2.4) * 1.2;
    const p = at.progress ? shown() : 0;
    g.save();
    // A black keyline first, so the ring reads over a dense engraving.
    g.strokeStyle = "#000"; g.lineWidth = 5;
    g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.stroke();
    // Two pixels at least: thinner, the dither breaks the dashes up and the ring fades out.
    g.strokeStyle = "#fff";
    g.lineWidth = 2;
    g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2);
    if (at.progress) {
      g.globalAlpha = 0.45; g.setLineDash([2, 3]); g.stroke();
      g.globalAlpha = 1; g.setLineDash([]); g.lineWidth = 3;
      g.beginPath(); g.arc(x, y, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * p);
    } else {
      g.setLineDash([7, 4]); g.lineDashOffset = -t * 12;
    }
    g.stroke();
    g.setLineDash([]); g.lineWidth = 1;
    const label = at.progress && api.tasks ? `${at.label} ${Math.round(p * api.tasks)}/${api.tasks}` : at.label;
    g.font = "bold 10px ui-monospace, Menlo, monospace";
    const w = g.measureText(label).width + 6;
    // The label points toward the middle, the one part of the band the fade leaves opaque.
    // Decided from the ring's own position, not the drifting one, so it never flips mid-play.
    const side = at.side || (at.x < W / 2 ? 1 : -1);
    const up = at.y > H / 2;
    const ex = x + side * r * 0.7, ey = y + (up ? -1 : 1) * r * 0.7;
    const lx = x + side * (r + 16), ly = up ? y - r - 12 : y + r + 20;
    const leader = () => { g.beginPath(); g.moveTo(ex, ey); g.lineTo(lx, ly); g.lineTo(lx + side * w, ly); g.stroke(); };
    g.strokeStyle = "#000"; g.lineWidth = 4; leader();
    g.strokeStyle = "#fff"; g.lineWidth = 1.5; leader();
    const tx = side > 0 ? lx : lx - w;
    g.fillStyle = "#000"; g.fillRect(tx, ly - 12, w, 11);
    g.fillStyle = "#fff"; g.fillText(label, tx + 3, ly - 3);
    g.restore();
  }
  let eased = 0;
  const shown = () => (eased += (api.progress - eased) * 0.08);

  // PHONE, once a phone is picked: the phone as the switch reads it, seen
  // from above. Face up it shows its screen and a lit lens; face down, its
  // back and a closed lens; unread, an outline and a question mark.
  function flip(photo) {
    function body(g, x, y, w, h, r) {
      g.beginPath();
      g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
      g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
    }
    return {
      ready: photo.ready,
      get photo() { return !api.flip; },
      draw(g, t) {
        if (!api.flip) return photo.draw(g, t);
        const pos = api.flip;
        g.fillStyle = "#000"; g.fillRect(0, 0, W, H);
        g.strokeStyle = "rgba(255,255,255,0.14)"; g.lineWidth = 1;
        g.beginPath();
        for (let x = 0; x < W; x += 14) { g.moveTo(x, 0); g.lineTo(x, H); }
        for (let y = 0; y < H; y += 14) { g.moveTo(0, y); g.lineTo(W, y); }
        g.stroke();
        const w = 74, h = 150, x = 70, y = (H - h) / 2;
        g.save();
        g.translate(x + w / 2, y + h / 2); g.rotate(-0.18); g.translate(-(x + w / 2), -(y + h / 2));
        body(g, x, y, w, h, 12);
        g.fillStyle = pos === "up" ? "rgba(255,255,255,0.22)" : pos === "down" ? "rgba(255,255,255,0.5)" : "rgba(255,255,255,0.06)";
        g.fill();
        g.strokeStyle = "#fff"; g.lineWidth = 2; g.stroke();
        if (pos === "up") {
          body(g, x + 7, y + 22, w - 14, h - 40, 4);
          g.fillStyle = "rgba(255,255,255,0.12)"; g.fill();
          for (let r = 0; r < 6; r++) { g.fillStyle = "rgba(255,255,255,0.5)"; g.fillRect(x + 14, y + 34 + r * 16, (w - 28) * (0.4 + ((r * 37) % 60) / 100), 3); }
          g.beginPath(); g.arc(x + w / 2, y + 11, 3.5 + Math.sin(t * 5) * 0.8, 0, Math.PI * 2); g.fillStyle = "#fff"; g.fill();
        } else if (pos === "down") {
          body(g, x + 10, y + 10, 26, 34, 6);
          g.fillStyle = "rgba(0,0,0,0.7)"; g.fill(); g.strokeStyle = "rgba(255,255,255,0.7)"; g.lineWidth = 1.5; g.stroke();
          g.beginPath(); g.moveTo(x + 14, y + 14); g.lineTo(x + 32, y + 40); g.stroke();
        } else {
          g.font = "bold 44px ui-monospace, Menlo, monospace"; g.fillStyle = "rgba(255,255,255,0.7)";
          g.textAlign = "center"; g.fillText("?", x + w / 2, y + h / 2 + 15); g.textAlign = "start";
        }
        g.restore();
        g.font = "bold 22px ui-monospace, Menlo, monospace"; g.fillStyle = "#fff";
        g.fillText(pos === "up" ? "CAM ON" : pos === "down" ? "CAM OFF" : "NO READ", 168, H / 2 + 2);
        g.fillStyle = "rgba(255,255,255,0.55)"; g.font = "12px ui-monospace, Menlo, monospace";
        g.fillText(pos === "up" ? "face up" : pos === "down" ? "face down" : "counts as off", 168, H / 2 + 22);
      },
    };
  }

  // Each module is the part of the brain that does its job.
  const SCENES = {
    voice: still("art/broca.png", { x: 105, y: 112, r: 16, label: "BROCA'S AREA" }),
    events: still("art/reflex.png", { x: 160, y: 53, r: 12, label: "REFLEX ARC" }),
    brain: still("art/cortex.png", { x: 135, y: 50, r: 22, label: "CORTEX" }),
    jev: still("art/striatum.png", { x: 183, y: 81, r: 20, label: "STRIATUM" }),
    presence: still("art/vision.png", { x: 178, y: 82, r: 16, label: "VISUAL PATHWAY" }),
    phone: flip(still("art/cerebellum.png", { x: 130, y: 95, r: 26, label: "CEREBELLUM" })),
    commit: still("art/hippocampus.png", { x: 157, y: 106, r: 18, label: "HIPPOCAMPUS", progress: true }),
  };
  // ── The band and the cutscene share one picture ─────────────────────

  const src = document.createElement("canvas");
  src.width = W; src.height = H;
  const sctx = src.getContext("2d", { willReadFrequently: true });

  const band = document.createElement("div");
  band.className = "band-art";
  band.innerHTML = `<canvas width="${W}" height="${H}" aria-hidden="true"></canvas>`;
  const mctx = band.querySelector("canvas").getContext("2d");
  const out = mctx.createImageData(W, H);

  const cut = document.createElement("div");
  cut.id = "cut";
  cut.hidden = true;
  cut.innerHTML = `<canvas width="${W}" height="${H}" aria-hidden="true"></canvas><div class="card"><div class="t"><span class="c"></span><span class="m"></span><span class="main"></span></div></div>`;
  const cctx = cut.querySelector("canvas").getContext("2d");
  document.body.appendChild(cut);

  let current = null, t0 = performance.now(), lastFrame = 0, cutting = null;

  function frame(now) {
    requestAnimationFrame(frame);
    if (!current || document.hidden) return;
    if (now - lastFrame < FPS_MS) return;
    lastFrame = now;
    SCENES[current].draw(sctx, (now - t0) / 1000);
    tube(sctx, out, ramp(TINT[current]), SCENES[current].photo);
    mctx.putImageData(out, 0, 0);
    if (cutting) cctx.drawImage(mctx.canvas, 0, 0);
  }
  requestAnimationFrame(frame);

  function still1(id) {
    SCENES[id].ready.then(() => { SCENES[id].draw(sctx, 0); tube(sctx, out, ramp(TINT[id]), SCENES[id].photo); mctx.putImageData(out, 0, 0); });
  }
  function show(id) {
    if (!SCENES[id]) { current = null; return; }
    if (current !== id) { current = id; lastFrame = 0; }
    if (reduce) still1(id);
  }

  const api = {
    progress: 0,
    tasks: 0,
    _flip: null,
    get flip() { return this._flip; },
    set flip(v) { this._flip = v; if (reduce && current === "phone") still1("phone"); },
    has: (id) => !!SCENES[id],
    mount(slot, id) {
      if (slot && SCENES[id]) slot.appendChild(band);
      show(id);
    },
    skip() { if (cutting) cutting.finish(); },
    get cutting() { return !!cutting; },
    // About 0.9 s: the picture fills the window with the module's title,
    // then shrinks into the pane's band. Resolves at the moment the pane can
    // paint underneath, so the handoff has something to land on.
    async cutscene(id, title) {
      if (reduce || !SCENES[id]) return;
      await SCENES[id].ready;
      show(id);
      const canvas = cut.querySelector("canvas");
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
            const r = band.getBoundingClientRect();
            const ok = r.width > 0 && band.isConnected;
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
