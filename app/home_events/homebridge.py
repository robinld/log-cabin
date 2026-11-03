"""Write Homebridge's config.json from SSM.

Homebridge runs beside our own HomeKit bridge, for YoLink sensors and Kasa
plugs (at the addresses in SSM homebridge/kasa_hosts, if set), as the
homebridge user from /data/homebridge (the root disk is too small for its
packages). It advertises on mdns0 like our bridge; the relay points its
address records at the box's WireGuard address.

Only YoLink sensor types go to HomeKit: a relay or valve there could be switched off
from any Home device. The bridge's identity (username and pin) is kept from
the existing file, so re-running never unpairs it from Home.

Once Homebridge is paired with Home, this also pairs the Homebridge flume as
one more, non-admin, HomeKit controller: it adds the flume's public key to
Homebridge's pairing file (Homebridge must be stopped, or it would write its
own copy back) and writes the flume's key and Homebridge's to CONTROLLER,
readable only by the flume's user. Not before: a pairing makes HAP-NodeJS
treat the bridge as paired, so Home could no longer add it.

Run as root from install.sh, with Homebridge stopped; safe to re-run.
"""
import json
import os
import random
import shutil
import sys
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from home_events import config

STORAGE = Path("/data/homebridge/storage")
PORT = 51828
USER = "homebridge"
CONTROLLER = Path("/data/homebridge-controller.json")
FLUME_USER = "homeevents"
USER_PERMISSION, ADMIN_PERMISSION = 0, 1
SENSORS = ["DoorSensor", "LeakSensor", "THSensor", "MotionSensor", "VibrationSensor",
           "COSmokeSensor", "PowerFailureAlarm"]


def new_identity():
    mac = [0x0E] + [random.randrange(256) for _ in range(5)]
    return {"username": ":".join("%02X" % b for b in mac),
            "pin": "%03d-%02d-%03d" % (random.randrange(1000), random.randrange(100), random.randrange(1000))}


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
             "allDevices": False, "includeTypes": SENSORS,
             # Off, the plugin logs each state it receives at info; on (its
             # default), only at debug, which Homebridge doesn't record.
             "liteLog": False},
        ] + ([
            # Discovery broadcasts don't cross the tunnel, so each plug is listed.
            {"platform": "TplinkSmarthome", "name": "Kasa", "deviceTypes": ["plug"],
             "devices": [{"host": h} for h in kasa_hosts]},
        ] if kasa_hosts else []),
    }


def new_controller():
    key = ed25519.Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    return {"id": str(uuid.uuid4()).upper(),
            "ltsk": key.private_bytes(raw, serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption()).hex(),
            "ltpk": key.public_key().public_bytes(raw, serialization.PublicFormat.Raw).hex()}


def accessory_info_path(username):
    """HAP-NodeJS's pairing file for the bridge with this username."""
    return STORAGE / "persist" / f"AccessoryInfo.{username.replace(':', '').upper()}.json"


def has_admin(info):
    perms = info.get("pairedClientsPermission") or {}
    # Pairings saved without a permission are admins (AccessoryInfo.load).
    return any(perms.get(u, ADMIN_PERMISSION) == ADMIN_PERMISSION for u in info.get("pairedClients") or {})


def with_controller(info, controller):
    """info with the controller paired as a non-admin, or None if it already is."""
    clients = dict(info.get("pairedClients") or {})
    perms = dict(info.get("pairedClientsPermission") or {})
    if clients.get(controller["id"]) == controller["ltpk"] and perms.get(controller["id"]) == USER_PERMISSION:
        return None
    clients[controller["id"]] = controller["ltpk"]
    perms[controller["id"]] = USER_PERMISSION
    return {**info, "pairedClients": clients, "pairedClientsPermission": perms}


def write_private(path, text, owner):
    tmp = path.with_suffix(".new")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    shutil.chown(tmp, owner, owner)
    tmp.replace(path)


def pair_flume(username):
    path = accessory_info_path(username)
    info = json.loads(path.read_text()) if path.exists() else None
    if not info or not has_admin(info):
        return "flume not paired: pair Homebridge with Home, then deploy again"
    controller = json.loads(CONTROLLER.read_text()) if CONTROLLER.exists() else new_controller()
    write_private(CONTROLLER, json.dumps({**controller, "accessory_id": username,
                                          "accessory_ltpk": info["signPk"], "port": PORT}), FLUME_USER)
    updated = with_controller(info, controller)
    if updated:
        write_private(path, json.dumps(updated), USER)
        return "flume paired"
    return "flume already paired"


def main():
    path = STORAGE / "config.json"
    old = json.loads(path.read_text()) if path.exists() else None
    kasa = (config.optional_param("homebridge/kasa_hosts") or "").replace(",", " ").split()
    cfg = build(old, config.param("yolink/uaid"), config.param("yolink/secret"), kasa)
    STORAGE.mkdir(parents=True, exist_ok=True)
    write_private(path, json.dumps(cfg, indent=2), USER)
    print("homebridge config ok" + ("" if old else ": new bridge, pair it in Home with " + cfg["bridge"]["pin"]))
    print("homebridge " + pair_flume(cfg["bridge"]["username"]))


if __name__ == "__main__":
    sys.exit(main())
