import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx

from desk.controls import ControlPanel
from desk.telegram import (HELP, CommandHandler, MultiNotifier, TelegramClient, TelegramCommands, TelegramError,
                           TelegramNotifier)
from desk.telegram_setup import find_chats, run as setup_run, save_chat_id

TOKEN = "123456789:AAH4kPq_ZfakeTokenValueForTesting1234567"
BASE = f"https://api.telegram.org/bot{TOKEN}"


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


# ---- client ------------------------------------------------------------------------------

@respx.mock
def test_send_and_errors():
    route = respx.post(f"{BASE}/sendMessage").mock(return_value=ok({}))
    TelegramClient(TOKEN).send("42", "hello")
    assert json.loads(route.calls.last.request.content) == {"chat_id": "42", "text": "hello",
                                                            "disable_web_page_preview": True}
    respx.post(f"{BASE}/getMe").mock(return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramError, match="Unauthorized"):
        TelegramClient(TOKEN).get_me()


@respx.mock
def test_long_messages_are_cut_and_network_errors_wrapped():
    route = respx.post(f"{BASE}/sendMessage").mock(return_value=ok({}))
    TelegramClient(TOKEN).send("42", "x" * 5000)
    assert len(json.loads(route.calls.last.request.content)["text"]) == 4000
    respx.post(f"{BASE}/getMe").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(TelegramError, match="couldn't reach"):
        TelegramClient(TOKEN).get_me()


@respx.mock
def test_get_updates_sends_telegrams_timeout_and_offset():
    route = respx.post(f"{BASE}/getUpdates").mock(return_value=ok([{"update_id": 5}]))
    assert TelegramClient(TOKEN).updates(7, timeout=25) == [{"update_id": 5}]
    body = json.loads(route.calls.last.request.content)
    assert body["timeout"] == 25 and body["offset"] == 7
    TelegramClient(TOKEN).updates(None, timeout=0)
    assert "offset" not in json.loads(route.calls.last.request.content)


def test_token_never_in_repr():
    assert TOKEN not in repr(TelegramClient(TOKEN))


# ---- alerts -----------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, fail_times=0, updates=None):
        self.sent, self.fail_times, self._updates = [], fail_times, list(updates or [])
        self.update_calls = []

    def send(self, chat_id, text):
        if self.fail_times:
            self.fail_times -= 1
            raise TelegramError("boom")
        self.sent.append((chat_id, text))

    def updates(self, offset, timeout=25):
        self.update_calls.append(offset)
        out = [u for u in self._updates if offset is None or u["update_id"] >= offset]
        return out

    def get_me(self):
        return {"username": "test_bot", "first_name": "Test"}


def test_notifier_queues_and_sends_with_icons():
    c = FakeClient()
    n = TelegramNotifier(c, "42", start=False)
    n.send("trade", "PAPER BUY 0.01 BTC")
    n.send("daily_summary", "Daily summary ...")
    assert c.sent == []                                   # nothing blocks the trading loop
    n.flush()
    assert c.sent == [("42", "💱 PAPER BUY 0.01 BTC"), ("42", "📊 Daily summary ...")]


def test_notifier_retries_then_drops():
    c = FakeClient(fail_times=2)
    n = TelegramNotifier(c, "42", start=False, sleep=lambda s: None)
    n.send("error", "x")
    n.flush()
    assert n.sent == 1 and len(c.sent) == 1
    c2 = FakeClient(fail_times=10)
    n2 = TelegramNotifier(c2, "42", start=False, sleep=lambda s: None, retries=3)
    n2.send("error", "x")
    n2.flush()
    assert n2.dropped == 1 and c2.sent == []


def test_background_thread_delivers():
    import time
    c = FakeClient()
    n = TelegramNotifier(c, "42")
    n.send("info", "hi")
    for _ in range(50):
        if c.sent:
            break
        time.sleep(0.02)
    assert c.sent == [("42", "ℹ️ hi")]


def test_multi_notifier_survives_a_broken_channel():
    good = SimpleNamespace(got=[])
    good.send = lambda k, t: good.got.append(t)
    bad = SimpleNamespace(send=lambda k, t: 1 / 0)
    MultiNotifier(bad, good).send("info", "still delivered")
    assert good.got == ["still delivered"]


# ---- commands -----------------------------------------------------------------------------------

@pytest.fixture
def handler(db, clock):
    panel = ControlPanel(db, clock)
    return CommandHandler(panel, lambda: "STATUS TEXT", lambda: "POSITIONS TEXT", clock.now), panel


def test_basic_commands(handler):
    h, panel = handler
    assert h.handle("/status") == "STATUS TEXT"
    assert h.handle("/positions") == "POSITIONS TEXT"
    assert h.handle("/help") == HELP and h.handle("/start") == HELP
    assert "don't know" in h.handle("/moon")
    assert h.handle("/status@my_desk_bot") == "STATUS TEXT"


def test_pause_resume_kill(handler):
    h, panel = handler
    h.handle("/pause")
    assert panel.state().paused
    h.handle("/resume")
    assert not panel.state().paused
    h.handle("/kill")
    assert panel.state().killed and not panel.state().close_all_requested
    assert "/resume confirm" in h.handle("/resume")
    assert panel.state().killed
    h.handle("/resume confirm")
    assert not panel.state().killed


def test_kill_close_needs_confirm_within_60s(handler, clock):
    h, panel = handler
    msg = h.handle("/kill close")
    assert "SELL ALL" in msg and not panel.state().killed           # nothing happens yet
    h.handle("/confirm")
    assert panel.state().killed and panel.state().close_all_requested


def test_confirm_expires(handler, clock):
    h, panel = handler
    h.handle("/kill close")
    clock.advance(seconds=61)
    assert "Nothing to confirm" in h.handle("/confirm")
    assert not panel.state().killed
    assert "Nothing to confirm" in h.handle("/confirm")             # and a stray /confirm does nothing


def msg(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat, "type": "private"}, "text": text}}


def test_poll_only_obeys_your_chat(handler, db):
    h, panel = handler
    c = FakeClient(updates=[msg(1, 999, "/kill"), msg(2, 42, "/pause"), msg(3, 42, "hello there")])
    tc = TelegramCommands(c, "42", h, db=db)
    assert tc.poll_once() == 1
    assert panel.state().paused and not panel.state().killed       # the stranger's /kill was ignored
    assert len(c.sent) == 1 and c.sent[0][0] == "42" and c.sent[0][1].startswith("Paused")   # one reply, to you
    assert db.query_one("SELECT COUNT(*) FROM events WHERE message LIKE 'Ignored a message%'")[0] == 1
    tc.poll_once()
    assert c.update_calls[-1] == 4                                   # offset moved past handled messages


def test_old_commands_are_skipped_at_startup(handler, db):
    h, panel = handler
    c = FakeClient(updates=[msg(5, 42, "/kill close"), msg(6, 42, "/confirm")])
    tc = TelegramCommands(c, "42", h, db=db)
    tc.skip_backlog()
    assert tc.poll_once() == 0 and not panel.state().killed


def test_a_failing_command_replies_instead_of_crashing(db, clock):
    h = CommandHandler(ControlPanel(db, clock), lambda: 1 / 0, lambda: "", clock.now)
    c = FakeClient(updates=[msg(1, 42, "/status")])
    TelegramCommands(c, "42", h).poll_once()
    assert "failed" in c.sent[0][1]


# ---- setup helper -----------------------------------------------------------------------------------

def test_find_chats():
    ups = [msg(1, 42, "hi"), msg(2, 42, "again"),
           {"update_id": 3, "message": {"chat": {"id": 7, "type": "private", "first_name": "Sam", "username": "sam"}}},
           {"update_id": 4, "message": {"chat": {"id": -100, "type": "group"}}}]
    assert find_chats(ups) == [("42", "unknown"), ("7", "Sam (@sam)")]


def test_save_chat_id_replaces_or_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=\nOTHER=1\n", encoding="utf-8")
    save_chat_id(env, "42")
    assert env.read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=42\nOTHER=1\n"
    env2 = tmp_path / "b.env"
    env2.write_text("X=1", encoding="utf-8")
    save_chat_id(env2, "7")
    assert env2.read_text(encoding="utf-8") == "X=1\nTELEGRAM_CHAT_ID=7\n"


def test_setup_flow(tmp_path, capsys, monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_CHAT_ID=\n", encoding="utf-8")
    assert setup_run(env, FakeClient()) == 1
    assert "@BotFather" in capsys.readouterr().out                  # no token -> instructions

    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
    assert setup_run(env, FakeClient()) == 1
    assert "send it \"hi\"" in capsys.readouterr().out               # no messages yet

    c = FakeClient(updates=[msg(1, 42, "hi")])
    assert setup_run(env, c, ask=lambda q: "n") == 1
    assert "TELEGRAM_CHAT_ID=42" not in env.read_text(encoding="utf-8")
    c = FakeClient(updates=[msg(1, 42, "hi")])
    assert setup_run(env, c, ask=lambda q: "y") == 0
    assert "TELEGRAM_CHAT_ID=42" in env.read_text(encoding="utf-8")
    assert c.sent and c.sent[0][0] == "42" and "connected" in c.sent[0][1]


def test_running_bot_switches_telegram_on_when_setup_finishes(tmp_path, db, clock, monkeypatch):
    import threading
    import time
    from types import SimpleNamespace
    import desk.main as main_mod
    from desk.notify import MemoryNotifier
    from tests.fakes import baseline_config

    fake = FakeClient()
    monkeypatch.setattr(main_mod, "TelegramClient", lambda token: fake)
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")      # token, but no chat ID yet
    app = SimpleNamespace(config=baseline_config(), clock=clock, db=db,
                          coinspot=SimpleNamespace(latest_all=lambda: {}))
    desk = SimpleNamespace(controls=ControlPanel(db, clock))
    multi = MultiNotifier(MemoryNotifier())
    stop = threading.Event()
    main_mod.watch_for_telegram(app, desk, multi, stop, every=0.02, env_path=env)
    time.sleep(0.1)
    assert len(multi.notifiers) == 1                                        # still waiting
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8")   # setup finished
    for _ in range(100):
        if fake.sent:
            break
        time.sleep(0.02)
    stop.set()
    assert len(multi.notifiers) == 2
    assert fake.sent and fake.sent[0][0] == "42" and "connected" in fake.sent[0][1]


def test_polling_never_spins_when_telegram_answers_instantly(db, clock):
    sleeps = []
    c = FakeClient()
    tc = TelegramCommands(c, "42", CommandHandler(ControlPanel(db, clock), str, str, clock.now),
                          sleep=lambda s: (sleeps.append(s), tc.stop.set()))
    tc.run()
    assert sleeps == [1.0]
