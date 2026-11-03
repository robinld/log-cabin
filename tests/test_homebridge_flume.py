import asyncio
import json
import uuid

import pytest

pytest.importorskip("pyhap")
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
from pyhap.accessory import Accessory, Bridge  # noqa: E402
from pyhap.accessory_driver import AccessoryDriver  # noqa: E402

from home_events import db, hap_client  # noqa: E402
from home_events.flumes import homebridge as hb  # noqa: E402


def test_tlv_round_trip_splits_long_values():
    data = hap_client.tlv_encode((5, b"x" * 300), (6, b"\x01"), (1, b""))
    assert data[:2] == b"\x05\xff" and hap_client.tlv_decode(data) == {5: b"x" * 300, 6: b"\x01", 1: b""}


def test_parse_http_waits_for_the_whole_message():
    msg = b"EVENT/1.0 200 OK\r\nContent-Length: 5\r\n\r\nhello" + b"HTTP/1.1 204 No Content\r\n\r\n"
    assert hap_client.parse_http(msg[:30]) is None
    m, rest = hap_client.parse_http(msg)
    assert (m.event, m.status, m.body) == (True, 200, b"hello")
    m, rest = hap_client.parse_http(rest)
    assert (m.event, m.status, m.body, rest) == (False, 204, b"", b"")
    chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
    assert hap_client.parse_http(chunked)[0].body == b"abcde"
    assert hap_client.parse_http(chunked[:-4]) is None


def test_type_names_take_short_and_full_uuids():
    assert hb.type_name("47") == "Outlet" and hb.type_name("25") == "On"
    assert hb.type_name("00000025-0000-1000-8000-0026bb765291") == "On"
    assert hb.type_name("ABCDEF01-0000-0000-0000-000000000000") == "ABCDEF01-0000-0000-0000-000000000000"


def test_store_skips_unchanged_readable_values_but_not_events():
    conn = db.connect(":memory:")
    db.init(conn)
    f = hb.Flume(conn, None, 0)
    f.learn({"accessories": [{"aid": 2, "services": [
        {"type": "47", "characteristics": [{"iid": 9, "type": "25", "perms": ["pr", "pw", "ev"], "value": False}]},
        {"type": "89", "characteristics": [{"iid": 12, "type": "73", "perms": ["ev"]}]}]}]})
    assert not f.store({"aid": 2, "iid": 9, "value": False})  # what the accessory list said
    assert f.store({"aid": 2, "iid": 9, "value": True})
    assert not f.store({"aid": 2, "iid": 9, "value": True})
    assert f.store({"aid": 2, "iid": 12, "value": 0}) and f.store({"aid": 2, "iid": 12, "value": 0})
    rows = conn.execute("select event_type, event_name, device_id, body from log").fetchall()
    assert [tuple(r) for r in rows] == [
        ("Outlet", "On", "2", '{"aid":2,"iid":9,"value":true}'),
        ("StatelessProgrammableSwitch", "ProgrammableSwitchEvent", "2", '{"aid":2,"iid":12,"value":0}'),
        ("StatelessProgrammableSwitch", "ProgrammableSwitchEvent", "2", '{"aid":2,"iid":12,"value":0}')]


@pytest.fixture
def server(tmp_path):
    """A HAP-python bridge with one outlet, "Heater", on 127.0.0.1, and a
    controller paired with it as a non-admin."""
    loop = asyncio.new_event_loop()
    driver = AccessoryDriver(address="127.0.0.1", port=0, persist_file=str(tmp_path / "state"),
                             interface_choice=["127.0.0.1"], loop=loop)
    bridge = Bridge(driver, "Homebridge")
    heater = Accessory(driver, "Heater")
    on = heater.add_preload_service("Outlet").configure_char("On", value=False)
    bridge.add_accessory(heater)
    driver.add_accessory(bridge)
    key = ed25519.Ed25519PrivateKey.generate()
    cid = str(uuid.uuid4()).upper()
    driver.state.add_paired_client(cid.encode(), hap_client.raw(key.public_key()), b"\x00")
    pairing = hap_client.Pairing(cid, key, driver.state.mac, hap_client.raw(driver.state.public_key))

    async def start():
        driver.aio_stop_event = asyncio.Event()
        await driver.http_server.async_start(loop)
        return driver.http_server.server.sockets[0].getsockname()[1]

    port = loop.run_until_complete(start())
    yield loop, pairing, port, on
    driver.http_server.async_stop()
    loop.close()


def test_flume_stores_the_accessory_list_and_each_change(server, tmp_path):
    loop, pairing, port, on = server
    conn = db.connect(str(tmp_path / "events.db"))
    db.init(conn)
    flume = hb.Flume(conn, pairing, port)

    def rows(sql):
        return [tuple(r) for r in conn.execute(sql)]

    async def until(check):
        for _ in range(100):
            if check():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("timed out")

    async def run():
        task = asyncio.create_task(flume.session())
        await until(lambda: rows("select 1 from log where event_type = 'accessories'") and flume.chars)
        await asyncio.sleep(0.2)  # subscribed
        on.set_value(True)
        await until(lambda: rows("select 1 from homebridge_changes"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    loop.run_until_complete(run())
    accessories = json.loads(rows("select body from log where event_type = 'accessories'")[0][0])
    assert {a["aid"] for a in accessories["accessories"]} == {1, 2}
    assert rows("select device_name, service, characteristic, value from homebridge_changes") == [
        ("Heater", "Outlet", "On", 1)]


def test_an_unpaired_controller_is_refused(server):
    loop, pairing, port, _ = server
    stranger = hap_client.Pairing(str(uuid.uuid4()).upper(), ed25519.Ed25519PrivateKey.generate(),
                                  pairing.accessory_id, pairing.accessory_ltpk)
    with pytest.raises(hap_client.HapError, match="M4"):
        loop.run_until_complete(hap_client.Session.open("127.0.0.1", port, stranger))


def test_a_wrong_accessory_key_is_refused(server):
    loop, pairing, port, _ = server
    impostor = hap_client.Pairing(pairing.controller_id, pairing.controller_key, pairing.accessory_id,
                                  hap_client.raw(ed25519.Ed25519PrivateKey.generate().public_key()))
    with pytest.raises(hap_client.HapError, match="signature"):
        loop.run_until_complete(hap_client.Session.open("127.0.0.1", port, impostor))
