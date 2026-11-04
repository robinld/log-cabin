"""Lennox iComfort S30 thermostat (local API) -> SQLite. Read-only.

The S30 serves an HTTPS message API on the home LAN, reached over the
WireGuard tunnel. A client registers with an application id, subscribes to
data paths, then long-polls for messages: zone temperatures, setpoints,
humidity, what the equipment is doing, alerts. Every message is appended to
the log table as received.

The S30's local API has no authentication, so this only ever Connects,
RequestData (subscribe), Retrieves and Disconnects. It never calls
/Messages/Publish, which is how settings are changed.

Protocol from lennoxs30api (https://github.com/PeteRager/lennoxs30api).
"""

import asyncio
import json
import logging
import ssl
import time
import uuid

import httpx

from .. import config, db

log = logging.getLogger("lennox")

APP_ID = "log_cabin"  # each client needs its own; the S30 queues messages per id
# The same paths lennoxs30api subscribes to for a LAN connection.
SUBSCRIBE_PATHS = ("1;/systemControl;/systemController;/reminderSensors;/reminders;"
                   "/alerts/active;/alerts/meta;/bleProvisionDB;/ble;/indoorAirQuality;"
                   "/fwm;/rgw;/devices;/zones;/equipments;/schedules;/occupancy;/system")
LONG_POLL_S = 15
MIN_BACKOFF_S, MAX_BACKOFF_S = 5, 300

# The S30 re-sends the same few messages every minute or so. Store a given
# message at most this often; anything that differs is stored at once.
REPEAT_EVERY_S = 900
# Its own Wi-Fi statistics (signal, bit rate) change constantly and say
# nothing about the house, so keep them hourly whatever they say.
REPEAT_EVERY_S_BY_SECTION = {"interfaces": 3600}
# Fields that tick on every message. They are ignored when deciding whether
# a message is a repeat; a stored message keeps them, unedited.
CLOCK_FIELDS = {"MessageID", "MessageId", "currentTime", "sysUpTime"}


def ssl_context():
    # The S30's certificate is self-issued by Lennox and can't be verified,
    # and it needs ciphers newer Pythons leave out by default.
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def lennox_fields(msg):
    """A message -> (event_type, event_name) for the log columns.

    event_type is the MessageType (PropertyChange, ...), event_name the
    top-level Data sections it carries, e.g. "zones" or "equipments,system".
    """
    if not isinstance(msg, dict):
        return "unknown", None
    data = msg.get("Data")
    return (msg.get("MessageType") or "unknown",
            ",".join(sorted(data)) if isinstance(data, dict) and data else None)


PARSE_VERSION = 1
# Zone status fields worth reading, and their names in parsed. Temperatures
# are the thermostat's °F; the °C copies are left out.
ZONE_FIELDS = {"temperature": "temperature_f", "humidity": "humidity_pct",
               "tempOperation": "operation", "humOperation": "humidity_operation",
               "fan": "fan", "demand": "demand_pct", "aux": "aux_heat", "defrost": "defrost"}
PERIOD_FIELDS = {"systemMode": "mode", "fanMode": "fan_mode", "hsp": "heat_setpoint_f",
                 "csp": "cool_setpoint_f", "sp": "setpoint_f", "husp": "humidify_setpoint_pct",
                 "desp": "dehumidify_setpoint_pct"}


def epoch_iso(s):
    """Lennox's seconds-since-epoch strings -> ISO-8601, or None."""
    return db.iso(int(s) * 1000) if str(s or "").isdigit() else None


def parse(event_type, event_name, body):
    """A message -> per zone, temperature, humidity, setpoints and what the
    system is doing; the alerts; and which sections the rest updated. Messages
    can be partial: a zone carries only the fields that changed."""
    data = json.loads(body).get("Data")
    if not isinstance(data, dict):
        return None
    out = {"sections": sorted(data)}
    zones = {}
    for z in data.get("zones") or []:
        st = z.get("status") or {}
        period = st.get("period") or {}
        f = {name: st[k] for k, name in ZONE_FIELDS.items() if k in st}
        f.update({name: period[k] for k, name in PERIOD_FIELDS.items() if k in period})
        if f:
            zones[str(z.get("id"))] = f
    if zones:
        out["zones"] = zones
    alerts = data.get("alerts")
    if isinstance(alerts, dict):
        meta = alerts.get("meta") or {}
        if "numActiveAlerts" in meta:
            out["active_alerts"] = meta["numActiveAlerts"]
        out["alerts"] = [{"code": a.get("code"), "message": a.get("userMessage"),
                          "priority": a.get("priority"), "active": a.get("isStillActive"),
                          "first": epoch_iso(a.get("timestampFirst"))}
                         for a in ((x.get("alert") or {}) for x in alerts.get("active") or []) if a]
    clock = epoch_iso(((data.get("system") or {}).get("time") or {}).get("currentTime"))
    if clock:
        out["thermostat_time"] = clock
    interfaces = data.get("interfaces")
    if isinstance(interfaces, list) and interfaces:
        # Info.status only: APDetails holds the Wi-Fi password.
        status = (interfaces[0].get("Info") or {}).get("status") or {}
        wifi = {k: status[k] for k in ("ssid", "ip", "rssi") if k in status}
        if wifi:
            out["wifi"] = wifi
    return out


def without_clocks(o):
    if isinstance(o, dict):
        return {k: without_clocks(v) for k, v in o.items() if k not in CLOCK_FIELDS}
    if isinstance(o, list):
        return [without_clocks(v) for v in o]
    return o


def shape(o):
    """A message's fields and item ids without their values: what it updates."""
    if isinstance(o, dict):
        return {k: v if k == "id" else shape(v) for k, v in o.items() if k not in CLOCK_FIELDS}
    if isinstance(o, list):
        return [shape(v) for v in o]
    return None


def repeat_key(event_name, msg):
    """(what a message updates, what it says). A message is a repeat when the
    last one stored that updates the same things said the same."""
    if event_name in REPEAT_EVERY_S_BY_SECTION:
        return event_name, None  # kept on a timer, whatever they say
    return (json.dumps(shape(msg), sort_keys=True),
            json.dumps(without_clocks(msg), sort_keys=True))


class Flume:
    def __init__(self, conn, host):
        self.conn = conn
        self.base = f"https://{host}"
        self.http = httpx.AsyncClient(verify=ssl_context(), timeout=LONG_POLL_S + 30)
        self.last_stored = {}  # shape -> (content, time stored)

    def store(self, msg, now=None):
        now = time.time() if now is None else now
        event_type, event_name = lennox_fields(msg)
        key, content = repeat_key(event_name, msg)
        window = REPEAT_EVERY_S_BY_SECTION.get(event_name, REPEAT_EVERY_S)
        last_content, last_time = self.last_stored.get(key, (None, -window))
        if content == last_content and now - last_time < window:
            return False
        db.append(self.conn, source="lennox", event_type=event_type,
                  event_name=event_name, body=msg)
        # Forget anything older than the longest window, so this stays small.
        longest = max(REPEAT_EVERY_S, *REPEAT_EVERY_S_BY_SECTION.values())
        self.last_stored = {k: v for k, v in self.last_stored.items() if now - v[1] < longest}
        self.last_stored[key] = (content, now)
        return True

    async def session(self):
        r = await self.http.post(f"{self.base}/Endpoints/{APP_ID}/Connect")
        r.raise_for_status()
        try:
            r = await self.http.post(f"{self.base}/Messages/RequestData", json={
                "MessageType": "RequestData", "SenderID": APP_ID,
                "MessageID": str(uuid.uuid4()), "TargetID": "LCC",
                "AdditionalParameters": {"JSONPath": SUBSCRIBE_PATHS}})
            r.raise_for_status()
            log.info("subscribed to %s", self.base)
            while True:
                r = await self.http.get(f"{self.base}/Messages/{APP_ID}/Retrieve", params={
                    "Direction": "Oldest-to-Newest", "MessageCount": "10",
                    "StartTime": "1", "LongPollingTimeout": str(LONG_POLL_S)})
                if r.status_code == 204:
                    continue
                r.raise_for_status()
                for msg in r.json().get("messages", []):
                    self.store(msg)
        finally:
            try:
                await self.http.post(f"{self.base}/Endpoints/{APP_ID}/Disconnect")
            except httpx.HTTPError:
                pass

    async def run_forever(self):
        backoff = MIN_BACKOFF_S
        while True:
            started = time.time()
            try:
                await self.session()
            except (httpx.HTTPError, ValueError) as e:
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
    await Flume(conn, config.param("lennox/host")).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
