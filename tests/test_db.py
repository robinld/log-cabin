from home_events import db
from home_events.flumes.evl import evl_fields


def test_alarm_zones_takes_each_zones_newest_name_from_the_keypad():
    conn = db.connect(":memory:")
    db.init(conn)
    for line in ["%00,01,0008,16,00,FAULT 16 1ST    DOWNSTAIRS GARAG$",
                 "%00,01,1C08,08,00,****DISARMED****  Ready to Arm  $",
                 "%00,01,0208,16,00,CHECK 16 1ST    DOWNSTAIRS GARAG$",
                 "%00,01,0008,19,00,FAULT 19 BEDROOM3 DOOR          $",
                 "%00,01,0008,10,00,FAULT 10 FRONT  DOOR            $",
                 "%00,01,0008,10,00,FAULT 10 FRONT DOOR RENAMED     $"]:
        event_type, event_name, zone = evl_fields(line)
        db.append(conn, source="evl", event_type=event_type, event_name=event_name,
                  device_id=zone, body=line)
    assert [tuple(r) for r in conn.execute("select zone, name from alarm_zones order by zone")] == [
        (10, "FRONT DOOR RENAMED"), (16, "1ST DOWNSTAIRS GARAG"), (19, "BEDROOM3 DOOR")]


def test_weather_and_power_views_read_tempest_and_emporia_rows():
    conn = db.connect(":memory:")
    db.init(conn)
    db.append(conn, source="tempest", event_type="obs_st", event_name="mqtt", device_id="104829",
              ts_ms=1793639940000, body={"type": "obs_st", "obs": [
                  [1793639940, 0.5, 1.3, 2.1, 275, 3, 1012.4, 9.8, 81, 1200, 0.4, 12, 0.2, 1,
                   0, 0, 2.65, 1]]})
    db.append(conn, source="emporia", event_type="usage", event_name="15MIN", ts_ms=1793640600000,
              body={"deviceListUsages": {"devices": [{"deviceGid": 1, "channelUsages": [
                  {"name": "Heater", "channelNum": "4", "usage": 0.5}]}]}})
    w = conn.execute("select air_temp_c, humidity_pct, wind_gust_ms, rain_mm from weather").fetchone()
    assert tuple(w) == (9.8, 81, 2.1, 0.2)
    p = conn.execute("select until, name, kwh, avg_kw from power").fetchone()
    assert tuple(p) == ("2026-11-02T17:30:00.000Z", "Heater", 0.5, 2.0)
