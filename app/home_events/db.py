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
