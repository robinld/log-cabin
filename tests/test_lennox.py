import pytest

from home_events import db
from home_events.flumes import lennox


@pytest.fixture
def flume(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "append", lambda conn, **kw: rows.append(kw["body"]))
    f = lennox.Flume.__new__(lennox.Flume)  # no HTTP client needed
    f.conn, f.last_stored = None, {}
    return f, rows


def zone(zone_id, sp, msg_id=0):
    return {"MessageId": msg_id, "MessageType": "PropertyChange",
            "Data": {"zones": [{"id": zone_id, "status": {"period": {"sp": sp}}}]}}


def test_a_repeat_is_dropped_but_a_change_back_is_kept(flume):
    f, rows = flume
    for t, msg in enumerate([zone(0, 73), zone(0, 73, msg_id=5), zone(0, 70), zone(0, 73)]):
        f.store(msg, now=t)
    assert [r["Data"]["zones"][0]["status"]["period"]["sp"] for r in rows] == [73, 70, 73]


def test_messages_about_different_things_dont_hide_each_other(flume):
    f, rows = flume
    for t, msg in enumerate([zone(0, 73), zone(1, 68), zone(0, 73), zone(1, 68)]):
        f.store(msg, now=t)
    assert len(rows) == 2


def test_a_repeat_is_stored_again_after_the_window(flume):
    f, rows = flume
    f.store(zone(0, 73), now=0)
    f.store(zone(0, 73), now=lennox.REPEAT_EVERY_S - 1)
    f.store(zone(0, 73), now=lennox.REPEAT_EVERY_S + 1)
    assert len(rows) == 2


def test_wifi_statistics_are_kept_hourly_whatever_they_say(flume):
    f, rows = flume
    wifi = lambda rssi: {"MessageType": "PropertyChange", "Data": {"interfaces": [{"rssi": rssi}]}}
    for t, rssi in [(0, -50), (60, -60), (120, -50), (3601, -55)]:
        f.store(wifi(rssi), now=t)
    assert [r["Data"]["interfaces"][0]["rssi"] for r in rows] == [-50, -55]
