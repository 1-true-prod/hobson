"""The setup wizard's rules and its local server (scripts/setup_wizard.py)."""

import json
import os
import re
import stat
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import decider
import home
import setup_wizard as sw


def facts(**over):
    """A probe result for a well-equipped Mac, with overrides."""
    base = {
        "ram_gb": 48, "disk_free_gb": 58, "uv": True, "brew": True,
        "ollama": {"installed": True, "running": True, "version": "0.32.5", "models": ["llama3.2:3b"]},
        "engines": [{"id": "say", "ready": True}, {"id": "kokoro-realtime", "ready": False},
                    {"id": "chatterbox", "ready": False}],
        "presence": {"built": True, "swiftc": True, "camera": "authorized"},
        "phone": {"adb": "/opt/homebrew/bin/adb", "devices": []},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


# ── What to suggest ────────────────────────────────────────────────────────

def test_a_model_already_pulled_is_suggested_over_pulling_one():
    assert sw.recommend_model(48, 58, ["qwen3.5:4b"]) == "qwen3.5:4b"
    assert sw.recommend_model(48, 58, ["llama3.2:3b", "qwen3.5:4b"]) == "llama3.2:3b"
    assert sw.recommend_model(48, 58, ["llama3.2:3b:latest"]) is not None


def test_model_by_memory_and_disk():
    assert sw.recommend_model(48, 58, []) == "llama3.2:3b"
    assert sw.recommend_model(4, 58, []) == "llama3.2:1b"
    assert sw.recommend_model(48, 3, []) is None


def test_express_never_installs_uv_itself():
    assert sw.recommend(facts(uv=False))["engine"] == "say"
    assert sw.recommend(facts())["engine"] == "kokoro-realtime"


def test_express_has_no_brain_without_ollama_or_homebrew():
    rec = sw.recommend(facts(brew=False, ollama={"installed": False, "running": False, "models": []}))
    assert rec["model"] == "none"
    assert rec["engine"] == "say"  # Kokoro's phrases need a brain


def test_express_leaves_the_decider_to_be_asked():
    assert sw.recommend(facts())["decider"] is None


@pytest.mark.parametrize("presence,mode", [
    ({"camera": "authorized"}, "auto"),
    ({"camera": "not-determined"}, "auto"),
    ({"camera": "denied"}, "signals"),
    ({"built": False, "swiftc": False, "camera": ""}, "signals"),
])
def test_express_presence(presence, mode):
    assert sw.recommend(facts(presence=presence))["presence"] == mode


def test_express_never_turns_the_phone_switch_on():
    rec = sw.recommend(facts(phone={"devices": [{"serial": "X", "model": "Pixel", "transport": "usb"}]}))
    assert rec["phone"] is None


# ── What gets written ──────────────────────────────────────────────────────

def test_jev_is_never_written_without_a_passing_call():
    assert sw.desired_settings({"decider": "jev"}, False, 0)["decider.backend"] == "local"
    assert sw.desired_settings({"decider": "jev"}, True, 0)["decider.backend"] == "jev"
    assert "decider.backend" not in sw.desired_settings({"decider": None}, True, 0)


def test_presence_off_disables_it_without_touching_the_mode():
    want = sw.desired_settings({"presence": "off"}, False, 0)
    assert want == {"presence.enabled": False}


def test_one_phone_is_followed_by_auto_several_by_serial():
    assert sw.desired_settings({"phone": "adb-1"}, False, 1)["presence.phone"] == "auto"
    assert sw.desired_settings({"phone": "adb-1"}, False, 2)["presence.phone"] == "adb-1"
    assert sw.desired_settings({"phone": None}, False, 1)["presence.phone"] is None


def test_no_brain_writes_no_model():
    assert "ollama.model" not in sw.desired_settings({"model": "none"}, False, 0)


def test_a_default_is_not_written():
    config, changes = sw.apply_settings({}, {"engine": "say", "presence.mode": "auto"})
    assert config == {} and changes == []


def test_a_choice_that_differs_is_written_and_nothing_else():
    config, changes = sw.apply_settings({"volume": 7}, {"engine": "kokoro-realtime", "presence.mode": "continuous"})
    assert config == {"volume": 7, "engine": "kokoro-realtime", "presence": {"mode": "continuous"}}
    assert {c["op"] for c in changes} == {"set"}


def test_a_setting_put_back_to_its_default_is_removed():
    user = {"engine": "say", "presence": {"mode": "continuous"}, "decider": {"backend": "jev", "timeout_ms": 9000}}
    config, changes = sw.apply_settings(user, {"engine": "say", "presence.mode": "auto", "decider.backend": "local"})
    assert config == {"decider": {"timeout_ms": 9000}}
    assert sorted(c["key"] for c in changes if c["op"] == "unset") == ["decider.backend", "engine", "presence.mode"]
    assert user["presence"] == {"mode": "continuous"}  # the input is left alone


def test_defaults_kept_counts_leaf_settings():
    total = sw.defaults_kept({})
    assert sw.defaults_kept({"engine": "x", "presence": {"mode": "auto"}}) == total - 2


def test_clean_answers_drops_what_it_does_not_know():
    a = sw.clean_answers({"engine": "rm -rf", "events": ["stop", "bogus"], "model": "evil; rm -rf ~",
                          "decider": "jev", "greetings": "yes", "phone": "adb-1", "extra": 1})
    assert a == {"events": ["stop"], "decider": "jev", "phone": "adb-1"}


def test_an_unanswered_phone_leaves_the_switch_alone():
    assert "phone" not in sw.clean_answers({"engine": "say"})
    assert sw.clean_answers({"phone": None}) == {"phone": None}


# ── What COMMIT does ───────────────────────────────────────────────────────

def ids(tasks):
    return [t["id"] for t in tasks]


def test_tasks_install_only_what_is_missing():
    a = {"engine": "kokoro-realtime", "model": "gemma4:e4b", "presence": "auto"}
    assert ids(sw.tasks_for(a, facts(uv=False), False)) == ["uv", "kokoro", "pull", "config", "hooks", "hello"]
    ready = facts(engines=[{"id": "kokoro-realtime", "ready": True}], ollama={"models": ["gemma4:e4b"]})
    assert ids(sw.tasks_for(a, ready, False)) == ["config", "hooks", "hello"]


def test_tasks_install_ollama_before_pulling():
    a = {"engine": "say", "model": "llama3.2:3b"}
    f = facts(ollama={"installed": False, "running": False, "models": []})
    assert ids(sw.tasks_for(a, f, False))[:2] == ["ollama", "pull"]


def test_the_camera_is_asked_for_only_when_used_and_not_yet_asked():
    f = facts(presence={"camera": "not-determined"})
    assert "camera" in ids(sw.tasks_for({"presence": "auto"}, f, False))
    assert "camera" not in ids(sw.tasks_for({"presence": "signals"}, f, False))
    assert "camera" not in ids(sw.tasks_for({"presence": "auto"}, facts(), False))


def test_a_verified_typed_key_is_saved():
    assert "key" in ids(sw.tasks_for({}, facts(), True))
    assert "key" not in ids(sw.tasks_for({}, facts(), False))


def test_chatterbox_is_left_to_its_own_setup():
    assert "chatterbox" in ids(sw.tasks_for({"engine": "chatterbox"}, facts(), False))


# ── Sections seen ──────────────────────────────────────────────────────────

def test_a_fresh_install_sees_every_section():
    assert sw.unseen({}, config_exists=False) == list(sw.SECTIONS)


def test_an_install_older_than_the_wizard_is_offered_the_new_sections():
    assert sw.unseen({}, config_exists=True) == ["brain", "jev", "presence", "phone"]


def test_mark_seen_accumulates(claude_home):
    sw.mark_seen(["voice"])
    sw.mark_seen(["jev"])
    assert sw.load_setup_state()["seen"] == ["jev", "voice"]
    assert sw.unseen(sw.load_setup_state(), True) == ["events", "brain", "presence", "phone"]


# ── Files ──────────────────────────────────────────────────────────────────

def test_parse_adb_devices_keeps_ready_phones_only():
    text = ("List of devices attached\n"
            "adb-00A024RZ-X7._adb-tls-connect._tcp device product:Nothing model:A024 device:A024 transport_id:3\n"
            "R58M12345 unauthorized usb:1-1 transport_id:4\n"
            "emulator-5554 device product:sdk model:sdk_gphone64_arm64 transport_id:1\n")
    devices = sw.parse_adb_devices(text)
    assert [d["serial"] for d in devices] == ["adb-00A024RZ-X7._adb-tls-connect._tcp"]
    assert devices[0]["transport"] == "wireless" and devices[0]["model"] == "A024"


def test_a_wireless_phone_listed_twice_is_kept_once_by_name():
    text = ("List of devices attached\n"
            "192.168.1.20:41235 device product:Nothing model:A024 transport_id:5\n"
            "adb-00A024RZ-X7._adb-tls-connect._tcp device product:Nothing model:A024 transport_id:6\n"
            "R58M12345 device usb:1-1 product:beyond model:SM_G973F transport_id:7\n")
    assert [d["serial"] for d in sw.parse_adb_devices(text)] == [
        "adb-00A024RZ-X7._adb-tls-connect._tcp", "R58M12345"]


def test_set_env_value_replaces_only_its_own_line():
    text = "# mine\nOTHER=1\nOPENROUTER_API_KEY=old\n"
    assert sw.set_env_value(text, "OPENROUTER_API_KEY", "new") == "# mine\nOTHER=1\nOPENROUTER_API_KEY=new\n"


def test_save_key_is_private_and_readable_by_the_decider(claude_home):
    sw.save_key("sk-or-v1-test")
    path = home.env_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert decider.find_key() == "sk-or-v1-test"


# ── The key check ──────────────────────────────────────────────────────────

def test_check_key_says_why_a_key_failed(fake_decider):
    fake_decider.http_error(401)
    assert decider.check_key("sk-bad") == {"ok": False, "reason": "the key was refused (401)"}


def test_check_key_reports_time_and_cost(fake_decider):
    fake_decider.respond({"answers": {"answer": {"type": "noul", "noul": 0.1}}, "usage": {"cost": 0.000017}})
    result = decider.check_key("sk-good")
    assert result["ok"] and result["cost"] == 0.000017 and result["ms"] >= 0


def test_check_key_without_a_key_makes_no_call(fake_decider):
    fake_decider.respond({})
    assert decider.check_key(None)["ok"] is False
    assert fake_decider.calls() == 0


def test_a_passing_typed_key_is_held_for_commit(fake_decider):
    fake_decider.respond({"answers": {"answer": {"type": "noul", "noul": 0.1}}})
    wiz = sw.Wizard(python="/usr/bin/python3")
    wiz.facts = facts()
    assert wiz.jev_test({"key": "sk-typed"})["ok"]
    plan = wiz.plan({"decider": "jev"})
    assert {"key": "decider.backend", "value": "jev"} in plan["writes"]
    assert plan["secrets"] and "key" in ids(plan["tasks"])


def test_a_key_found_only_in_the_environment_is_saved_unless_declined(fake_decider, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    fake_decider.respond({"answers": {"answer": {"type": "noul", "noul": 0.1}}})
    wiz = sw.Wizard(python="/usr/bin/python3")
    wiz.facts = facts()
    assert wiz.jev_test({})["ok"]
    assert wiz.key_pending({"decider": "jev"})
    assert not wiz.key_pending({"decider": "jev", "save_env_key": False})
    assert not wiz.key_pending({"decider": "local"})


def test_a_key_already_in_hobson_env_is_not_written_again(fake_decider, claude_home):
    sw.save_key("sk-file")
    fake_decider.respond({"answers": {"answer": {"type": "noul", "noul": 0.1}}})
    wiz = sw.Wizard(python="/usr/bin/python3")
    wiz.facts = facts()
    assert wiz.jev_test({})["ok"] and wiz.verified_source == "file"
    assert not wiz.key_pending({"decider": "jev"})


def test_jev_chosen_without_a_passing_key_stays_local_and_says_so():
    wiz = sw.Wizard(python="/usr/bin/python3")
    wiz.facts = facts()
    plan = wiz.plan({"decider": "jev"})
    assert not any(w["key"] == "decider.backend" and w["value"] == "jev" for w in plan["writes"])
    assert any(w.get("note") and w["key"] == "decider.backend" for w in plan["writes"])


def test_config_task_writes_only_the_differences(claude_home):
    wiz = sw.Wizard(python="/usr/bin/python3")
    f = facts()
    answers = {"engine": "kokoro-realtime", "personality": "hobson", "events": ["stop", "permission", "notification"],
               "presence": "auto", "decider": "local"}
    assert wiz._task_config(answers, f, lambda e: None)[0] == "ok"
    with open(home.config_file()) as fh:
        assert json.load(fh) == {"engine": "kokoro-realtime"}
    assert sw.load_setup_state()["seen"] == sorted(sw.SECTIONS)


# ── The server ─────────────────────────────────────────────────────────────

@pytest.fixture
def server():
    wiz = sw.Wizard(python="/usr/bin/python3")
    wiz.facts = facts()
    wiz.get_facts = lambda fresh=False: wiz.facts
    srv = sw.serve(wiz)
    yield wiz, srv.server_address[1]
    wiz.done.set()
    srv.shutdown()


def call(port, path, token=None, body=None, host=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Hobson-Token"] = token
    if host:
        headers["Host"] = host
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    req = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers)
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def test_api_needs_the_token(server):
    wiz, port = server
    assert call(port, "/api/probe")[0] == 403
    assert call(port, "/api/probe", token="wrong")[0] == 403
    assert call(port, "/api/plan", body={"answers": {}})[0] == 403
    status, body = call(port, "/api/probe", token=wiz.token)
    assert status == 200 and json.loads(body)["ram_gb"] == 48


def test_another_host_name_is_refused(server):
    # A DNS-rebinding page reaches 127.0.0.1 under its own host name.
    wiz, port = server
    assert call(port, "/api/probe", token=wiz.token, host=f"evil.example:{port}")[0] == 403
    assert call(port, "/api/plan", token=wiz.token, body={"answers": {}},
                host=f"evil.example:{port}")[0] == 403
    assert call(port, "/", host=f"evil.example:{port}")[0] == 403


def test_another_origin_is_refused_even_with_the_token(server):
    """The guard is loopback's, shared with the TTS daemons: a page elsewhere
    posts with its own Origin, token or not; the wizard's page with this one."""
    wiz, port = server
    plan = {"answers": {}}
    assert call(port, "/api/plan", token=wiz.token, body=plan, origin="https://evil.example")[0] == 403
    assert call(port, "/api/plan", token=wiz.token, body=plan, origin="null")[0] == 403
    assert call(port, "/api/plan", token=wiz.token, body=plan, origin=f"http://127.0.0.1:{port}")[0] == 200


def test_a_post_outside_the_api_is_not_routed(server):
    wiz, port = server
    assert call(port, "/xxxxplan", token=wiz.token, body={"answers": {}})[0] == 404


def test_only_the_wizard_files_are_served(server):
    wiz, port = server
    assert call(port, "/")[0] == 200
    assert call(port, "/wizard.js")[0] == 200
    assert call(port, "/mock.js")[0] == 404
    assert call(port, "/../scripts/home.py")[0] == 404
    assert call(port, "/%2e%2e/hobson.json")[0] == 404


def test_plan_over_http(server, claude_home):
    wiz, port = server
    status, body = call(port, "/api/plan", token=wiz.token,
                        body={"answers": {"engine": "kokoro-realtime", "model": "llama3.2:3b"}})
    plan = json.loads(body)
    assert status == 200
    assert plan["writes"] == [{"key": "engine", "value": "kokoro-realtime"}]
    assert ids(plan["tasks"])[:1] == ["kokoro"]


def test_apply_streams_one_event_per_step_and_refuses_a_second_run(server, monkeypatch):
    wiz, port = server
    gate = threading.Event()
    for name in ("config", "hooks", "hello"):
        monkeypatch.setattr(wiz, f"_task_{name}", lambda a, f, emit: (gate.wait(5), ("ok", ""))[1])
    result = {}

    def run():
        result["resp"] = call(port, "/api/apply", token=wiz.token, body={"answers": {"engine": "say"}})
    t = threading.Thread(target=run)
    t.start()
    for _ in range(100):
        if wiz.busy.locked():
            break
        threading.Event().wait(0.02)
    assert call(port, "/api/apply", token=wiz.token, body={"answers": {}})[0] == 409
    gate.set()
    t.join(5)
    events = [json.loads(line) for line in result["resp"][1].decode().splitlines()]
    assert events[-1] == {"done": True, "ok": True}
    assert [e["state"] for e in events if e.get("task") == "config"] == ["run", "ok"]
    assert wiz.applied is True


# ── Voices ─────────────────────────────────────────────────────────────────

def test_a_voice_is_written_only_when_it_differs_from_the_default(claude_home):
    assert sw.apply_settings({}, sw.desired_settings({"engine": "kokoro-realtime", "voice": "am_puck"}, False, 0))[0] == \
        {"engine": "kokoro-realtime"}
    config, _ = sw.apply_settings({}, sw.desired_settings({"engine": "pocket-tts", "voice": "anna"}, False, 0))
    assert config == {"engine": "pocket-tts", "pocket_tts": {"voice": "anna"}}


def test_the_page_cannot_point_a_voice_at_a_file():
    a = sw.clean_answers({"engine": "pocket-tts", "voice": "/Users/x/.claude/models/clone.safetensors"})
    assert a == {"engine": "pocket-tts"}
    assert sw.clean_answers({"engine": "say", "voice": "anna"}) == {"engine": "say"}


def test_a_voice_of_your_own_is_shown_as_yours_and_left_alone(claude_home):
    config = home.load_config()
    config["engine"] = "pocket-tts"
    config["pocket_tts"] = {**config["pocket_tts"], "voice": "/Users/x/.claude/models/butler-voice.safetensors"}
    existing = sw.answers_from_config(config)
    assert existing["voice"] is None and existing["voice_custom"] == "butler-voice.safetensors"
    assert "pocket_tts.voice" not in sw.desired_settings({"engine": "pocket-tts", "voice": None}, False, 0)


def test_express_never_picks_pocket_tts():
    assert sw.recommend(facts())["engine"] != "pocket-tts"


def test_pocket_tts_install_is_planned_with_its_real_size():
    f = facts(engines=[{"id": "pocket-tts", "ready": False}])
    tasks = sw.tasks_for({"engine": "pocket-tts"}, f, False)
    assert ids(tasks)[0] == "pocket" and "1 GB" in tasks[0]["detail"]


def test_clips_are_served_and_nothing_else_in_the_voice_folder(server):
    wiz, port = server
    with open(os.path.join(home.ROOT, "setup", "ui", "voice", "manifest.json")) as f:
        clip = next(iter(json.load(f)["lines"].values()))
    status, body = call(port, "/" + clip)
    assert status == 200 and len(body) > 1000
    assert call(port, "/voice/LICENSES.md")[0] == 404
    assert call(port, "/voice/0123456789abcdef.m4a")[0] == 404  # listed shape, no such file
    assert call(port, "/lines.json")[0] == 200


def test_every_picture_art_js_shows_is_served(server):
    # A still the allowlist refuses is not an error anyone sees: the monitor
    # just stays black.
    wiz, port = server
    with open(os.path.join(home.ROOT, "setup", "ui", "art.js"), encoding="utf-8") as f:
        stills = re.findall(r'still\("([^"]+)"', f.read())
    assert len(stills) == 7
    for path in stills:
        status, body = call(port, "/" + path)
        assert status == 200 and body.startswith(b"\x89PNG"), path
    assert call(port, "/art.js")[0] == 200
    assert call(port, "/art/LICENSES.md")[0] == 404


def test_the_display_font_is_served_and_credited(server):
    # Bundled so the page fetches nothing; refused, the titles fall back to
    # a system face and nobody is told.
    wiz, port = server
    with open(os.path.join(home.ROOT, "setup", "ui", "wizard.css"), encoding="utf-8") as f:
        fonts = re.findall(r'url\("([^"]+\.woff2)"\)', f.read())
    assert fonts
    for path in fonts:
        status, body = call(port, "/" + path)
        assert status == 200 and body.startswith(b"wOF2"), path
    with open(os.path.join(home.ROOT, "setup", "ui", "fonts", "LICENSES.md"), encoding="utf-8") as f:
        credits = f.read()
    assert all(f"`{os.path.basename(p)}`" in credits for p in fonts)
    assert call(port, "/fonts/OFL.md")[0] == 404


def test_every_picture_is_credited():
    art = os.path.join(home.ROOT, "setup", "ui", "art")
    with open(os.path.join(art, "LICENSES.md"), encoding="utf-8") as f:
        credits = f.read()
    pictures = [n for n in os.listdir(art) if n.endswith(".png")]
    assert pictures and all(f"`{n}`" in credits for n in pictures)


# ── Any Ollama model ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["mistral:7b", "qwen3:8b", "library/llama3", "hf.co/user/repo:Q4_K_M",
                                  "hf.co/csoares31/AMALIA-9B-0626-DPO-GGUF:latest"])
def test_any_ollama_model_name_is_accepted(name):
    assert sw.clean_answers({"model": name}) == {"model": name}


@pytest.mark.parametrize("name", ["", "rm -rf /", "a b", "-x", "$(id)", "x" * 300, 7])
def test_what_is_not_a_model_name_is_dropped(name):
    assert "model" not in sw.clean_answers({"model": name})


def test_a_model_outside_the_catalogue_is_written_and_pulled():
    config, _ = sw.apply_settings({}, sw.desired_settings({"model": "mistral:7b"}, False, 0))
    assert config == {"ollama": {"model": "mistral:7b"}}
    pull = [t for t in sw.tasks_for({"model": "mistral:7b"}, facts(), False) if t["id"] == "pull"]
    assert pull and "size shows once it starts" in pull[0]["detail"]
    hf = [t for t in sw.tasks_for({"model": "hf.co/u/r:Q4"}, facts(), False) if t["id"] == "pull"]
    assert "Hugging Face" in hf[0]["detail"]


def test_a_model_already_pulled_is_not_pulled_again():
    f = facts(ollama={"models": ["mistral:7b"]})
    assert "pull" not in ids(sw.tasks_for({"model": "mistral:7b"}, f, False))


def test_an_audition_reads_the_page_text_as_text(no_audio):
    """The page sends the text; before `--`, "-o<path>" was an option to say."""
    sw.speak("-o/Users/me/notes.txt", "hobson")
    [argv] = [a for a in no_audio["popen"] if a and a[0] == "say"]
    assert argv[-2:] == ["--", "-o/Users/me/notes.txt"]
