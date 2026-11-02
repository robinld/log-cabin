import asyncio
import json

import pytest
import websockets

from home_events import db
from home_events.flumes import tempest

OBS = {"type": "obs_st", "device_id": 104829, "source": "mqtt",
       "obs": [[1793639940, 0.5, 1.3, 2.1, 275, 3, 1012.4, 9.8, 81, 1200, 0.4, 12, 0.0, 0,
                0, 0, 2.65, 1]]}


@pytest.fixture
def rows(monkeypatch):
    out = []
    monkeypatch.setattr(db, "append", lambda conn, **kw: out.append(kw))
    return out


def test_tempest_devices_are_the_st_devices_of_every_station():
    stations = {"stations": [{"devices": [{"device_id": 1, "device_type": "HB"},
                                          {"device_id": 2, "device_type": "ST"}]},
                             {"devices": [{"device_id": 3, "device_type": "ST"}]}]}
    assert tempest.tempest_devices(stations) == [2, 3]


def test_store_keeps_readings_at_their_own_time_and_skips_replays_and_acks(rows):
    f = tempest.Flume(None, "token")
    assert not f.store({"type": "ack", "id": "x"})
    assert f.store(OBS)
    assert not f.store({**OBS, "source": "cache"})  # replayed on the next listen_start
    assert f.store({"type": "evt_strike", "device_id": 104829, "evt": [1793640000, 12, 3848]})
    assert [(r["event_type"], r["device_id"], r["ts_ms"]) for r in rows] == [
        ("obs_st", "104829", 1793639940000), ("evt_strike", "104829", 1793640000000)]


def test_session_listens_to_each_device_and_stores_what_arrives(rows, monkeypatch):
    sent = []

    async def station(ws):
        await ws.send(json.dumps({"type": "connection_opened"}))
        sent.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "ack", "id": sent[0]["id"]}))
        await ws.send(json.dumps(OBS))

    async def run():
        async with websockets.serve(station, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(tempest, "WS_URL", f"ws://127.0.0.1:{port}")
            f = tempest.Flume(None, "token")
            f.devices, f.stations_at = [104829], 1e12
            with pytest.raises(websockets.ConnectionClosed):
                await f.session()

    asyncio.run(run())
    assert sent[0]["type"] == "listen_start" and sent[0]["device_id"] == 104829
    assert [r["event_type"] for r in rows] == ["obs_st"]
