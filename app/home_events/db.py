"""SQLite storage. One file, WAL mode, every commit fsynced.

Everything goes in one append-only table, `log`: what arrived, as it arrived,
plus a few columns copied out of it for querying. Names, state changes,
health and the alarm's zones are all views, so they can change at any time
without touching stored data.

Run as a module to create or upgrade the schema:  python -m home_events.db
"""

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS log (
    id          INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,          -- when the box got it, ISO-8601 UTC
    ts          TEXT NOT NULL,          -- when it happened if the source says, else received_at
    source      TEXT NOT NULL,          -- yolink | evl | protect
    event_type  TEXT NOT NULL,          -- what kind of thing: DoorSensor, keypad, zone_timers
    event_name  TEXT,                   -- what happened: Alert, getState, FAULT
    device_id   TEXT,                   -- YoLink deviceId, alarm zone number
    body        TEXT NOT NULL           -- the message as received: JSON, or the EVL's raw line
);
CREATE INDEX IF NOT EXISTS log_ts ON log(ts);
CREATE INDEX IF NOT EXISTS log_source_type ON log(source, event_type, id);
CREATE INDEX IF NOT EXISTS log_device ON log(device_id, id);
"""

# Views are recreated on every init so edits here take effect on deploy.
VIEWS = """
DROP VIEW IF EXISTS device_names;
CREATE VIEW device_names AS
-- YoLink and Lutron names from their newest device lists; alarm zones as the keypad named them.
WITH latest AS (
    SELECT body FROM log
    WHERE source = 'yolink' AND event_name = 'getDeviceList'
    ORDER BY id DESC LIMIT 1
)
SELECT 'yolink' AS source, json_extract(d.value, '$.deviceId') AS device_id,
       json_extract(d.value, '$.name') AS name, json_extract(d.value, '$.type') AS type
FROM latest, json_each(latest.body, '$.devices') AS d
UNION ALL
SELECT 'evl', cast(zone AS text), name, 'zone' FROM alarm_zones
UNION ALL
-- Lutron zones (dimmers, switches) from the newest device list, as "Area Device".
SELECT 'lutron', substr(json_extract(z.value, '$.href'), 2),
       coalesce(json_extract(d.value, '$.FullyQualifiedName[0]') || ' '
                || json_extract(d.value, '$.FullyQualifiedName[1]'),
                json_extract(d.value, '$.Name')),
       json_extract(d.value, '$.DeviceType')
FROM (SELECT body FROM log WHERE source = 'lutron' AND event_name = '/device'
      ORDER BY id DESC LIMIT 1) AS latest_lutron,
     json_each(latest_lutron.body, '$.Body.Devices') AS d,
     json_each(d.value, '$.LocalZones') AS z;

DROP VIEW IF EXISTS events;
CREATE VIEW events AS
-- The log with device names, without the noisy periodic messages.
SELECT l.id, l.ts, l.source, l.event_type, l.event_name, l.device_id,
       n.name AS device_name, l.body
FROM log l LEFT JOIN device_names n ON n.source = l.source AND n.device_id = l.device_id
WHERE NOT (l.source = 'yolink' AND coalesce(l.event_name, '') IN ('getState', 'getDeviceList'))
  AND NOT (l.source = 'evl' AND l.event_type = 'zone_timers');

DROP VIEW IF EXISTS yolink_state_changes;
CREATE VIEW yolink_state_changes AS
-- Polled states that differ from the previous poll of the same device: what
-- MQTT should have reported. The first poll of each device is a baseline.
WITH s AS (
    SELECT id, ts, device_id, coalesce(json_extract(body, '$.state'), body) AS state
    FROM log WHERE source = 'yolink' AND event_name = 'getState'
), c AS (
    SELECT id, ts, device_id, state, lag(state) OVER (PARTITION BY device_id ORDER BY id) AS prev
    FROM s
)
SELECT c.id, c.ts, c.device_id, n.name AS device_name, c.prev IS NULL AS baseline, c.state
FROM c LEFT JOIN device_names n ON n.source = 'yolink' AND n.device_id = c.device_id
WHERE c.prev IS NULL OR c.state <> c.prev;

DROP VIEW IF EXISTS health;
CREATE VIEW health AS
-- The EVL answers a zone-timer request every 30 s and YoLink is polled every
-- 30 min, so a source whose newest row is old has stopped working.
SELECT source, max(received_at) AS last_received,
       cast(round((julianday('now') - julianday(max(received_at))) * 86400) AS integer) AS seconds_ago,
       count(*) AS rows
FROM log GROUP BY source;

DROP VIEW IF EXISTS alarm_keypad;
CREATE VIEW alarm_keypad AS
-- %00,PP,LLLL,NN,BB,<32 chars of keypad text>$. The text can hold commas.
WITH f1 AS (
    SELECT id, received_at, substr(body, 5, length(body) - 5) || ',' AS r
    FROM log WHERE source = 'evl' AND event_type = 'keypad'
), f2 AS (
    SELECT id, received_at, substr(r, 1, instr(r, ',') - 1) AS partition,
           substr(r, instr(r, ',') + 1) AS r FROM f1
), f3 AS (
    SELECT id, received_at, partition, substr(r, 1, instr(r, ',') - 1) AS leds,
           substr(r, instr(r, ',') + 1) AS r FROM f2
), f4 AS (
    SELECT id, received_at, partition, leds, substr(r, 1, instr(r, ',') - 1) AS numeric,
           substr(r, instr(r, ',') + 1) AS r FROM f3
), f5 AS (
    SELECT id, received_at, partition, leds, numeric,
           substr(r, 1, instr(r, ',') - 1) AS beeps,
           substr(r, instr(r, ',') + 1, length(r) - instr(r, ',') - 1) AS raw_text FROM f4
)
SELECT id, received_at, partition, leds, numeric, beeps, raw_text,
       -- The keypad's two 16-char lines, with runs of padding collapsed.
       trim(replace(replace(replace(replace(replace(raw_text,
           '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' ')) AS text
FROM f5;

DROP VIEW IF EXISTS alarm_keypad_zones;
CREATE VIEW alarm_keypad_zones AS
-- Screens that name a zone, e.g. "FAULT 11 1ST UPSTAIRS GARAGE".
WITH z AS (
    SELECT id, received_at, text, substr(text, 1, 5) AS kind,
           cast(substr(text, 7, 3) AS integer) AS zone
    FROM alarm_keypad
    WHERE substr(text, 1, 6) IN ('FAULT ', 'CHECK ', 'LOBAT ', 'BYPAS ', 'ALARM ')
)
SELECT id, received_at, kind, zone,
       trim(substr(text, 7 + length(cast(zone AS text)))) AS name
FROM z WHERE zone > 0;

DROP VIEW IF EXISTS alarm_zones;
CREATE VIEW alarm_zones AS
-- Zone names as the keypad last showed them.
SELECT zone, name, max(received_at) AS last_named
FROM alarm_keypad_zones WHERE name <> ''
GROUP BY zone;

DROP VIEW IF EXISTS alarm_zone_seen;
CREATE VIEW alarm_zone_seen AS
-- One row per time a zone was seen faulted: from FAULT screens, and from
-- %FF dumps (requested every 30 s) for zones faulted in the last 35 s. A
-- %FF value is a little-endian UINT16 per zone; 0xFFFF is open now and it
-- counts down one per 5 s since the zone was last faulted.
WITH RECURSIVE n(zone) AS (
    SELECT 1 UNION ALL SELECT zone + 1 FROM n WHERE zone < 64
), t AS (
    SELECT m.id, m.received_at, n.zone,
           substr(m.body, 5 + (n.zone - 1) * 4, 2) AS lo,
           substr(m.body, 7 + (n.zone - 1) * 4, 2) AS hi
    FROM log m, n WHERE m.source = 'evl' AND m.event_type = 'zone_timers'
), v AS (
    SELECT id, received_at, zone,
           (instr('0123456789ABCDEF', upper(substr(hi, 1, 1))) - 1) * 4096
         + (instr('0123456789ABCDEF', upper(substr(hi, 2, 1))) - 1) * 256
         + (instr('0123456789ABCDEF', upper(substr(lo, 1, 1))) - 1) * 16
         + (instr('0123456789ABCDEF', upper(substr(lo, 2, 1))) - 1) AS timer
    FROM t
)
SELECT zone, strftime('%Y-%m-%dT%H:%M:%fZ', received_at,
                      printf('-%d seconds', (65535 - timer) * 5)) AS seen_at,
       'zone_timer' AS via, id AS log_id
FROM v WHERE timer > 0 AND 65535 - timer <= 7
UNION ALL
SELECT zone, received_at, 'keypad', id FROM alarm_keypad_zones WHERE kind = 'FAULT';

DROP VIEW IF EXISTS alarm_zone_intervals;
CREATE VIEW alarm_zone_intervals AS
-- Sightings less than 45 s apart are one open interval. 45 s spans the 30 s
-- between zone-timer dumps, which keep reporting a zone that stays open.
-- A zone closes some time between last_seen and the next dump.
WITH s AS (
    SELECT zone, seen_at,
           CASE WHEN julianday(seen_at) - julianday(lag(seen_at) OVER w) <= 45.0 / 86400
                THEN 0 ELSE 1 END AS starts
    FROM alarm_zone_seen WINDOW w AS (PARTITION BY zone ORDER BY seen_at)
), g AS (
    SELECT zone, seen_at, sum(starts) OVER (PARTITION BY zone ORDER BY seen_at) AS grp FROM s
)
SELECT g.zone, z.name, min(seen_at) AS first_seen, max(seen_at) AS last_seen,
       count(*) AS sightings
FROM g LEFT JOIN alarm_zones z ON z.zone = g.zone
GROUP BY g.zone, grp;

DROP VIEW IF EXISTS alarm_troubles;
CREATE VIEW alarm_troubles AS
SELECT kind, zone, name, min(received_at) AS first_seen, max(received_at) AS last_seen,
       count(*) AS screens
FROM alarm_keypad_zones WHERE kind IN ('CHECK', 'LOBAT')
GROUP BY kind, zone
UNION ALL
SELECT 'SYSTEM', NULL, text, min(received_at), max(received_at), count(*)
FROM alarm_keypad
WHERE (text LIKE '%BAT%' OR text LIKE '%AC LOSS%' OR text LIKE '%FAIL%' OR text LIKE '%TRBL%')
  AND substr(text, 1, 6) NOT IN ('CHECK ', 'LOBAT ')
GROUP BY text;
"""

# Tables from before everything moved into `log` (v0.2.x).
OLD_TABLES = ("events", "devices", "device_state", "heartbeats", "alarm_messages")


def iso(ts_ms=None):
    """Milliseconds since epoch (default: now) -> ISO-8601 UTC string."""
    if ts_ms is None:
        ts_ms = time.time() * 1000
    t = time.gmtime(ts_ms / 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + f".{int(ts_ms % 1000):03d}Z"


def connect(path=None):
    conn = sqlite3.connect(path or os.environ["DB_PATH"], timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # FULL fsyncs the WAL on every commit, so an acknowledged write survives
    # a power cut. At a few events a minute the cost is irrelevant.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(conn):
    conn.executescript(SCHEMA)
    migrate_old_tables(conn)
    conn.executescript(VIEWS)


def append(conn, *, source, event_type, event_name, body, ts_ms=None, device_id=None):
    """Append one row. body is stored as given if it is a string, else as JSON."""
    if not isinstance(body, str):
        body = json.dumps(body, separators=(",", ":"))
    now = iso()
    conn.execute(
        """INSERT INTO log (received_at, ts, source, event_type, event_name, device_id, body)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (now, now if ts_ms is None else iso(ts_ms), source, event_type, event_name,
         device_id, body),
    )


def migrate_old_tables(conn):
    """Copy rows from the v0.2.x tables into `log`, then drop those tables.

    devices, device_state and heartbeats were derived, so they are dropped
    without copying: the YoLink flume re-fetches the device list on start.
    """
    from .flumes.evl import evl_fields

    conn.execute("BEGIN IMMEDIATE")
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not tables & set(OLD_TABLES):
            conn.execute("COMMIT")
            return
        rows = []
        if "events" in tables:
            for r in conn.execute("SELECT * FROM events"):
                if r["source"] == "yolink_poll":
                    etype, ename = r["event_type"].removesuffix(".poll_change"), "getState"
                    body = json.dumps(json.loads(r["payload"]).get("data"), separators=(",", ":"))
                else:
                    etype, _, ename = r["event_type"].partition(".")
                    body = r["payload"]
                rows.append((r["received_at"], r["ts"], "yolink", etype, ename or None,
                             r["device_id"], body))
        if "alarm_messages" in tables:
            for r in conn.execute("SELECT * FROM alarm_messages"):
                line = r["data"] if r["code"] == "?" else f"{r['code']},{r['data']}$"
                etype, ename, zone = evl_fields(line)
                rows.append((r["received_at"], r["received_at"], "evl", etype, ename, zone, line))
        rows.sort(key=lambda row: row[0])
        conn.executemany(
            """INSERT INTO log (received_at, ts, source, event_type, event_name, device_id, body)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", rows)
        for t in OLD_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("COMMIT")
        print(f"migrated {len(rows)} rows into log; dropped {', '.join(sorted(tables & set(OLD_TABLES)))}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


if __name__ == "__main__":
    c = connect()
    init(c)
    print("schema ok:", os.environ["DB_PATH"])
