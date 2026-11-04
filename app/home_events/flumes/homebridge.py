"""Homebridge, as HomeKit sees it -> SQLite. Read-only.

Homebridge tells every subscribed HomeKit controller about each change any of
its plugins makes: a plug switched, a leak detected, a door opened, and the
changes made from the Home app. This connects as one more controller, the way
a Home hub does, and stores them all, whichever plugin they came from. It
only reads and subscribes; it never sets a value.

Its pairing is a non-admin one that home_events/homebridge.py adds to
Homebridge's own pairing file at install, with the key in CONTROLLER.

Rows: event_type "accessories" with Homebridge's accessory list as received
(current values included) on each connection, then one row per change, with
device_id the accessory's aid, event_type its service, event_name the
characteristic (e.g. Outlet / On) and body the change as received.
Characteristics that can be read are stored only when their value changes:
plugins re-send unchanged values, and HomeKit doesn't act on those.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import pyhap
from cryptography.hazmat.primitives.asymmetric import ed25519

from .. import config, db
from ..hap_client import HapError, Pairing, Session

log = logging.getLogger("homebridge")

CONTROLLER = Path("/data/homebridge-controller.json")
HOST = "127.0.0.1"
REFRESH_S = 24 * 3600  # reconnect, storing a fresh accessory list, this often
PING_S = 600  # this long without an event, check the session with a read
NOT_SET_UP_WAIT_S = 3600
MIN_BACKOFF_S, MAX_BACKOFF_S = 5, 300
APPLE_SUFFIX = "-0000-1000-8000-0026BB765291"


def _type_names():
    """HAP type UUIDs -> names (Outlet, On), from HAP-python's definitions."""
    names = {}
    for kind in ("services", "characteristics"):
        for name, d in json.loads((Path(pyhap.__file__).parent / "resources" / f"{kind}.json").read_text()).items():
            names[d["UUID"].upper()] = name
    return names


TYPE_NAMES = _type_names()


def type_name(t):
    """A type as HAP sends it ("47", or a full UUID) -> its name, or the UUID."""
    t = str(t).upper()
    if len(t) <= 8:
        t = t.rjust(8, "0") + APPLE_SUFFIX
    return TYPE_NAMES.get(t, t)


def characteristics(accessories):
    """(aid, iid) -> (service, characteristic, perms, value) for every characteristic."""
    out = {}
    for a in accessories.get("accessories", []):
        for s in a.get("services", []):
            for c in s.get("characteristics", []):
                out[(a["aid"], c["iid"])] = (type_name(s["type"]), type_name(c["type"]),
                                             c.get("perms", []), c.get("value"))
    return out


def load_pairing(path=CONTROLLER):
    d = json.loads(path.read_text())
    return Pairing(d["id"], ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(d["ltsk"])),
                   d["accessory_id"], bytes.fromhex(d["accessory_ltpk"])), d["port"]


PARSE_VERSION = 1


def parse(event_type, event_name, body):
    """A change -> its value; the accessory list -> how many accessories."""
    msg = json.loads(body)
    if event_type == "accessories":
        return {"accessories": len(msg.get("accessories", []))}
    return {"value": msg.get("value")}


class Flume:
    def __init__(self, conn, pairing, port, host=HOST):
        self.conn, self.pairing, self.port, self.host = conn, pairing, port, host
        self.chars = {}
        self.last = {}  # (aid, iid) -> the value last read or stored

    def learn(self, accessories):
        """Remember what each characteristic is and its value; return the ones that send events."""
        self.chars = characteristics(accessories)
        self.last = {k: v[3] for k, v in self.chars.items() if "pr" in v[2]}
        return [k for k, v in self.chars.items() if "ev" in v[2]]

    def store(self, change):
        key = (change.get("aid"), change.get("iid"))
        service, name, perms, _ = self.chars.get(key, ("unknown", "unknown", [], None))
        value = change.get("value")
        if "pr" in perms and key in self.last and self.last[key] == value:
            return False
        self.last[key] = value
        db.append(self.conn, source="homebridge", event_type=service, event_name=name,
                  device_id=str(key[0]), body=change)
        return True

    async def session(self):
        s = await Session.open(self.host, self.port, self.pairing)
        try:
            accessories = await s.accessories()
            db.append(self.conn, source="homebridge", event_type="accessories", event_name=None,
                      body=accessories)
            ids = self.learn(accessories)
            failed = await s.subscribe(ids)
            if failed:
                bad = [c for c in failed.get("characteristics", []) if c.get("status")]
                if bad:
                    log.warning("couldn't subscribe to %d characteristics: %s", len(bad), bad[:5])
            log.info("subscribed to %d characteristics on %d accessories", len(ids),
                     len(accessories.get("accessories", [])))
            readable = next((k for k, v in self.chars.items() if "pr" in v[2]), None)
            started = time.time()
            while time.time() - started < REFRESH_S:
                try:
                    event = await s.next_event(PING_S)
                except TimeoutError:
                    if readable:
                        await s.request("GET", "/characteristics?id=%d.%d" % readable)
                    continue
                new = False
                for change in event.get("characteristics", []):
                    new |= (change.get("aid"), change.get("iid")) not in self.chars
                    self.store(change)
                if new:
                    log.info("a characteristic we didn't know about; reading the accessory list again")
                    return
        finally:
            await s.close()

    async def run_forever(self):
        backoff = MIN_BACKOFF_S
        while True:
            started = time.time()
            try:
                await self.session()
                continue  # due a refresh: reconnect at once
            except (HapError, OSError, TimeoutError, ValueError, KeyError) as e:
                log.warning("session ended: %s", e or type(e).__name__)
            if time.time() - started > 600:
                backoff = MIN_BACKOFF_S  # it was up a while; this is a fresh failure
            log.info("reconnecting in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


async def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    if not CONTROLLER.exists():
        # install.sh writes it once Homebridge is paired with Home.
        log.info("not set up: no %s yet; it's written by install.sh once Homebridge is paired", CONTROLLER)
        await asyncio.sleep(NOT_SET_UP_WAIT_S)
        raise SystemExit(0)
    pairing, port = load_pairing()
    await Flume(conn, pairing, port).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
