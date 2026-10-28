"""EnvisaLink (Honeywell) TPI -> SQLite. Read-only.

Holds the one TPI session the EVL allows, on port 4025 over the WireGuard
tunnel, and stores every line it sends in alarm_messages. Zones, open
intervals and troubles are derived at query time by the views in db.py.

The only command ever sent is ^02 (dump zone timers), every 30 s, which also
keeps the session alive. Never ^03: that is a keypress, and keypresses can
arm, disarm or program the panel.
"""

import asyncio
import json
import logging
import time

from . import config, db

log = logging.getLogger("evl")

PORT = 4025
DUMP_ZONE_TIMERS = b"^02,$\r\n"
DUMP_EVERY_S = 30
# The dump replies every 30 s, so this much silence means the session is dead.
IDLE_TIMEOUT_S = 120
MIN_BACKOFF_S, MAX_BACKOFF_S = 5, 300
# The keypad repeats its screen while nothing changes; store a repeat at most
# this often. Kept under the 45 s gap used by alarm_zone_intervals.
REPEAT_EVERY_S = 30


class LoginFailed(Exception):
    pass


class Ingest:
    def __init__(self, conn, host, password):
        self.conn, self.host, self.password = conn, host, password
        self.connected = False
        self.last_line_at = None
        self.last_stored = {}  # code -> (data, time stored)

    def store(self, line):
        if line.startswith("^"):
            # Ack for our ^02, "^02,00$". Anything but 00 is an error code.
            if line[4:6] != "00":
                log.warning("EVL rejected a command: %s", line)
            return
        if line.startswith("%") and line.endswith("$"):
            code, data = line[:3], line[4:-1]
        else:
            code, data = "?", line  # keep anything unexpected, as sent
        prev = self.last_stored.get(code)
        now = time.time()
        if prev and prev[0] == data and now - prev[1] < REPEAT_EVERY_S:
            return
        db.insert_alarm_message(self.conn, code, data)
        self.last_stored[code] = (data, now)

    async def readline(self, reader, timeout):
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line:
            raise ConnectionError("EVL closed the connection (another TPI client connected?)")
        self.last_line_at = time.time()
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
            self.connected = True
            dumper = asyncio.create_task(self.dump_forever(writer))
            try:
                while True:
                    line = await self.readline(reader, IDLE_TIMEOUT_S)
                    if line:
                        self.store(line)
            finally:
                dumper.cancel()
        finally:
            self.connected = False
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

    async def heartbeat_forever(self):
        while True:
            age = None if self.last_line_at is None else int(time.time() - self.last_line_at)
            db.heartbeat(self.conn, "evl_ingest", json.dumps(
                {"connected": self.connected, "seconds_since_message": age}))
            await asyncio.sleep(60)


async def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    ingest = Ingest(conn, config.param("evl/host"), config.param("evl/password"))
    await asyncio.gather(ingest.run_forever(), ingest.heartbeat_forever())


if __name__ == "__main__":
    asyncio.run(main())
