"""EnvisaLink (Honeywell) TPI -> SQLite, plus four keypad actions.

Holds the one TPI session the EVL allows, on port 4025 over the WireGuard
tunnel, and appends every line it sends to the log table, as sent. Zones,
open intervals and troubles are derived at query time by the views in db.py.

It sends ^02 (dump zone timers) every 30 s, which also keeps the session
alive. The only other command is ^03 (a keypress), and only to send one of
ACTIONS: a user code followed by one key, one key per ^03, each acknowledged
before the next. Raw keys are never accepted, so nothing here
can reach installer programming.

Actions come from a small HTTP server on 127.0.0.1:ACTION_PORT, for the
HomeKit bridge on this box; nothing publishes it. The user code arrives with
each request, the same as at a wall keypad, and is never stored or logged here.
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
# Sent only on change, so a repeat is a real change back (open, closed, open
# again) and is always stored: zone state, partition state, Contact ID report.
CHANGES = ("%01,", "%02,", "%03,")
# The keypad's prompt through the entry delay. It ends in a disarm or an alarm;
# the partition state isn't a guide, as it can go back to armed partway through.
ENTRY_DELAY = "DISARM SYSTEM"

ACTION_PORT = 8002
# Honeywell keypad commands: user code + this key.
ACTIONS = {"chime": "9", "stay": "3", "away": "2", "disarm": "1"}
ACTION_LABELS = {"chime": "Chime toggled", "stay": "Arm stay", "away": "Arm away", "disarm": "Disarm"}
ACTION_GAP_S = 3  # between the end of one action and the next
KEY_ACK_TIMEOUT_S = 3
CODE = re.compile(r"[0-9]{4}")  # not \d, which matches other scripts' digits too

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


# --------------------------------------------------------------- parse --

PARSE_VERSION = 1

# Keypad LED bits (EnvisaLink TPI, Honeywell): bit -> the light it is.
LEDS = {0: "alarm", 1: "alarm_in_memory", 2: "armed_away", 3: "ac_present", 4: "bypass",
        5: "chime", 7: "armed_instant", 8: "fire_alarm", 9: "trouble", 12: "ready",
        13: "fire", 14: "low_battery", 15: "armed_stay"}
# %02 partition states, one byte per partition.
PARTITION_STATES = {1: "ready", 2: "ready (zones bypassed)", 3: "not ready", 4: "armed stay",
                    5: "armed away", 6: "armed instant", 7: "exit delay", 8: "in alarm",
                    9: "alarm in memory", 10: "armed away (zero entry delay)"}
# Contact ID event codes seen from this panel, and the usual ones.
CID_EVENTS = {110: "fire", 120: "panic", 130: "burglary", 131: "perimeter burglary",
              132: "interior burglary", 134: "entry/exit burglary", 137: "tamper",
              301: "AC power lost", 302: "low system battery", 344: "RF jam",
              373: "fire loop trouble", 380: "sensor trouble", 381: "lost supervision (RF)",
              383: "sensor tamper", 384: "low battery (RF)", 401: "disarmed/armed by user",
              403: "auto-armed", 406: "cancelled", 407: "remote arm/disarm", 408: "quick arm",
              441: "armed stay", 570: "zone bypassed", 602: "periodic test"}


def alarm_status(leds, text):
    """What the keypad says the system is doing, as the Home page shows it."""
    if leds & 0x0001:
        return "alarm"
    if text.startswith(ENTRY_DELAY):
        return "entry delay"
    if "May Exit Now" in text:
        return "exit delay"
    if leds & 0x0004:
        return "armed away"
    if leds & 0x8000:
        return "armed stay"
    return "disarmed, ready" if leds & 0x1000 else "disarmed, not ready"


def bits(hex_bytes):
    """A run of hex bytes -> the 1-based numbers of the bits set, lowest bit of the first byte first."""
    return [i * 8 + b + 1 for i in range(len(hex_bytes) // 2)
            for b in range(8) if int(hex_bytes[i * 2:i * 2 + 2], 16) & (1 << b)]


def parse(event_type, event_name, body):
    """A stored TPI line -> what it says: the keypad's screen and lights,
    which zones are open, the partitions' states, a Contact ID report."""
    if event_type == "action":
        return {"action": event_name, "text": body}
    if not (body.startswith("%") and body.endswith("$")):
        return None
    data = body[4:-1]
    if event_type == "keypad":
        partition, leds, numeric, beeps, text = data.split(",", 4)
        leds, text = int(leds, 16), " ".join(text.split())
        out = {"text": text, "status": alarm_status(leds, text), "partition": int(partition),
               "lights": [name for bit, name in LEDS.items() if leds & (1 << bit)]}
        if int(beeps, 16):
            out["beeps"] = int(beeps, 16)
        _, _, zone = evl_fields(body)
        if zone:
            out["zone"] = int(zone)
        return out
    if event_type == "zone_state":
        return {"open_zones": bits(data)}
    if event_type == "partition_state":
        states = {str(i + 1): PARTITION_STATES.get(int(data[i * 2:i * 2 + 2], 16), data[i * 2:i * 2 + 2])
                  for i in range(len(data) // 2) if data[i * 2:i * 2 + 2] != "00"}
        return {"partitions": states}
    if event_type == "cid":
        # QXXXPPZZZ: qualifier (1 event or disarm, 3 restore or arm), event
        # code, partition, then the zone or user number.
        q, code = data[0], int(data[1:4])
        return {"event": CID_EVENTS.get(code, f"code {code}"), "code": code,
                "restore": q == "3", "partition": int(data[4:6]), "zone_or_user": int(data[6:9])}
    if event_type == "zone_timers":
        # Per zone, a little-endian UINT16: 0xFFFF while open, counting down
        # one per 5 s since it was last faulted; 0 if not for a long while.
        timers = {z + 1: int(data[z * 4 + 2:z * 4 + 4] + data[z * 4:z * 4 + 2], 16)
                  for z in range(len(data) // 4)}
        return {"open_zones": [z for z, t in timers.items() if t == 0xFFFF],
                "faulted_s_ago": {str(z): (0xFFFF - t) * 5 for z, t in timers.items() if 0 < t < 0xFFFF}}
    return None


class LoginFailed(Exception):
    pass


class Flume:
    def __init__(self, conn, host, password):
        self.conn, self.host, self.password = conn, host, password
        self.last_stored = {}  # line -> time stored
        self.writer = None  # the live session, for actions
        self.leds = None  # latest keypad LED bits
        self.entry_delay = False
        self.last_action = 0  # when the last action finished
        self.action_lock = asyncio.Lock()  # one action's keys at a time
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
            else:
                text = line[:-1].split(",", 5)[-1]
                if text.startswith(ENTRY_DELAY):
                    self.entry_delay = True
                elif self.leds & 0x0001 or not self.leds & 0x8004 or text.startswith("ARMED"):
                    self.entry_delay = False  # alarm, disarmed, or armed and quiet
        now = time.time()
        if not line.startswith(CHANGES) and now - self.last_stored.get(line, 0) < REPEAT_EVERY_S:
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
            self.writer = self.leds = None  # nothing below is current any more
            self.entry_delay = False
            writer.close()

    def status(self):
        L = self.leds
        if L is None:
            return {"connected": self.writer is not None}
        state = ("alarm" if L & 0x0001 else "away" if L & 0x0004 else "stay" if L & 0x8000
                 else "disarmed")
        return {"connected": True, "state": state, "ready": bool(L & 0x1000),
                "bypass": bool(L & 0x0010), "chime": bool(L & 0x0020),
                "entry_delay": self.entry_delay}

    async def act(self, action, code):
        """Send one of ACTIONS. Returns an error message, or None once sent."""
        if not isinstance(action, str) or action not in ACTIONS:
            return "unknown action"
        if not isinstance(code, str) or not CODE.fullmatch(code):
            return "the code is 4 digits"
        if self.writer is None:
            return "not connected to the alarm panel"
        if self.action_lock.locked() or time.time() - self.last_action < ACTION_GAP_S:
            return "one action at a time"
        async with self.action_lock:
            try:
                err = await self.send_keys(action, code + ACTIONS[action])
            finally:
                self.last_action = time.time()
        if err:
            return err
        log.info("sent %s", action)  # never the code
        db.append(self.conn, source="evl", event_type="action", event_name=action,
                  body=f"{ACTION_LABELS[action]} from HomeKit")
        return None

    async def send_keys(self, action, keys):
        writer = self.writer
        for i, key in enumerate(keys):
            if self.writer is not writer:
                return "lost the connection to the alarm panel"
            self.key_ack = asyncio.get_running_loop().create_future()
            writer.write(f"^03,1,{key}$\r\n".encode())
            await writer.drain()
            try:
                ack = await asyncio.wait_for(self.key_ack, KEY_ACK_TIMEOUT_S)
            except asyncio.TimeoutError:
                ack = "no reply"
            if ack != "00":
                log.warning("%s: key %d of %d not accepted (%s)", action, i + 1, len(keys), ack)
                return f"the alarm panel didn't accept the keys ({ack})"
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
    """Minimal HTTP for GET status and POST action, on localhost only."""

    def __init__(self, flume):
        self.flume = flume

    async def handle(self, reader, writer):
        try:
            status, reply = await asyncio.wait_for(self.respond(reader), 10)
        except (OSError, TimeoutError, ValueError, EOFError):  # EOFError: IncompleteReadError
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
        path = path.split("?")[0]
        if method == "GET" and path in ("", "/", "/status"):
            return 200, self.flume.status()
        if method == "POST" and path == "/action":
            req = json.loads(data or b"{}")
            if not isinstance(req, dict):
                return 400, {"error": "bad request"}
            err = await self.flume.act(req.get("action"), req.get("code"))
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
