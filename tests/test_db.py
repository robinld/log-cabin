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
