"""The face's page, laid out by a real browser: headless Chrome, where there is one.

WKWebView is WebKit, not Blink, but the layout at fault here is plain CSS grid
and flex, which both engines follow. A line's project is laid out the moment
it is said, so each is measured after a moment, not once revealed.
"""

import html
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

_REAL_POPEN = subprocess.Popen  # the autouse no_audio fixture replaces it
FACE = Path(__file__).resolve().parent.parent / "presence" / "face"
CHROME = next((p for p in (
    os.environ.get("HOBSON_TEST_CHROME"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome"), shutil.which("chromium"),
) if p and os.path.exists(p)), None)

pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome to lay the page out")

PROBE = """
const lines = %s;
const out = [];
const measure = (project) => {
  const c = document.querySelector("canvas"), win = document.querySelector(".win");
  const name = document.querySelector(".meta > span"), chip = document.querySelector(".chip");
  out.push({project, col: c.parentElement.clientWidth, w: c.clientWidth, h: c.clientHeight,
            win: win.offsetWidth, winH: win.offsetHeight,
            lines: Math.round(name.clientHeight / parseFloat(getComputedStyle(name).lineHeight)),
            cut: name.scrollHeight > name.clientHeight, chipLines: chip.getClientRects().length,
            // Layout offsets, not client rects: those carry the CRT's squash.
            off: (chip.offsetTop + chip.offsetHeight / 2) - (name.offsetTop + name.offsetHeight / 2)});
};
let t = 0;
for (const [project, text] of lines) {
  setTimeout(() => Face.say({text, kind: "waiting", project, start: Date.now() / 1000}), t);
  setTimeout(() => measure(project), t + 400);
  t += 600;
}
setTimeout(() => { document.getElementById("out").textContent = JSON.stringify(out); }, t);
"""


def _lay_out(tmp_path, lines):
    for name in ("face.js", "face.css"):
        shutil.copy(FACE / name, tmp_path / name)
    # The panel's width: headless Chrome will not open a window under 500px.
    (tmp_path / "probe.css").write_text("body { width: 280px; }")
    page = (FACE / "index.html").read_text().replace(
        '<link rel="stylesheet" href="face.css">',
        '<link rel="stylesheet" href="face.css"><link rel="stylesheet" href="probe.css">').replace(
        '<script src="face.js"></script>',
        '<script src="face.js"></script><pre id="out"></pre><script src="probe.js"></script>')
    (tmp_path / "index.html").write_text(page)
    (tmp_path / "probe.js").write_text(PROBE % json.dumps(lines))
    chrome = _REAL_POPEN(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
         # HOME is a scratch dir here: without these, Chrome asks for a keychain.
         "--use-mock-keychain", "--password-store=basic",
         "--window-size=280,1000", "--user-data-dir=" + str(tmp_path / "profile"),
         "--virtual-time-budget=5000", "--dump-dom", (tmp_path / "index.html").as_uri()],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # It prints the page once the budget is spent and then, with the page still animating,
    # never exits: read up to the end of the page and stop it.
    killer = threading.Timer(30, chrome.kill)
    killer.start()
    dom = ""
    try:
        for line in chrome.stdout:
            dom += line
            if "</html>" in line:
                break
    finally:
        killer.cancel()
        chrome.kill()
        chrome.wait()
    found = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    assert found and found.group(1), "the probe never reported"
    return json.loads(html.unescape(found.group(1)))


LONG = ("mobile app, search result filters redesign",
        "I hit a fork in the search result filters redesign, and it needs a decision from you.")
SHORT = ("Dev", "I need you for this — nothing moves until you reply.")
HUGE = ("a project whose name goes on and on, " * 5, "Done.")


def test_a_long_project_name_wraps_and_leaves_the_bust_alone(tmp_path):
    long, short, huge = _lay_out(tmp_path, [LONG, SHORT, HUGE])
    for m in (long, short, huge):
        # The bust fills its column, and the column stays inside the window's padding.
        assert m["col"] <= m["win"] - 24, m
        assert m["w"] == m["col"], m
        # The chip on one line, centred on the name however many lines it takes.
        assert m["chipLines"] == 1, m
        assert abs(m["off"]) <= 1, m
    # The same size and shape for every line, never stretched.
    assert {(m["w"], m["h"]) for m in (long, short, huge)} == {(short["w"], short["h"])}
    assert (short["lines"], short["cut"]) == (1, False)
    assert (long["lines"], long["cut"]) == (2, False)
    assert long["winH"] > short["winH"]
    assert (huge["lines"], huge["cut"]) == (3, True)
