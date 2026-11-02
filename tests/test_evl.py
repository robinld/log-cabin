import asyncio
import json

import pytest

from home_events import db
from home_events.flumes import evl


@pytest.fixture
def rows(monkeypatch):
    out = []
    monkeypatch.setattr(db, "append", lambda conn, **kw: out.append(kw))
    return out


class Panel:
    """A TPI writer that acks each keypress (or not), like the EVL."""

    def __init__(self, flume, ack="00", delay=0):
        self.flume, self.ack, self.delay, self.keys = flume, ack, delay, []

    def write(self, b):
        self.keys.append(b.decode()[6])  # "^03,1,<key>$\r\n"
        if self.ack:
            asyncio.get_running_loop().call_later(self.delay, self.flume.store, f"^03,{self.ack}$")

    async def drain(self):
        pass


def connected(ack="00", delay=0):
    f = evl.Flume(None, "host", "password")
    f.writer = Panel(f, ack, delay)
    return f


def test_store_drops_repeated_keypad_screens_but_keeps_changes_back(rows):
    f = evl.Flume(None, "host", "password")
    for line in ["%00,01,1C08,08,00,DISARMED$", "%00,01,1C08,08,00,DISARMED$",
                 "%01,0080$", "%01,0000$", "%01,0080$",
                 "%03,1130010100$", "%03,3130010100$", "%03,1130010100$"]:
        f.store(line)
    assert [r["body"] for r in rows] == ["%00,01,1C08,08,00,DISARMED$", "%01,0080$", "%01,0000$",
                                         "%01,0080$", "%03,1130010100$", "%03,3130010100$",
                                         "%03,1130010100$"]


def test_status_reads_the_keypad_leds_and_forgets_them_when_disconnected(rows):
    f = evl.Flume(None, "host", "password")
    f.writer = object()
    f.store("%00,01,1C38,08,00,DISARMED CHIME$")
    assert f.status() == {"connected": True, "state": "disarmed", "ready": True,
                          "bypass": True, "chime": True, "entry_delay": False}
    f.store("%00,01,8008,08,00,ARMED STAY$")
    assert f.status()["state"] == "stay"
    f.writer = f.leds = None  # what session() does when it ends
    assert f.status() == {"connected": False}


@pytest.mark.parametrize("action, code, err", [
    ("panic", "1234", "unknown action"),
    (["chime"], "1234", "unknown action"),
    ("chime", "123", "the code is 4 digits"),
    ("chime", "١٢٣٤", "the code is 4 digits"),  # Arabic-Indic digits match \d
    ("chime", 1234, "the code is 4 digits"),
    ("chime", None, "the code is 4 digits"),
])
def test_act_refuses_anything_but_an_allowed_action_and_four_ascii_digits(rows, action, code, err):
    f = connected()
    assert asyncio.run(f.act(action, code)) == err
    assert f.writer.keys == [] and rows == []


def test_act_sends_the_code_then_the_key_one_at_a_time_and_logs_without_the_code(rows):
    f = connected()
    assert asyncio.run(f.act("away", "1234")) is None
    assert f.writer.keys == ["1", "2", "3", "4", "2"]
    assert rows == [{"source": "evl", "event_type": "action", "event_name": "away",
                     "body": "Arm away from HomeKit"}]


def test_act_refuses_a_second_action_while_the_first_is_still_sending(rows):
    async def go():
        f = connected(delay=0.05)
        first = asyncio.create_task(f.act("stay", "1234"))
        await asyncio.sleep(0.01)
        second = await f.act("disarm", "1234")
        return f, await first, second
    f, first, second = asyncio.run(go())
    assert (first, second) == (None, "one action at a time")
    assert f.writer.keys == ["1", "2", "3", "4", "3"]


def test_act_waits_the_gap_after_an_action_finishes(rows, monkeypatch):
    f = connected()
    assert asyncio.run(f.act("chime", "1234")) is None
    assert asyncio.run(f.act("chime", "1234")) == "one action at a time"
    f.last_action -= evl.ACTION_GAP_S
    assert asyncio.run(f.act("chime", "1234")) is None


def test_act_stops_when_the_panel_rejects_a_key(rows):
    f = connected(ack="02")
    assert "didn't accept" in asyncio.run(f.act("chime", "1234"))
    assert f.writer.keys == ["1"] and rows == []


def test_act_stops_cleanly_when_the_session_drops_between_keys(rows):
    async def go():
        f = connected(delay=0.01)
        task = asyncio.create_task(f.act("disarm", "1234"))
        await asyncio.sleep(0.015)  # after the first key's ack
        panel, f.writer = f.writer, None
        return panel, await task
    panel, err = asyncio.run(go())
    assert err == "lost the connection to the alarm panel"
    assert len(panel.keys) < 5 and rows == []


class Reader:
    def __init__(self, raw):
        self.stream = asyncio.StreamReader()
        self.stream.feed_data(raw)
        self.stream.feed_eof()


class Writer:
    def __init__(self):
        self.out = b""

    def write(self, b):
        self.out += b

    async def drain(self):
        pass

    def close(self):
        pass


def request(flume, raw):
    async def go():
        w = Writer()
        await evl.ActionServer(flume).handle(Reader(raw).stream, w)
        head, _, body = w.out.partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(body)
    return asyncio.run(go())


def post(body):
    return b"POST /action HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body)


@pytest.mark.parametrize("raw", [
    post(b"[1, 2]"), post(b"7"), post(b"not json"),
    b"POST /action HTTP/1.1\r\nContent-Length: 50\r\n\r\n{}",  # body cut short
    b"garbage\r\n\r\n",
])
def test_server_answers_bad_requests_with_400(rows, raw):
    assert request(connected(), raw)[0] == 400


def test_server_status_and_action(rows):
    f = connected()
    f.store("%00,01,1C08,08,00,DISARMED$")
    assert request(f, b"GET /status HTTP/1.1\r\n\r\n") == (200, f.status())
    assert request(f, post(b'{"action": "chime", "code": "1234"}')) == (200, {"ok": True})
    assert request(f, post(b'{"action": "chime", "code": "1234"}')) == (409, {"error": "one action at a time"})
    assert request(f, b"GET /alarm/status HTTP/1.1\r\n\r\n")[0] == 404


def test_entry_delay_lasts_from_the_disarm_prompt_until_a_disarm_or_an_alarm(rows):
    f = evl.Flume(None, "host", "password")
    f.writer = object()
    seen = []
    for screen in ["0C0C,08,00,ARMED ***AWAY***** ALL SECURE **",
                   "0C0C,08,05,DISARM SYSTEM   or alarm occurs ",
                   "0C0C,10,00,FAULT 10 FRONT DOOR",  # another screen in the countdown
                   "1C08,08,01,****DISARMED****  Ready to Arm  ",
                   "8C08,08,05,DISARM SYSTEM   or alarm occurs ",
                   "800B,10,00,ALARM 10 FRONT  DOOR            ",
                   "000C,60,05,ARMED ***AWAY***May Exit Now  60"]:  # the exit delay isn't one
        f.store(f"%00,01,{screen}$")
        seen.append(f.status()["entry_delay"])
    assert seen == [False, True, True, False, True, False, False]
