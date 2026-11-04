"""Emporia Energy (Vue energy monitor and other devices), through Emporia's cloud -> SQLite. Read-only.

Emporia's devices talk only to Emporia's cloud; nothing on the LAN can be read.
This signs in to the cloud API the Emporia app uses, through PyEmVue, an
unofficial client (https://github.com/magico13/PyEmVue), with the account in
SSM emporia/username and emporia/password.

Every WINDOW_MINUTES it stores each device's usage per circuit over that
window, in kWh, and every 6 h the device list with the circuits' names, both
as received. Fifteen-minute windows keep it to about 100 rows a day; one-minute
ones would grow the database by over a gigabyte a year. It never calls the
outlet or charger endpoints, which change settings.
"""

import datetime
import json
import logging
import time

import requests
from pyemvue import PyEmVue
from pyemvue.pyemvue import API_CUSTOMER_DEVICES, API_DEVICES_USAGE

from .. import config, db

log = logging.getLogger("emporia")

WINDOW_MINUTES = 15
SCALE = f"{WINDOW_MINUTES}MIN"
LAG_S = 120  # Emporia's cloud fills in the last minute or two of a window late
DEVICES_REFRESH_S = 6 * 3600
SIGN_IN_RETRY_S = 900  # don't hammer the account's sign-in with a wrong password


def device_gids(devices):
    """Every deviceGid in a customers/devices response, nested ones included."""
    out = []
    for d in devices.get("devices", []):
        out.append(d["deviceGid"])
        out.extend(device_gids(d))
    return out


def usage_path(gids, until):
    """The getDeviceListUsages request for the window ending at until (UTC seconds)."""
    instant = datetime.datetime.fromtimestamp(until, datetime.timezone.utc)
    return API_DEVICES_USAGE.format(deviceGids="+".join(map(str, gids)),
                                    instant=instant.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    scale=SCALE, unit="KilowattHours")


PARSE_VERSION = 1


def parse(event_type, event_name, body):
    """A usage window -> kWh per circuit, by name (or channel number); the
    device list -> how many devices."""
    msg = json.loads(body)
    if event_type == "devices":
        return {"devices": len(device_gids(msg))}
    if event_type != "usage":
        return None
    kwh = {}
    for d in (msg.get("deviceListUsages") or {}).get("devices") or []:
        for c in d.get("channelUsages") or []:
            if c.get("usage") is not None:
                kwh[c.get("name") or str(c.get("channelNum"))] = round(c["usage"], 4)
    return {"kwh": kwh}


def next_window_end(now):
    return (int(now) // (WINDOW_MINUTES * 60) + 1) * WINDOW_MINUTES * 60


class Flume:
    def __init__(self, conn, vue):
        self.conn, self.vue = conn, vue
        self.gids, self.devices_at = [], 0.0

    def get(self, path):
        r = self.vue.auth.request("get", path)
        r.raise_for_status()
        return r.json()

    def refresh_devices(self):
        devices = self.get(API_CUSTOMER_DEVICES)
        db.append(self.conn, source="emporia", event_type="devices", event_name=None, body=devices)
        self.gids, self.devices_at = device_gids(devices), time.time()
        log.info("Emporia devices: %s", self.gids)

    def store_usage(self, until):
        usage = self.get(usage_path(self.gids, until))
        db.append(self.conn, source="emporia", event_type="usage", event_name=SCALE,
                  ts_ms=until * 1000, body=usage)

    def run_forever(self):
        until = next_window_end(time.time())
        while True:
            time.sleep(max(0, until + LAG_S - time.time()))
            try:
                if not self.gids or time.time() - self.devices_at > DEVICES_REFRESH_S:
                    self.refresh_devices()
                self.store_usage(until)
            except (requests.RequestException, ValueError) as e:
                log.warning("window ending %s not stored: %s", until, e)
            until = max(until + WINDOW_MINUTES * 60, next_window_end(time.time() - LAG_S))


def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    username, password = config.params_or_wait("emporia/username", "emporia/password", log=log)
    vue = PyEmVue()
    if not vue.login(username=username, password=password):
        log.error("Emporia sign-in failed; trying again in %ss", SIGN_IN_RETRY_S)
        time.sleep(SIGN_IN_RETRY_S)
        raise SystemExit(1)
    Flume(conn, vue).run_forever()


if __name__ == "__main__":
    main()
