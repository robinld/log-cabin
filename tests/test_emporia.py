from home_events import db
from home_events.flumes import emporia


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class Vue:
    def __init__(self, bodies):
        self.paths, self.bodies = [], bodies
        self.auth = self

    def request(self, method, path):
        self.paths.append(path)
        return Response(self.bodies[path.split("?")[0]])


def test_device_gids_include_nested_devices():
    devices = {"devices": [{"deviceGid": 1, "devices": [{"deviceGid": 2}]}, {"deviceGid": 3}]}
    assert emporia.device_gids(devices) == [1, 2, 3]


def test_windows_end_on_the_quarter_hour():
    assert emporia.next_window_end(1793639940) == 1793640600  # 17:19 -> 17:30 UTC
    assert emporia.next_window_end(1793640600) == 1793641500


def test_usage_is_stored_as_received_at_the_windows_end(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "append", lambda conn, **kw: rows.append(kw))
    usage = {"deviceListUsages": {"devices": [{"deviceGid": 1, "channelUsages": []}]}}
    vue = Vue({"customers/devices": {"devices": [{"deviceGid": 1}, {"deviceGid": 7}]},
               "AppAPI": usage})
    f = emporia.Flume(None, vue)
    f.refresh_devices()
    f.store_usage(1793640600)
    assert vue.paths[1] == ("AppAPI?apiMethod=getDeviceListUsages&deviceGids=1+7"
                            "&instant=2026-11-02T17:30:00Z&scale=15MIN&energyUnit=KilowattHours")
    assert [(r["event_type"], r.get("ts_ms")) for r in rows] == [("devices", None),
                                                                ("usage", 1793640600000)]
    assert rows[1]["body"] == usage
