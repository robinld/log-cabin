import json

import pytest

from home_events import db, parsing, reparse


def parsed(source, event_type, event_name, body):
    text, version = parsing.parse(source, event_type, event_name,
                                  body if isinstance(body, str) else json.dumps(body))
    assert version == 1
    return None if text is None else json.loads(text)


# Samples are real messages, trimmed, with ids and addresses changed.

def test_yolink_reports_and_polls_give_the_same_fields():
    report = {"event": "LeakSensor.Alert", "time": 1793651047509, "deviceId": "d88b4c0400000001", "data": {
        "sensorMode": "WaterLeak", "state": "alert", "battery": 4, "devTemperature": 21,
        "alarmState": {"stayError": False, "detectorError": False, "reminder": True},
        "loraInfo": {"netId": "010204", "signal": -34, "gateways": 2}, "stateChangedAt": 1793645348547}}
    assert parsed("yolink", "LeakSensor", "Alert", report) == {
        "state": "alert", "device_temperature_c": 21, "mode": "WaterLeak", "battery": 4,
        "alarms": ["reminder"], "signal_dbm": -34, "gateways": 2,
        "state_changed_at": "2026-11-02T18:49:08.547Z"}
    poll = {"online": True, "deviceId": "d88b4c0100000002", "state": {
        "state": "alert", "temperature": 19.9, "humidity": 59.2, "battery": 2, "mode": "f",
        "alarm": {"lowBattery": False, "highTemp": True, "code": 4}}}
    assert parsed("yolink", "THSensor", "getState", poll) == {
        "state": "alert", "online": True, "temperature_c": 19.9, "humidity_pct": 59.2,
        "battery": 2, "alarms": ["highTemp"]}
    # A Switch's poll is the state itself; -1 is mains power, not a battery.
    switch = {"state": "closed", "battery": -1, "loraInfo": {"signal": -23, "gateways": 1}}
    assert parsed("yolink", "Switch", "getState", switch) == {"state": "closed", "signal_dbm": -23, "gateways": 1}
    assert parsed("yolink", "Home", "getDeviceList", {"devices": [{}, {}]}) == {"devices": 2}


def test_evl_keypad_zones_partitions_and_contact_id():
    assert parsed("evl", "keypad", "CHECK", "%00,01,0208,16,00,CHECK 16 1ST    DOWNSTAIRS GARAG$") == {
        "text": "CHECK 16 1ST DOWNSTAIRS GARAG", "status": "disarmed, not ready", "partition": 1,
        "lights": ["ac_present", "trouble"], "zone": 16}
    assert parsed("evl", "keypad", "ARMED", "%00,01,8008,08,00,ARMED ***STAY***May Exit Now  15$")["status"] \
        == "exit delay"
    assert parsed("evl", "keypad", "DISARMED", "%00,01,1C08,08,00,****DISARMED****  Ready to Arm  $") == {
        "text": "****DISARMED**** Ready to Arm", "status": "disarmed, ready", "partition": 1,
        "lights": ["ac_present", "ready"]}
    assert parsed("evl", "zone_state", None, "%01,0440000000000000$") == {"open_zones": [3, 15]}
    assert parsed("evl", "partition_state", None, "%02,0900000000000000$") == {"partitions": {"1": "alarm in memory"}}
    # Matches the keypad: zone 16 showed CHECK.
    assert parsed("evl", "cid", "1381010160", "%03,1381010160$") == {
        "event": "lost supervision (RF)", "code": 381, "restore": False, "partition": 1, "zone_or_user": 16}
    assert parsed("evl", "cid", "3441010020", "%03,3441010020$")["restore"] is True
    # Zone 1 open; zone 2 faulted 10 s ago (0xFFFD, little-endian).
    assert parsed("evl", "zone_timers", None, "%FF,FFFFFDFF0000$") == {
        "open_zones": [1], "faulted_s_ago": {"2": 10}}
    assert parsed("evl", "action", "chime", "Chime toggled from HomeKit") == {
        "action": "chime", "text": "Chime toggled from HomeKit"}


def test_unifi_cef_fields_and_detections():
    line = ('Nov 03 19:27:52 UDM Pro CEF:0|Ubiquiti|UniFi Protect|7.3.70|2161|smartDetectZone|3|'
            'UNIFIcategory=detection eventId=a49a eventType=smartDetectZone category=detection severity=3 '
            'timestamp=1793762853878 msg="Upper Doorbell has recorded a person."')
    assert parsed("unifi", "UniFi Protect", "smartDetectZone", line) == {
        "product": "UniFi Protect", "version": "7.3.70", "event": "smartDetectZone", "severity": 3,
        "category": "detection", "eventId": "a49a", "timestamp": "2026-11-04T03:27:33.878Z",
        "msg": "Upper Doorbell has recorded a person.", "camera": "Upper Doorbell", "detected": "person"}
    # UniFi OS puts msg last and unquoted, and its values have spaces.
    os_line = ('Nov 04 03:19:31 2026-11-04T03:19:31.321Z UDM Pro CEF:0|Ubiquiti|UniFi OS|5.1.34|1000|'
               'Admin Accessed UniFi OS|1|UNIFIdeviceName=UDM Pro UNIFIadmin=Pat Example '
               'msg=Pat Example accessed the UniFi OS. Source IP: 192.0.2.8')
    out = parsed("unifi", "UniFi OS", "Admin Accessed UniFi OS", os_line)
    assert (out["deviceName"], out["admin"], out["msg"]) == (
        "UDM Pro", "Pat Example", "Pat Example accessed the UniFi OS. Source IP: 192.0.2.8")
    ring = parsed("unifi", "UniFi Protect", "ring", line.replace("has recorded a person", "x").replace(
        'msg="Upper Doorbell x."', 'msg="Someone is ringing Upper Doorbell."'))
    assert ring["camera"] == "Upper Doorbell" and "detected" not in ring
    assert parsed("unifi", "syslog", None, "plain syslog line") == {"message": "plain syslog line"}


def test_lutron_zone_levels_and_lists():
    level = {"CommuniqueType": "ReadResponse", "Header": {"MessageBodyType": "OneZoneStatus", "StatusCode": "200 OK",
             "Url": "/zone/9/status/level"}, "Body": {"ZoneStatus": {"href": "/zone/9/status", "Level": 0,
             "SwitchedLevel": "Off", "StatusAccuracy": "Good"}}}
    assert parsed("lutron", "OneZoneStatus", "/zone/9/status/level", level) == {
        "zone": "zone/9", "level_pct": 0, "switched": "Off", "accuracy": "Good"}
    assert parsed("lutron", "MultipleDeviceDefinition", "/device", {"Body": {"Devices": [{}, {}, {}]}}) == {"devices": 3}
    assert parsed("lutron", "unknown", "/zone/status", {"Header": {"StatusCode": "204 NoContent"}}) == {
        "status": "204 NoContent"}


def test_lennox_zones_alerts_and_wifi_without_its_password():
    zones = {"MessageType": "PropertyChange", "Data": {"zones": [{"id": 0, "status": {
        "temperature": 68, "temperatureC": 20, "humidity": 58, "tempOperation": "off", "fan": False,
        "period": {"systemMode": "heat and cool", "hsp": 60, "csp": 76, "hspC": 15.5}}}]}}
    assert parsed("lennox", "PropertyChange", "zones", zones) == {"sections": ["zones"], "zones": {"0": {
        "temperature_f": 68, "humidity_pct": 58, "operation": "off", "fan": False,
        "mode": "heat and cool", "heat_setpoint_f": 60, "cool_setpoint_f": 76}}}
    alerts = {"Data": {"alerts": {"meta": {"numActiveAlerts": 0}, "active": [{"id": 0, "alert": {
        "code": 312, "userMessage": "Problem", "priority": "info", "isStillActive": False,
        "timestampFirst": "1793779062"}}]}}}
    assert parsed("lennox", "PropertyChange", "alerts", alerts)["alerts"] == [
        {"code": 312, "message": "Problem", "priority": "info", "active": False, "first": "2026-11-04T07:57:42.000Z"}]
    wifi = {"Data": {"interfaces": [{"Info": {"status": {"ssid": "home", "ip": "192.0.2.6", "rssi": -44},
                                              "APDetails": {"password": "secret"}}}]}}
    out = parsed("lennox", "PropertyChange", "interfaces", wifi)
    assert out["wifi"] == {"ssid": "home", "ip": "192.0.2.6", "rssi": -44} and "secret" not in json.dumps(out)
    assert parsed("lennox", "PropertyChange", None, {"MessageType": "PropertyChange"}) is None


def test_homebridge_tempest_and_emporia():
    assert parsed("homebridge", "Outlet", "On", {"aid": 15, "iid": 11, "value": 1}) == {"value": 1}
    obs = {"type": "obs_st", "obs": [[1793640000, 0.1, 1.2, 2.5, 180, 3, 1012.3, 11.5, 81, 0, 0, 0, 0.0, 0, 0, 0, 2.6, 1]]}
    out = parsed("tempest", "obs_st", "mqtt", obs)
    assert (out["air_temp_c"], out["humidity_pct"], out["wind_gust_ms"], out["battery_v"]) == (11.5, 81, 2.5, 2.6)
    assert parsed("tempest", "evt_strike", None, {"evt": [1793640000, 27, 3848]}) == {"distance_km": 27, "energy": 3848}
    usage = {"deviceListUsages": {"devices": [{"channelUsages": [{"name": "Main", "usage": 0.51234, "channelNum": "1,2,3"},
                                                                 {"name": None, "usage": 0.1, "channelNum": "4"}]}]}}
    assert parsed("emporia", "usage", "15MIN", usage) == {"kwh": {"Main": 0.5123, "4": 0.1}}


def test_sources_without_a_flume_or_parse_are_stored_unparsed():
    assert parsing.parse("nowhere", "x", None, "{}") == (None, None)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init(c)
    return c


def test_append_stores_parsed_and_a_failing_parser_still_stores_the_row(conn, monkeypatch):
    db.append(conn, source="homebridge", event_type="Outlet", event_name="On", body={"aid": 1, "iid": 2, "value": 1})
    monkeypatch.setitem(parsing._parsers, "homebridge", (lambda *a: 1 / 0, 1))
    db.append(conn, source="homebridge", event_type="Outlet", event_name="On", body={"aid": 1, "iid": 2, "value": 0})
    rows = [tuple(r) for r in conn.execute("select parsed, parser from log order by id")]
    assert rows == [('{"value":1}', 1), (None, None)]


def test_reparse_redoes_older_versions_and_failures(conn, monkeypatch):
    for v in (1, 0):
        db.append(conn, source="homebridge", event_type="Outlet", event_name="On", body={"value": v})
    monkeypatch.setitem(parsing._parsers, "homebridge", (lambda *a: {"v": 2}, 2))
    conn.execute("update log set parser = null, parsed = null where id = 2")  # as if it had failed
    assert reparse.reparse(conn, "homebridge") == (2, 0, None)
    assert [tuple(r) for r in conn.execute("select parsed, parser from log")] == [('{"v":2}', 2)] * 2
    assert reparse.reparse(conn, "homebridge") == (0, 0, None)  # nothing older left
    monkeypatch.setitem(parsing._parsers, "homebridge", (lambda *a: 1 / 0, 3))
    done, failed, error = reparse.reparse(conn, "homebridge")
    assert (done, failed) == (0, 2) and "ZeroDivisionError" in error


def test_init_adds_the_parsed_columns_to_an_older_database(tmp_path):
    path = str(tmp_path / "old.db")
    old = db.connect(path)
    old.execute("CREATE TABLE log (id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, ts TEXT NOT NULL, "
                "source TEXT NOT NULL, event_type TEXT NOT NULL, event_name TEXT, device_id TEXT, body TEXT NOT NULL)")
    old.execute("INSERT INTO log VALUES (1, 'a', 'a', 'evl', 'zone_state', NULL, NULL, '%01,04$')")
    db.init(old)
    assert reparse.reparse(old, "evl") == (1, 0, None)
    assert old.execute("select parsed from log").fetchone()[0] == '{"open_zones":[3]}'
