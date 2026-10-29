"""EnvisaLink (Honeywell) TPI -> SQLite. Read-only.

Holds the one TPI session the EVL allows, on port 4025 over the WireGuard
tunnel, and appends every line it sends to the log table, as sent. Zones,
open intervals and troubles are derived at query time by the views in db.py.

The only command ever sent is ^02 (dump zone timers), every 30 s, which also
keeps the session alive. Never ^03: that is a keypress, and keypresses can
arm, disarm or program the panel.
"""

import asyncio
import logging
import time

from .. import config, db

log = logging.getLogger("evl")

PORT = 4025
DUMP_ZONE_TIMERS = b"^02,$\r\n"
DUMP_EVERY_S = 30
# The dump replies every 30 s, so this much silence means the session is dead.
IDLE_TIMEOUT_S = 120
MIN_BACKOFF_S, MAX_BACKOFF_S = 5, 300
# The keypad cycles through the same few screens every few seconds; store any
# given message at most this often. A message not seen in the last 30 s is
# always stored. Kept under the 45 s gap used by alarm_zone_intervals.
REPEAT_EVERY_S = 30

KINDS = {"%00": "keypad", "%01": "zone_state", "%02": "partition_state",
         "%03": "cid", "%FF": "zone_timers"}
ZONE_SCREENS = {"FAULT", "CHECK", "LOBAT", "BYPAS", "ALARM"}


def evl_fields(line):
    """A raw TPI line -> (event_type, event_name, zone) for the log columns.

    Keypad screens are named by their first word ("FAULT 13 DINING ..." ->
    FAULT, "****DISARMED**** Ready to Arm" -> DISARMED), with the zone for
    screens that name one. CID events are named by their event code.
    """
    if not (line.startswith("%") and line.endswith("$")):
        return "unknown", None, None
    kind = KINDS.get(line[:3], line[:3])
    data = line[4:-1]
    if kind == "keypad":
        words = data.split(",", 4)[-1].split()
        name = words[0].strip("*") if words else None
        zone = words[1] if name in ZONE_SCREENS and len(words) > 1 and words[1].isdigit() else None
        return kind, name or None, str(int(zone)) if zone else None
    if kind == "cid":
        return kind, data.split(",")[0], None
    return kind, None, None


class LoginFailed(Exception):
    pass


class Flume:
    def __init__(self, conn, host, password):
        self.conn, self.host, self.password = conn, host, password
        self.last_stored = {}  # line -> time stored

    def store(self, line):
        if line.startswith("^"):
            # Ack for our ^02, "^02,00$". Anything but 00 is an error code.
            if line[4:6] != "00":
                log.warning("EVL rejected a command: %s", line)
            return
        now = time.time()
        if now - self.last_stored.get(line, 0) < REPEAT_EVERY_S:
            return
        event_type, event_name, zone = evl_fields(line)
        db.append(self.conn, source="evl", event_type=event_type, event_name=event_name,
                  device_id=zone, body=line)
        # Forget anything older than the window, so this stays a handful of entries.
        self.last_stored = {k: t for k, t in self.last_stored.items() if now - t < REPEAT_EVERY_S}
        self.last_stored[line] = now

    async def readline(self, reader, timeout):
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise ConnectionError("EVL closed the connection (another TPI client connected?)")
        return line.decode("ascii", "replace").strip()

    async def dump_forever(self, writer):
        while True:
            writer.write(DUMP_ZONE_TIMERS)
            await writer.drain()
            await asyncio.sleep(DUMP_EVERY_S)

    async def session(self):
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, PORT), 15)
        try:
            greeting = await self.readline(reader, 15)
            if greeting != "Login:":
                raise ConnectionError(f"unexpected greeting {greeting!r}")
            writer.write(self.password.encode() + b"\r\n")
            await writer.drain()
            reply = await self.readline(reader, 15)
            if reply != "OK":
                raise LoginFailed(reply)
            log.info("logged in to %s", self.host)
            dumper = asyncio.create_task(self.dump_forever(writer))
            try:
                while True:
                    line = await self.readline(reader, IDLE_TIMEOUT_S)
                    if line:
                        self.store(line)
            finally:
                dumper.cancel()
        finally:
            writer.close()

    async def run_forever(self):
        backoff = MIN_BACKOFF_S
        while True:
            started = time.time()
            try:
                await self.session()
            except LoginFailed as e:
                log.error("login refused (%s); check SSM evl/password", e)
                backoff = MAX_BACKOFF_S
            except (OSError, TimeoutError) as e:
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
    flume = Flume(conn, config.param("evl/host"), config.param("evl/password"))
    await flume.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
