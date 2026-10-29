"""Lutron Caseta Smart Bridge (LEAP) -> SQLite. Read-only.

Holds a LEAP session (JSON lines over TLS, port 8081) with the bridge on the
home LAN, over the WireGuard tunnel, and appends every message the bridge
sends to the log table as received: dimmer and switch levels, Pico button
presses, occupancy, plus the device, area and button lists at each connect.
Only ReadRequest, SubscribeRequest and pings are ever sent.

The bridge only talks to clients holding a certificate it signed. Get one
once, on the box, while someone presses the button on the back of the bridge:

    python -m home_events.flumes.lutron pair

pylutron-caseta generates the key and handles the pairing; the certificate
goes straight into SSM (lutron/certs) and is never printed.
"""

import asyncio
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import time
import uuid

from .. import config, db

log = logging.getLogger("lutron")

PORT = 8081
PING_EVERY_S = 300
# Pings get a reply, so this much silence means the session is dead.
IDLE_TIMEOUT_S = PING_EVERY_S + 120
MIN_BACKOFF_S, MAX_BACKOFF_S = 5, 300
RESOURCE = re.compile(r"^/(zone|button|occupancygroup|device|area|led)/(\d+)")


def lutron_fields(msg):
    """A LEAP message -> (event_type, event_name, device_id) for the log columns.

    event_type is the bridge's MessageBodyType (OneZoneStatus,
    OneButtonStatusEvent, MultipleDeviceDefinition, ...), event_name the
    resource it is about (/zone/2/status), device_id e.g. "zone/2".
    """
    header = msg.get("Header", {}) if isinstance(msg, dict) else {}
    url = header.get("Url")
    m = RESOURCE.match(url or "")
    return (header.get("MessageBodyType") or header.get("CommuniqueType") or "unknown",
            url, f"{m.group(1)}/{m.group(2)}" if m else None)


def tls_context(certs):
    """An SSL context from the paired certificate. ssl only loads keys from files."""
    with tempfile.TemporaryDirectory() as d:
        paths = {}
        for k in ("key", "cert", "ca"):
            paths[k] = os.path.join(d, k)
            fd = os.open(paths[k], os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(certs[k])
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_verify_locations(paths["ca"])
        ctx.load_cert_chain(paths["cert"], paths["key"])
    ctx.check_hostname = False  # the bridge's certificate names no hostname
    return ctx


class Flume:
    def __init__(self, conn, host, ctx):
        self.conn, self.host, self.ctx = conn, host, ctx
        self.writer = None

    def send(self, communique_type, url):
        cmd = {"CommuniqueType": communique_type,
               "Header": {"ClientTag": str(uuid.uuid4()), "Url": url}}
        self.writer.write(json.dumps(cmd).encode() + b"\r\n")

    def store(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            msg = None
        event_type, event_name, device_id = lutron_fields(msg)
        db.append(self.conn, source="lutron", event_type=event_type,
                  event_name=event_name, device_id=device_id, body=line)
        return msg

    async def ping_forever(self):
        while True:
            self.send("ReadRequest", "/server/1/status/ping")
            await self.writer.drain()
            await asyncio.sleep(PING_EVERY_S)

    async def session(self):
        reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, PORT, ssl=self.ctx, limit=2**20), 15)
        pinger = None
        try:
            for url in ("/device", "/area", "/button", "/occupancygroup"):
                self.send("ReadRequest", url)
            self.send("SubscribeRequest", "/occupancygroup/status")
            await self.writer.drain()
            log.info("connected to %s", self.host)
            pinger = asyncio.create_task(self.ping_forever())
            while True:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_S)
                if not line:
                    raise ConnectionError("bridge closed the connection")
                msg = self.store(line.decode("utf-8", "replace").strip())
                await self.follow_up(msg)
        finally:
            if pinger:
                pinger.cancel()
            self.writer.close()

    async def follow_up(self, msg):
        """Once the lists arrive: baseline every zone, subscribe to every button."""
        if not isinstance(msg, dict):
            return
        url = msg.get("Header", {}).get("Url")
        body = msg.get("Body") or {}
        if url == "/device":
            for d in body.get("Devices", []):
                for z in d.get("LocalZones", []):
                    self.send("ReadRequest", f"{z['href']}/status")
        elif url == "/button":
            for b in body.get("Buttons", []):
                self.send("SubscribeRequest", f"{b['href']}/status/event")
        else:
            return
        await self.writer.drain()

    async def run_forever(self):
        backoff = MIN_BACKOFF_S
        while True:
            started = time.time()
            try:
                await self.session()
            except (OSError, TimeoutError, ssl.SSLError) as e:
                log.warning("session ended: %s", e or type(e).__name__)
            if time.time() - started > 600:
                backoff = MIN_BACKOFF_S  # it was up a while; this is a fresh failure
            log.info("reconnecting in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)


async def pair():
    """Pair with the bridge (press its button now) and store the cert in SSM."""
    from pylutron_caseta.pairing import async_pair

    host = config.param("lutron/host")
    print(f"Pairing with {host}: press the small button on the back of the bridge now.", flush=True)
    data = await async_pair(host)
    config.ssm().put_parameter(
        Name=f"{os.environ['PARAM_PREFIX']}/lutron/certs", Type="SecureString",
        Value=json.dumps({k: data[k] for k in ("key", "cert", "ca")}), Overwrite=True)
    print(f"Paired; bridge runs LEAP {data['version']}. Certificate stored in SSM lutron/certs.")


async def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    ctx = tls_context(json.loads(config.param("lutron/certs")))
    await Flume(conn, config.param("lutron/host"), ctx).run_forever()


if __name__ == "__main__":
    asyncio.run(pair() if sys.argv[1:] == ["pair"] else main())
