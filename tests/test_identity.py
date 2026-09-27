"""Per-project audio identity, and reading notification_type."""


def _engine(**over):
    from engines.say import SayEngine
    cfg = {"engine": "say", "personality": "hobson", "volume": 3}
    cfg.update(over)
    return SayEngine(cfg)


def test_afplay_args_carry_volume_and_the_path():
    eng = _engine()
    args = eng._afplay_args("/tmp/x.wav")
    assert args[0] == "afplay"
    assert "-v" in args
    assert args[-1] == "/tmp/x.wav"


def _detail(hook_input):
    return _engine()._describe_event(hook_input)


def test_permission_notification_is_not_described_as_an_error():
    """Live defect: 'Message: Claude needs your permission' produced
    'broken | I hit a permission issue.' — the model inferred an error from a
    bare string because it was never told the type."""
    detail = _detail({"hook_event_name": "Notification",
                      "notification_type": "permission_prompt",
                      "message": "Claude needs your permission"})
    assert "waiting" in detail.lower() or "approval" in detail.lower()
    assert "error" not in detail.lower()


def test_idle_prompt_is_described_as_waiting_on_the_user():
    detail = _detail({"hook_event_name": "Notification",
                      "notification_type": "idle_prompt"})
    assert "waiting" in detail.lower()


def test_unknown_notification_type_falls_back_to_the_message():
    detail = _detail({"hook_event_name": "Notification",
                      "notification_type": "some_future_type",
                      "message": "Something happened"})
    assert "Something happened" in detail


def test_notification_without_a_type_still_works():
    assert _detail({"hook_event_name": "Notification", "message": "hi"})


from engines.base import project_rate


def test_rate_is_deterministic_for_a_label():
    assert project_rate("hobson") == project_rate("hobson")


def test_different_projects_usually_differ():
    rates = {project_rate(n) for n in
             ("hobson", "mobile app", "dark mode migration", "notes", "dev")}
    assert len(rates) >= 3, "labels should spread across the available steps"


def test_rate_stays_in_a_natural_band():
    for name in ("a", "b", "c", "hobson", "very-long-project-label-here"):
        assert 0.9 <= project_rate(name) <= 1.1


def test_one_step_is_exactly_unity():
    """Some project must sound untouched, or every project sounds 'off'."""
    rates = {project_rate(f"p{i}") for i in range(60)}
    assert 1.0 in rates


def test_rate_flag_reaches_afplay_when_enabled(monkeypatch):
    import home
    # A label whose rate is not exactly 1.0 ("hobson" hashes to 1.0, no -r).
    monkeypatch.setattr(home, "derive_project_label", lambda: "butler")
    eng = _engine(project_identity={"enabled": True, "spread": 0.06})
    args = eng._afplay_args("/tmp/x.wav")
    assert "-r" in args
    assert "-q" in args, "rate-scaled playback needs high-quality resampling"


def test_no_rate_flag_when_disabled(monkeypatch):
    eng = _engine(project_identity={"enabled": False})
    assert "-r" not in eng._afplay_args("/tmp/x.wav")
