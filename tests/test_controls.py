from datetime import timedelta

from desk import control
from desk.controls import ControlPanel


def test_default_is_running(db, clock):
    st = ControlPanel(db, clock).state()
    assert st.entries_allowed and st.describe() == "RUNNING"


def test_pause_and_resume(db, clock):
    p = ControlPanel(db, clock)
    p.pause(by="test")
    assert p.state().paused and not p.state().entries_allowed
    ok, _ = p.resume(by="test")
    assert ok and p.state().entries_allowed
    kinds = [r["message"] for r in db.query("SELECT message FROM events WHERE kind='control'")]
    assert any("paused" in k for k in kinds) and any("resumed" in k for k in kinds)


def test_kill_needs_confirm_to_lift(db, clock):
    p = ControlPanel(db, clock)
    p.kill(by="test")
    assert p.state().killed and "KILLED" in p.state().describe()
    ok, msg = p.resume(by="test")
    assert not ok and p.state().killed and "confirm" in msg
    ok, _ = p.resume(by="test", confirm=True)
    assert ok and not p.state().killed


def test_kill_with_close_all(db, clock):
    p = ControlPanel(db, clock)
    p.kill(by="test", close_all=True)
    assert p.state().close_all_requested
    p.clear_close_all()
    assert not p.state().close_all_requested and p.state().killed


def test_daily_pause_ends_at_local_midnight(db, clock):
    p = ControlPanel(db, clock)
    p.set_daily_pause(clock.local_date())
    assert p.state().daily_paused and not p.state().entries_allowed
    ok, msg = p.resume(by="test")
    assert ok and "until midnight" in msg and p.state().daily_paused     # resume can't lift it
    clock.set(clock.next_day_start_utc() + timedelta(seconds=1))
    assert not p.state().daily_paused and p.state().entries_allowed


def test_control_cli(tmp_path, monkeypatch, capsys):
    import yaml
    from desk.config import DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["paths"]["database"] = str(tmp_path / "c.db")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert control.main(["pause", "--config", str(cfg)]) == 0
    assert "PAUSED" in capsys.readouterr().out
    assert control.main(["kill", "--config", str(cfg)]) == 0
    assert control.main(["resume", "--config", str(cfg)]) == 1          # kill needs --confirm
    assert control.main(["resume", "--confirm", "--config", str(cfg)]) == 0
    assert "RUNNING" in capsys.readouterr().out
