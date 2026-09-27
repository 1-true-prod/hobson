// The city behind the wizard: a slow flight down a street of wireframe data
// towers, some with glyphs streaming up their faces. A 2D canvas with its
// own perspective projection, 30 frames a second, paused when hidden.
//
//   City.mode("warp" | "cruise" | "idle")   how fast the flight goes
//   City.tint("#19e6ff")                    the colour that leads the palette
//   City.burst()                            a moment of colour cycling
"use strict";

(function () {
  const canvas = document.getElementById("city");
  const ctx = canvas.getContext("2d");
  const PALETTE = ["#19e6ff", "#ff2bd6", "#39ff88", "#8c6bff", "#19e6ff", "#ffc640"];
  const GLYPHS = "0123456789ABCDEF<>/\\|=+*#$%&@";
  const FAR = 1800;
  const STREET = 110;
  let W = 0, H = 0, DPR = 1, F = 0, HORIZON = 0;
  let speed = 420, target = 420;
  let lead = "#19e6ff";
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
    const c = Math.random() < 0.4 ? lead : PALETTE[(Math.random() * PALETTE.length) | 0];
    return { x, z, w, d, h, c, glyphs: Math.random() < 0.38, seed: Math.random() * 1000, floor: rand(22, 34) };
  }

  function reset() {
    towers = [];
    for (let i = 0; i < 64; i++) towers.push(spawn(rand(40, FAR)));
    packets = [];
    for (let i = 0; i < 26; i++) packets.push({ x: (Math.random() < 0.5 ? -1 : 1) * rand(0, STREET - 10), z: rand(0, FAR), c: PALETTE[i % 3] });
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
    // Lines running away down the street.
    for (let i = -12; i <= 12; i++) {
      const x = i * 70;
      const a = 0.16 - Math.abs(i) * 0.008;
      if (a <= 0) continue;
      ctx.strokeStyle = `rgba(25,230,255,${a})`;
      ctx.beginPath();
      ctx.moveTo(px(x, 30), py(0, 30));
      ctx.lineTo(px(x, FAR), py(0, FAR));
      ctx.stroke();
    }
    // Cross lines, moving towards us.
    const step = 90;
    const off = (t * speed) % step;
    for (let z = step - off; z < FAR; z += step) {
      if (z < 30) continue;
      const a = 0.14 * alphaFor(z);
      ctx.strokeStyle = `rgba(255,43,214,${a})`;
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
      ctx.fillStyle = p.c;
      ctx.globalAlpha = a;
      ctx.fillRect(px(p.x, p.z) - r / 2, py(0, p.z) - r / 2, r, r);
      ctx.globalAlpha = a * 0.35;
      const tail = Math.min(p.z + 60, FAR);
      ctx.strokeStyle = p.c;
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
      ctx.strokeStyle = b.c;
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
    ctx.strokeStyle = b.c;
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
    ctx.shadowColor = b.c;
    ctx.shadowBlur = z0 < 500 ? 8 : 0;
    ctx.strokeRect(fx0, fy1, fx1 - fx0, fy0 - fy1);
    ctx.shadowBlur = 0;

    // Glyphs streaming up the face.
    if (b.glyphs && z0 < 900) {
      const size = Math.max(5, (13 * F) / z0 / 1.6);
      if (size > 5.5) {
        ctx.font = `${size.toFixed(1)}px ui-monospace, Menlo, monospace`;
        ctx.fillStyle = b.c;
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
    camX = Math.sin(t * 0.13) * 38;
    camY = 70 + Math.sin(t * 0.21) * 10;

    for (const b of towers) {
      b.z -= speed * dt;
      if (b.z + b.d < 22) Object.assign(b, spawn(FAR + rand(0, 200)));
    }
    for (const p of packets) {
      p.z -= speed * dt * 2.2;
      if (p.z < 20) { p.z = FAR; p.x = (Math.random() < 0.5 ? -1 : 1) * rand(0, STREET - 10); }
    }

    ctx.clearRect(0, 0, W, H);
    // Sky: a faint glow at the horizon.
    const g = ctx.createLinearGradient(0, HORIZON - H * 0.3, 0, HORIZON + 30);
    g.addColorStop(0, "rgba(0,0,0,0)");
    g.addColorStop(1, "rgba(140,107,255,0.10)");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, HORIZON + 30);

    drawGround();
    towers.sort((a, b) => b.z - a.z);
    for (const b of towers) drawTower(b);
  }

  window.City = {
    mode(m) { target = m === "warp" ? 1500 : m === "idle" ? 90 : 300; },
    tint(c) { lead = c; },
    burst() {
      document.body.classList.remove("hack");
      void document.body.offsetWidth;
      document.body.classList.add("hack");
      target = 2200;
      setTimeout(() => { document.body.classList.remove("hack"); target = 300; }, 3600);
    },
  };

  window.addEventListener("resize", resize);
  resize();
  reset();
  // Start mid-flight rather than with every tower in the same place.
  for (let i = 0; i < 40; i++) { for (const b of towers) b.z -= 8; }
  requestAnimationFrame(frame);
})();
