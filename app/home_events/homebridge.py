"""Write Homebridge's config.json from SSM.

Homebridge runs beside our own HomeKit bridge, for YoLink sensors and Kasa
plugs (at the addresses in SSM homebridge/kasa_hosts, if set), as the
homebridge user from /data/homebridge (the root disk is too small for its
packages). It advertises on mdns0 like our bridge; the relay points its
address records at the box's WireGuard address.

Only YoLink sensor types go to HomeKit: a relay or valve there could be switched off
from any Home device. The bridge's identity (username and pin) is kept from
the existing file, so re-running never unpairs it from Home. Run as root from
install.sh; safe to re-run.
"""
import json
import os
import random
import shutil
import sys
from pathlib import Path

from home_events import config

STORAGE = Path("/data/homebridge/storage")
PORT = 51828
USER = "homebridge"
SENSORS = ["DoorSensor", "LeakSensor", "THSensor", "MotionSensor", "VibrationSensor",
           "COSmokeSensor", "PowerFailureAlarm"]


def new_identity():
    mac = [0x0E] + [random.randrange(256) for _ in range(5)]
    return {"username": ":".join("%02X" % b for b in mac),
            "pin": "%03d-%02d-%03d" % (random.randrange(1000), random.randrange(100), random.randrange(1000))}


def optional_param(name):
    try:
        return config.param(name)
    except config.ssm().exceptions.ParameterNotFound:
        return None


def build(old, uaid, secret, kasa_hosts=()):
    """The config for Homebridge, keeping the bridge identity from old (a config or None)."""
    bridge = (old or {}).get("bridge") or {}
    identity = {k: bridge[k] for k in ("username", "pin") if k in bridge} or new_identity()
    return {
        "bridge": {"name": "Log Cabin Homebridge", **identity, "port": PORT,
                   # Listen on every IPv4 address; advertise on mdns0, where the relay hears it.
                   "bind": ["0.0.0.0", "mdns0"], "advertiser": "ciao"},
        "platforms": [
            {"platform": "YoLink", "name": "YoLink", "userAccessId": uaid, "secretKey": secret,
             "allDevices": False, "includeTypes": SENSORS},
        ] + ([
            # Discovery broadcasts don't cross the tunnel, so each plug is listed.
            {"platform": "TplinkSmarthome", "name": "Kasa", "deviceTypes": ["plug"],
             "devices": [{"host": h} for h in kasa_hosts]},
        ] if kasa_hosts else []),
    }


def main():
    path = STORAGE / "config.json"
    old = json.loads(path.read_text()) if path.exists() else None
    kasa = (optional_param("homebridge/kasa_hosts") or "").replace(",", " ").split()
    cfg = build(old, config.param("yolink/uaid"), config.param("yolink/secret"), kasa)
    STORAGE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".new")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    shutil.chown(tmp, USER, USER)
    tmp.replace(path)
    print("homebridge config ok" + ("" if old else ": new bridge, pair it in Home with " + cfg["bridge"]["pin"]))


if __name__ == "__main__":
    sys.exit(main())
