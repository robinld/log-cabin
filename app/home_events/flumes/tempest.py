"""WeatherFlow Tempest weather station, through Tempest's cloud -> SQLite. Read-only.

The hub broadcasts its readings on the home LAN, and broadcasts don't cross
the WireGuard tunnel. It also sends them to Tempest's cloud, which pushes them
to a WebSocket in the same form: the same message types and obs arrays.

Every 6 h and on each connection it stores the station list (names, device
ids), then listens to each Tempest device and stores what arrives: obs_st, a
full observation every minute, and evt_strike (lightning) and evt_precip (rain
starting). Not rapid_wind, every 3 s: obs_st already has each minute's lull,
average and gust.

Needs a personal access token in SSM tempest/token (Tempest web app, Settings,
Data Authorizations). Docs: https://apidocs.tempestwx.com/reference/websocket-reference
"""

import asyncio
import json
import logging
import time
import uuid

import httpx
import websockets

from .. import config, db

log = logging.getLogger("tempest")

STATIONS_URL = "https://swd.weatherflow.com/swd/rest/stations"
WS_URL = "wss://ws.weatherflow.com/swd/data"
STATIONS_REFRESH_S = 6 * 3600
QUIET_S = 180  # obs_st comes every minute; this long without one, reconnect
MIN_BACKOFF_S, MAX_BACKOFF_S = 5, 300
NOT_STORED = {"ack", "connection_opened"}


def tempest_devices(stations):
    """The Tempest (ST) device ids in a station list."""
    return [d["device_id"] for s in stations.get("stations", []) for d in s.get("devices", [])
            if d.get("device_type") == "ST"]


def observed_ms(msg):
    """When a message's reading was taken, from its obs or evt array."""
    try:
        if msg.get("obs"):
            return int(msg["obs"][-1][0]) * 1000
        if msg.get("evt"):
            return int(msg["evt"][0]) * 1000
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    return None


PARSE_VERSION = 1
# obs_st's obs array, by index (apidocs.tempestwx.com); the weather view uses the same.
OBS_ST = {2: "wind_avg_ms", 3: "wind_gust_ms", 4: "wind_dir_deg", 6: "pressure_mb",
          7: "air_temp_c", 8: "humidity_pct", 9: "illuminance_lux", 10: "uv", 11: "solar_w_m2",
          12: "rain_mm", 14: "lightning_distance_km", 15: "lightning_strikes", 16: "battery_v"}


def parse(event_type, event_name, body):
    """An observation -> its readings by name; a strike -> its distance and energy."""
    msg = json.loads(body)
    if event_type == "stations":
        return {"stations": len(msg.get("stations") or [])}
    if event_type == "obs_st" and msg.get("obs"):
        obs = msg["obs"][0]
        return {name: obs[i] for i, name in OBS_ST.items() if i < len(obs) and obs[i] is not None}
    evt = msg.get("evt") or []
    if event_type == "evt_strike" and len(evt) >= 3:
        return {"distance_km": evt[1], "energy": evt[2]}
    if event_type == "evt_precip":
        return {"rain_started": True}
    return None


class Flume:
    def __init__(self, conn, token):
        self.conn, self.token = conn, token
        self.http = httpx.AsyncClient(timeout=30)
        self.last_obs = {}  # device id -> time of the latest obs_st stored
        self.devices, self.stations_at = [], 0.0

    async def refresh_stations(self):
        r = await self.http.get(STATIONS_URL, params={"token": self.token})
        r.raise_for_status()
        stations = r.json()
        db.append(self.conn, source="tempest", event_type="stations", event_name=None,
                  body=stations)
        self.devices, self.stations_at = tempest_devices(stations), time.time()
        log.info("Tempest devices: %s", self.devices)

    def store(self, msg):
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind is None or kind in NOT_STORED:
            return False
        device = msg.get("device_id")
        at = observed_ms(msg)
        # Each listen_start replays the latest obs_st, already stored if we were connected.
        if kind == "obs_st":
            if at is not None and self.last_obs.get(device) == at:
                return False
            self.last_obs[device] = at
        db.append(self.conn, source="tempest", event_type=kind, event_name=msg.get("source"),
                  device_id=None if device is None else str(device), ts_ms=at, body=msg)
        return True

    async def session(self):
        if not self.devices or time.time() - self.stations_at > STATIONS_REFRESH_S:
            await self.refresh_stations()
        if not self.devices:
            raise ValueError("no Tempest devices in the station list")
        async with websockets.connect(f"{WS_URL}?token={self.token}") as ws:
            for d in self.devices:
                await ws.send(json.dumps({"type": "listen_start", "device_id": d,
                                          "id": str(uuid.uuid4())}))
            log.info("listening to %s", self.devices)
            while time.time() - self.stations_at < STATIONS_REFRESH_S:
                self.store(json.loads(await asyncio.wait_for(ws.recv(), QUIET_S)))

    async def run_forever(self):
        backoff = MIN_BACKOFF_S
        while True:
            started = time.time()
            try:
                await self.session()
                continue  # stations due a refresh; reconnect at once
            except (httpx.HTTPError, websockets.WebSocketException, OSError, TimeoutError,
                    ValueError) as e:
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
    token, = config.params_or_wait("tempest/token", log=log)
    await Flume(conn, token).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
