"""EnvisaLink (Honeywell) TPI -> SQLite, plus three keypad actions.

Holds the one TPI session the EVL allows, on port 4025 over the WireGuard
tunnel, and appends every line it sends to the log table, as sent. Zones,
open intervals and troubles are derived at query time by the views in db.py.

It sends ^02 (dump zone timers) every 30 s, which also keeps the session
alive. The only other command is ^03 (a keypress), and only to send one of
ACTIONS: a user code followed by one key, one key per ^03, each acknowledged
before the next. Raw keys are never accepted, so nothing here
can reach installer programming.

Actions come from a small HTTP server on 127.0.0.1:ACTION_PORT; publishing it
is the box's business (see install.sh). The user code arrives with each
request, the same as at a wall keypad, and is never stored or logged. A wrong
code does nothing at the panel, so actions are rate-limited to make guessing
impractical.
"""

import asyncio
import json
import logging
import re
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

ACTION_PORT = 8002
# Honeywell keypad commands: user code + this key.
ACTIONS = {"chime": "9", "stay": "3", "disarm": "1"}
ACTION_LABELS = {"chime": "Chime toggled", "stay": "Arm stay", "disarm": "Disarm"}
ACTION_GAP_S = 3  # at most one action this often,
ACTION_LIMIT, ACTION_WINDOW_S = 5, 600  # and this many per window
KEY_ACK_TIMEOUT_S = 3
CODE = re.compile(r"\d{4}")

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
        self.writer = None  # the live session, for actions
        self.leds = None  # latest keypad LED bits
        self.action_times = []
        self.key_ack = None  # future for the pending ^03's reply

    def store(self, line):
        if line.startswith("^"):
            # Ack for our ^02 or ^03, e.g. "^02,00$". Anything but 00 is an error code.
            if line.startswith("^03,") and self.key_ack and not self.key_ack.done():
                self.key_ack.set_result(line[4:6])
            elif line[4:6] != "00":
                log.warning("EVL rejected a command: %s", line)
            return
        if line.startswith("%00,"):
            try:
                self.leds = int(line.split(",")[2], 16)
            except (IndexError, ValueError):
                pass
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
            self.writer = writer
            dumper = asyncio.create_task(self.dump_forever(writer))
            try:
                while True:
                    line = await self.readline(reader, IDLE_TIMEOUT_S)
                    if line:
                        self.store(line)
            finally:
                dumper.cancel()
        finally:
            self.writer = None
            writer.close()

    def status(self):
        L = self.leds
        if L is None:
            return {"connected": self.writer is not None}
        state = ("alarm" if L & 0x0001 else "away" if L & 0x0004 else "stay" if L & 0x8000
                 else "disarmed")
        return {"connected": self.writer is not None, "state": state,
                "ready": bool(L & 0x1000), "chime": bool(L & 0x0020)}

    async def act(self, action, code):
        """Send one of ACTIONS. Returns an error message, or None once sent."""
        if action not in ACTIONS:
            return "unknown action"
        if not CODE.fullmatch(code or ""):
            return "the code is 4 digits"
        if self.writer is None:
            return "not connected to the alarm panel"
        now = time.time()
        self.action_times = [t for t in self.action_times if now - t < ACTION_WINDOW_S]
        if self.action_times and now - self.action_times[-1] < ACTION_GAP_S:
            return "one action at a time"
        if len(self.action_times) >= ACTION_LIMIT:
            return f"limit is {ACTION_LIMIT} actions per {ACTION_WINDOW_S // 60} minutes; try again later"
        self.action_times.append(now)
        for i, key in enumerate(code + ACTIONS[action]):
            self.key_ack = asyncio.get_running_loop().create_future()
            self.writer.write(f"^03,1,{key}$\r\n".encode())
            await self.writer.drain()
            try:
                ack = await asyncio.wait_for(self.key_ack, KEY_ACK_TIMEOUT_S)
            except asyncio.TimeoutError:
                ack = "no reply"
            if ack != "00":
                log.warning("%s: key %d of %d not accepted (%s)", action, i + 1, len(code) + 1, ack)
                return f"the alarm panel didn't accept the keys ({ack})"
        log.info("sent %s", action)  # never the code
        db.append(self.conn, source="evl", event_type="action", event_name=action,
                  body=f"{ACTION_LABELS[action]} from /home")
        return None

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


class ActionServer:
    """Minimal HTTP for GET status and POST action, on localhost only.

    Whatever publishes it may or may not strip an /alarm prefix, so either
    form is accepted.
    """

    def __init__(self, flume):
        self.flume = flume

    async def handle(self, reader, writer):
        try:
            status, reply = await asyncio.wait_for(self.respond(reader), 10)
        except (OSError, TimeoutError, ValueError):
            status, reply = 400, {"error": "bad request"}
        body = json.dumps(reply).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 409: "Conflict"}[status]
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                     f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        finally:
            writer.close()

    async def respond(self, reader):
        method, path, _ = (await reader.readline()).decode("latin-1").split(" ", 2)
        headers = {}
        while (line := (await reader.readline()).decode("latin-1").strip()):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length") or 0)
        if length > 1024:
            return 400, {"error": "too large"}
        data = await reader.readexactly(length) if length else b""
        path = path.split("?")[0].removeprefix("/alarm")
        if method == "GET" and path in ("", "/", "/status"):
            return 200, self.flume.status()
        if method == "POST" and path == "/action":
            req = json.loads(data or b"{}")
            err = await self.flume.act(req.get("action"), str(req.get("code") or ""))
            return (409, {"error": err}) if err else (200, {"ok": True})
        return 404, {"error": "not found"}


async def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    flume = Flume(conn, config.param("evl/host"), config.param("evl/password"))
    server = await asyncio.start_server(ActionServer(flume).handle, "127.0.0.1", ACTION_PORT)
    async with server:
        await flume.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
