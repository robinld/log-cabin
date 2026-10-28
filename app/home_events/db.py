"""SQLite storage. One file, WAL mode, every commit fsynced.

Run as a module to create or upgrade the schema:  python -m home_events.db
"""

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,          -- yolink | yolink_poll
    event_type  TEXT NOT NULL,          -- e.g. DoorSensor.Alert, person, motion
    ts          TEXT NOT NULL,          -- when it happened, ISO-8601 UTC
    received_at TEXT NOT NULL,          -- when we stored it, ISO-8601 UTC
    device_id   TEXT,
    device_name TEXT,
    summary     TEXT,                   -- short human-readable line
    dedupe_key  TEXT NOT NULL UNIQUE,
    payload     TEXT NOT NULL           -- raw JSON as received
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_device_ts ON events(device_id, ts);
CREATE INDEX IF NOT EXISTS events_received ON events(received_at);

CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    name       TEXT,
    type       TEXT,
    model      TEXT,
    updated_at TEXT NOT NULL
);

-- Last polled state per device, used to turn polls into change events.
CREATE TABLE IF NOT EXISTS device_state (
    device_id TEXT PRIMARY KEY,
    state     TEXT NOT NULL,
    polled_at TEXT NOT NULL
);

-- Each long-running service bumps its row every minute.
CREATE TABLE IF NOT EXISTS heartbeats (
    service TEXT PRIMARY KEY,
    at      TEXT NOT NULL,
    detail  TEXT
);

-- Every line the EnvisaLink sends, as sent. Everything else about the alarm
-- (zones, intervals, troubles) is derived by the views below.
CREATE TABLE IF NOT EXISTS alarm_messages (
    id          INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,          -- ISO-8601 UTC; the EVL sends no timestamps
    code        TEXT NOT NULL,          -- %00 keypad, %01 open zones, %02 partitions, %03 CID, %FF zone timers
    data        TEXT NOT NULL           -- between "%CC," and "$"
);
CREATE INDEX IF NOT EXISTS alarm_messages_code_at ON alarm_messages(code, received_at);
"""

# Views are recreated on every init so edits here take effect on deploy.
VIEWS = """
DROP VIEW IF EXISTS alarm_keypad;
CREATE VIEW alarm_keypad AS
-- %00,PP,LLLL,NN,BB,<32 chars of keypad text>. The text can hold commas.
WITH f1 AS (
    SELECT id, received_at, data || ',' AS r FROM alarm_messages WHERE code = '%00'
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
           substr(m.data, (n.zone - 1) * 4 + 1, 2) AS lo,
           substr(m.data, (n.zone - 1) * 4 + 3, 2) AS hi
    FROM alarm_messages m, n WHERE m.code = '%FF'
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
       'zone_timer' AS via, id AS message_id
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

DROP VIEW IF EXISTS alarm_zones;
CREATE VIEW alarm_zones AS
-- Zone names as the keypad last showed them.
SELECT zone, name, max(received_at) AS last_named
FROM alarm_keypad_zones WHERE name <> ''
GROUP BY zone;

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
    conn.executescript(VIEWS)


def insert_event(conn, *, source, event_type, ts_ms, dedupe_key, payload,
                 device_id=None, device_name=None, summary=None):
    """Insert one event. Returns False if it was a duplicate."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO events
           (source, event_type, ts, received_at, device_id, device_name,
            summary, dedupe_key, payload)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, event_type, iso(ts_ms), iso(), device_id, device_name,
         summary, dedupe_key, json.dumps(payload, separators=(",", ":"))),
    )
    return cur.rowcount == 1


def upsert_device(conn, *, device_id, source, name, type_, model):
    conn.execute(
        """INSERT INTO devices (device_id, source, name, type, model, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(device_id) DO UPDATE SET
             name=excluded.name, type=excluded.type, model=excluded.model,
             updated_at=excluded.updated_at""",
        (device_id, source, name, type_, model, iso()),
    )


def insert_alarm_message(conn, code, data):
    conn.execute("INSERT INTO alarm_messages (received_at, code, data) VALUES (?, ?, ?)",
                 (iso(), code, data))


def heartbeat(conn, service, detail=None):
    conn.execute(
        """INSERT INTO heartbeats (service, at, detail) VALUES (?, ?, ?)
           ON CONFLICT(service) DO UPDATE SET at=excluded.at, detail=excluded.detail""",
        (service, iso(), detail),
    )


if __name__ == "__main__":
    c = connect()
    init(c)
    print("schema ok:", os.environ["DB_PATH"])
