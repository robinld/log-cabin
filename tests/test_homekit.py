import asyncio
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("pyhap")
from pyhap.accessory_driver import AccessoryDriver  # noqa: E402

from home_events import homekit as hk  # noqa: E402

NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FakeActions:
    """The EVL flume's action server: a panel state, and the actions posted."""

    def __init__(self):
        self.panel = {"connected": True, "state": "disarmed", "ready": True, "bypass": False,
                      "chime": False}
        self.posts = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self.reply(outer.panel)

            def do_POST(self):
                outer.posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply({"ok": True})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    actions = FakeActions()
    monkeypatch.setattr(hk, "ACTIONS_URL", actions.url)
    monkeypatch.setattr(hk, "PENDING_S", 0.2)
    path = tmp_path / "events.db"
    c = sqlite3.connect(path)
    c.executescript("""
        create table log(id integer primary key, received_at, source, event_type, event_name,
                         device_id, body);
        create table alarm_zones(zone, name);
        insert into alarm_zones values (10, 'FRONT DOOR'), (16, '1ST DOWNSTAIRS GARAG'),
                                       (21, 'LIVING MOTION DETECTOR');""")
    c.commit()

    def add(event_type, body, event_name=None, device_id=None):
        c.execute("insert into log(received_at, source, event_type, event_name, device_id, body)"
                  " values (?, 'evl', ?, ?, ?, ?)", (NOW, event_type, event_name, device_id, body))
        c.commit()

    driver = AccessoryDriver(address="127.0.0.1", port=0, persist_file=str(tmp_path / "state"),
                             interface_choice=["127.0.0.1"])
    driver.add_job = lambda f, *a: f(*a)
    yield actions, add, lambda code="1234": hk.AlarmBridge(driver, "Log Cabin", str(path), code)
    actions.srv.shutdown()


def poll(bridge):
    inner = next(c.cell_contents for c in hk.AlarmBridge.run.__closure__
                 if callable(c.cell_contents))
    asyncio.run(inner(bridge))


def test_open_zones_decodes_the_zone_bitmask():
    assert hk.open_zones("%01,00800000$") == {16}
    assert hk.open_zones("%01,01000010$") == {1, 29}


def test_zone_kinds_and_names(setup):
    _, add, bridge = setup
    add("cid", "%03,1373010090$")  # fire trouble on a zone the keypad never named
    b = bridge()
    assert {z: (a.display_name, a.kind) for z, a in b.zones.items()} == {
        9: ("Fire Zone 9", "SmokeSensor"), 10: ("Front Door", "ContactSensor"),
        16: ("1st Downstairs Garag", "ContactSensor"), 21: ("Living Motion Detector", "MotionSensor")}


def test_arm_is_sent_and_springs_back_if_the_panel_never_arms(setup):
    actions, _, bridge = setup
    b = bridge()
    poll(b)
    b.alarm.target.client_update_value(hk.STAY)
    assert actions.posts == [{"action": "stay", "code": "1234"}]
    poll(b)
    assert b.alarm.target.value == hk.STAY  # still waiting
    time.sleep(0.25)
    poll(b)
    assert b.alarm.target.value == hk.DISARMED


def test_arm_refused_without_sending_when_not_ready(setup):
    actions, _, bridge = setup
    actions.panel["ready"] = False
    b = bridge()
    poll(b)
    assert b.alarm.not_ready.value == 1
    b.alarm.target.client_update_value(hk.AWAY)
    poll(b)
    assert actions.posts == [] and b.alarm.target.value == hk.DISARMED


def test_read_only_without_a_code(setup):
    actions, _, bridge = setup
    b = bridge(code=None)
    poll(b)
    b.chime.on.client_update_value(True)
    poll(b)
    assert actions.posts == [] and b.chime.on.value is False


def test_chime_refused_until_the_panel_state_is_known(setup):
    actions, _, bridge = setup
    b = bridge()
    b.chime.on.client_update_value(False)  # before any poll
    assert actions.posts == []
    poll(b)
    b.chime.on.client_update_value(True)
    assert actions.posts == [{"action": "chime", "code": "1234"}]


def test_lost_sensor_shows_open_until_heard_from_again(setup):
    actions, add, bridge = setup
    actions.panel["ready"] = False
    add("cid", "%03,1381010160$")  # RF supervision lost, zone 16, no restore ever sent
    b = bridge()
    poll(b)
    assert (b.zones[16].state.value, b.zones[16].fault.value) == (1, 1)
    add("keypad", "%00,01,0008,16,00,FAULT 16$", "FAULT", "16")
    poll(b)
    assert (b.zones[16].state.value, b.zones[16].fault.value) == (0, 0)


def test_ready_closes_every_zone_unless_one_is_bypassed(setup):
    actions, add, bridge = setup
    add("zone_state", "%01,00020000$")  # zone 10 open, as the EVL last said
    b = bridge()
    actions.panel["ready"] = False
    poll(b)
    assert b.zones[10].state.value == 1
    actions.panel.update(ready=True, bypass=True)
    poll(b)
    assert b.zones[10].state.value == 1
    actions.panel["bypass"] = False
    poll(b)
    assert b.zones[10].state.value == 0
