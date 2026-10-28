"""YoLink cloud -> SQLite.

Keeps one MQTT subscription to the YoLink cloud broker open, and polls every
device's state on a timer to catch anything that was missed while the
connection was down. YoLink gives personal (UAC) accounts no history API, so
whatever this process doesn't see is gone.

Docs: https://doc.yosmart.com/docs/protocol/openAPIV2/
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid

import aiomqtt
import httpx

from . import config, db

log = logging.getLogger("yolink")

TOKEN_URL = "https://api.yosmart.com/open/yolink/token"
API_URL = "https://api.yosmart.com/open/yolink/v2/api"
MQTT_HOST = "mqtt.api.yosmart.com"
MQTT_PORT = 8003

POLL_MINUTES = float(os.environ.get("YOLINK_POLL_MINUTES", "30"))
DEVICE_REFRESH_S = 6 * 3600
# UACs may open at most 10 MQTT connections per 5 minutes, so never
# reconnect faster than every 30 s.
MIN_BACKOFF_S, MAX_BACKOFF_S = 30, 300

# Device types that are not sensors/actuators and don't support getState.
NO_STATE_TYPES = {"Hub", "SpeakerHub"}


class YoLinkError(Exception):
    pass


class YoLink:
    def __init__(self, uaid, secret):
        self.uaid, self.secret = uaid, secret
        self.http = httpx.AsyncClient(timeout=30)
        self.token = None
        self.expires_at = 0.0

    async def access_token(self):
        if self.token and time.time() < self.expires_at - 300:
            return self.token
        r = await self.http.post(TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.uaid,
            "client_secret": self.secret,
        })
        r.raise_for_status()
        body = r.json()
        if "access_token" not in body:
            raise YoLinkError(f"token request failed: {body}")
        self.token = body["access_token"]
        self.expires_at = time.time() + float(body.get("expires_in", 7200))
        log.info("got access token, expires in %ss", body.get("expires_in"))
        return self.token

    async def call(self, method, **params):
        token = await self.access_token()
        r = await self.http.post(
            API_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"method": method, "time": int(time.time() * 1000), **params},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("code") != "000000":
            raise YoLinkError(f"{method}: {body.get('code')} {body.get('desc')}")
        return body.get("data") or {}


class Ingest:
    def __init__(self, yl, conn):
        self.yl, self.conn = yl, conn
        self.home_id = None
        self.devices = {}  # deviceId -> device dict from Home.getDeviceList
        self.last_message_at = None

    async def refresh_devices(self):
        info = await self.yl.call("Home.getGeneralInfo")
        self.home_id = info["id"]
        data = await self.yl.call("Home.getDeviceList")
        self.devices = {d["deviceId"]: d for d in data.get("devices", [])}
        for d in self.devices.values():
            db.upsert_device(self.conn, device_id=d["deviceId"], source="yolink",
                             name=d.get("name"), type_=d.get("type"),
                             model=d.get("modelName"))
        log.info("home %s: %d devices", self.home_id, len(self.devices))

    # ---------------------------------------------------------------- MQTT --

    def handle_report(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            log.warning("unexpected MQTT payload: %r", raw[:200])
            msg = {"raw": raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)}
        device_id = msg.get("deviceId")
        event = msg.get("event") or "unknown"
        ts_ms = msg.get("time") or time.time() * 1000
        name = (self.devices.get(device_id) or {}).get("name")
        msgid = msg.get("msgid") or hashlib.sha1(raw if isinstance(raw, bytes) else raw.encode()).hexdigest()
        new = db.insert_event(
            self.conn, source="yolink", event_type=event, ts_ms=ts_ms,
            device_id=device_id, device_name=name,
            summary=summarize(name or device_id, event, msg.get("data")),
            dedupe_key=f"yolink:{device_id}:{msgid}", payload=msg,
        )
        self.last_message_at = time.time()
        log.debug("%s %s %s", "stored" if new else "dup", name, event)

    async def mqtt_forever(self):
        backoff = MIN_BACKOFF_S
        while True:
            started = time.time()
            try:
                token = await self.yl.access_token()
                # Reconnect shortly before the token (our MQTT password) expires.
                session_s = max(60.0, self.yl.expires_at - time.time() - 300)
                async with aiomqtt.Client(
                    hostname=MQTT_HOST, port=MQTT_PORT, username=token,
                    identifier=str(uuid.uuid4()), keepalive=60,
                ) as client:
                    topic = f"yl-home/{self.home_id}/+/report"
                    await client.subscribe(topic)
                    log.info("subscribed to %s for %.0fs", topic, session_s)
                    try:
                        async with asyncio.timeout(session_s):
                            async for message in client.messages:
                                self.handle_report(message.payload)
                    except TimeoutError:
                        log.info("rotating MQTT session for token refresh")
                backoff = MIN_BACKOFF_S
            except (aiomqtt.MqttError, httpx.HTTPError, YoLinkError) as e:
                log.warning("MQTT session ended: %s; retrying in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            # Even on a clean rotation, respect the connection-rate limit.
            await asyncio.sleep(max(0, MIN_BACKOFF_S - (time.time() - started)))

    # ---------------------------------------------------------------- poll --

    async def poll_once(self):
        changed = 0
        for d in list(self.devices.values()):
            if d.get("type") in NO_STATE_TYPES:
                continue
            try:
                data = await self.yl.call(f"{d['type']}.getState",
                                          targetDevice=d["deviceId"], token=d["token"])
            except (YoLinkError, httpx.HTTPError) as e:
                log.debug("getState %s (%s) failed: %s", d.get("name"), d.get("type"), e)
                continue
            state = data.get("state", data)
            state_json = json.dumps(state, sort_keys=True)
            row = self.conn.execute("SELECT state FROM device_state WHERE device_id=?",
                                    (d["deviceId"],)).fetchone()
            if row is None or row["state"] != state_json:
                now_ms = time.time() * 1000
                db.insert_event(
                    self.conn, source="yolink_poll", event_type=f"{d['type']}.poll_change",
                    ts_ms=now_ms, device_id=d["deviceId"], device_name=d.get("name"),
                    summary=summarize(d.get("name"), "polled state", state),
                    dedupe_key=f"yolink-poll:{d['deviceId']}:{int(now_ms)}",
                    payload={"baseline": row is None, "data": data},
                )
                changed += 1
            self.conn.execute(
                """INSERT INTO device_state (device_id, state, polled_at) VALUES (?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET state=excluded.state,
                   polled_at=excluded.polled_at""",
                (d["deviceId"], state_json, db.iso()),
            )
            await asyncio.sleep(1)  # be gentle with the API
        log.info("poll done: %d of %d devices changed", changed, len(self.devices))

    async def poll_forever(self):
        last_refresh = time.time()
        while True:
            try:
                if time.time() - last_refresh > DEVICE_REFRESH_S:
                    await self.refresh_devices()
                    last_refresh = time.time()
                await self.poll_once()
            except Exception:
                log.exception("poll failed")
            await asyncio.sleep(POLL_MINUTES * 60)

    async def heartbeat_forever(self):
        while True:
            age = None if self.last_message_at is None else int(time.time() - self.last_message_at)
            db.heartbeat(self.conn, "yolink_ingest", json.dumps({"seconds_since_mqtt_message": age}))
            await asyncio.sleep(60)


def summarize(name, event, data):
    if isinstance(data, dict) and "state" in data:
        data = data["state"]
    detail = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    s = f"{name or 'unknown device'}: {event} {detail or ''}".strip()
    return s if len(s) <= 200 else s[:197] + "..."


async def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    ingest = Ingest(YoLink(config.param("yolink/uaid"), config.param("yolink/secret")), conn)
    await ingest.refresh_devices()
    await asyncio.gather(
        ingest.mqtt_forever(),
        ingest.poll_forever(),
        ingest.heartbeat_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
