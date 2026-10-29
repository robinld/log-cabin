"""YoLink cloud -> SQLite.

Keeps one MQTT subscription to the YoLink cloud broker open, and polls every
device's state on a timer to catch anything that was missed while the
connection was down. YoLink gives personal (UAC) accounts no history API, so
whatever this process doesn't see is gone.

Everything is appended to the log table as received: MQTT reports, every
poll response and the device list. The views in db.py find state changes
and device names.

Docs: https://doc.yosmart.com/docs/protocol/openAPIV2/
"""

import asyncio
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

    async def refresh_devices(self):
        info = await self.yl.call("Home.getGeneralInfo")
        self.home_id = info["id"]
        data = await self.yl.call("Home.getDeviceList")
        self.devices = {d["deviceId"]: d for d in data.get("devices", [])}
        # Each device's token authorizes API calls to it. The database is
        # readable from the home LAN, so it is the one field not stored.
        stored = {**data, "devices": [{k: v for k, v in d.items() if k != "token"}
                                      for d in data.get("devices", [])]}
        db.append(self.conn, source="yolink", event_type="Home", event_name="getDeviceList",
                  body=stored)
        log.info("home %s: %d devices", self.home_id, len(self.devices))

    # ---------------------------------------------------------------- MQTT --

    def handle_report(self, raw):
        body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        try:
            msg = json.loads(body)
        except ValueError:
            msg = None
        if not isinstance(msg, dict):
            log.warning("unexpected MQTT payload: %r", body[:200])
            msg = {}
        # "DoorSensor.Alert" -> event_type DoorSensor, event_name Alert
        event_type, _, event_name = (msg.get("event") or "unknown").partition(".")
        db.append(self.conn, source="yolink", event_type=event_type,
                  event_name=event_name or None, device_id=msg.get("deviceId"),
                  ts_ms=msg.get("time"), body=body)
        log.debug("stored %s %s", msg.get("deviceId"), msg.get("event"))

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
        polled = 0
        for d in list(self.devices.values()):
            if d.get("type") in NO_STATE_TYPES:
                continue
            try:
                data = await self.yl.call(f"{d['type']}.getState",
                                          targetDevice=d["deviceId"], token=d["token"])
            except (YoLinkError, httpx.HTTPError) as e:
                log.debug("getState %s (%s) failed: %s", d.get("name"), d.get("type"), e)
                continue
            db.append(self.conn, source="yolink", event_type=d["type"], event_name="getState",
                      device_id=d["deviceId"], body=data)
            polled += 1
            await asyncio.sleep(1)  # be gentle with the API
        log.info("poll done: %d of %d devices answered", polled, len(self.devices))

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


async def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    ingest = Ingest(YoLink(config.param("yolink/uaid"), config.param("yolink/secret")), conn)
    await ingest.refresh_devices()
    await asyncio.gather(
        ingest.mqtt_forever(),
        ingest.poll_forever(),
    )


if __name__ == "__main__":
    asyncio.run(main())
