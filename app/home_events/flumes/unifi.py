"""UniFi SIEM syslog -> SQLite.

The UDM's "SIEM Server" integration (Settings > Control Plane > Integrations >
Activity Logging) streams its events as syslog lines in CEF format. This
listens for them on the WireGuard tunnel, over UDP and TCP, and appends each
line to the log table as received. Nothing is sent back to the UDM.

CEF:Version|Vendor|Product|Version|SignatureID|Name|Severity|key=value ...
event_type is the product (e.g. "UniFi Network"), event_name the Name.
"""

import asyncio
import logging
import os
import re

from .. import config, db

log = logging.getLogger("unifi")

PORT = int(os.environ.get("UNIFI_SYSLOG_PORT", "5514"))
# TCP syslog is either one message per line or "<length> <message>" (RFC 6587).
OCTET_COUNT = re.compile(r"^\d+ (?=<)")


def cef_fields(line):
    """A syslog line -> (event_type, event_name). Non-CEF lines are plain syslog."""
    i = line.find("CEF:")
    if i < 0:
        return "syslog", None
    # Header fields are separated by "|"; a literal pipe is escaped as "\|".
    parts = re.split(r"(?<!\\)\|", line[i:], maxsplit=7)
    if len(parts) < 7:
        return "syslog", None
    # parts: CEF:0, vendor, product, version, signature id, name, severity, extension
    return parts[2] or "unknown", parts[5] or None


PARSE_VERSION = 1
# CEF extension values run to the next " key=", or the end; msg may be quoted.
EXTENSION = re.compile(r'(\w+)=("[^"]*"|.*?)(?=\s\w+=|$)')
# "Upper Doorbell has recorded a person." / "Upper Doorbell detected someone speaking."
DETECTED = re.compile(r"^(?P<camera>.+?) (?:has recorded|detected) (?:an? )?(?P<what>.+?)\.?$")
RINGING = re.compile(r"^Someone is ringing (?P<camera>.+?)\.?$")


def parse(event_type, event_name, body):
    """A CEF line -> its product, event, severity and key=value fields
    (UniFi's own UNIFI prefix dropped), plus the camera and what it saw for
    detections."""
    i = body.find("CEF:")
    if i < 0:
        return {"message": body}
    parts = re.split(r"(?<!\\)\|", body[i:], maxsplit=7)
    if len(parts) < 8:
        return None
    out = {"product": parts[2], "version": parts[3], "event": parts[5]}
    if parts[6].isdigit():
        out["severity"] = int(parts[6])
    for key, value in EXTENSION.findall(parts[7]):
        key = key.removeprefix("UNIFI")
        key = key[:1].lower() + key[1:]
        out.setdefault(key, value[1:-1] if value.startswith('"') and value.endswith('"') else value)
    if out.get("eventType") == out["event"]:
        del out["eventType"]
    if str(out.get("timestamp", "")).isdigit():
        out["timestamp"] = db.iso(int(out["timestamp"]))
    m = DETECTED.match(out.get("msg", "")) or RINGING.match(out.get("msg", ""))
    if m:
        out["camera"] = m["camera"]
        if "what" in m.groupdict():
            out["detected"] = m["what"]
    return out


def store(conn, line):
    line = OCTET_COUNT.sub("", line.strip())
    if not line:
        return
    event_type, event_name = cef_fields(line)
    db.append(conn, source="unifi", event_type=event_type, event_name=event_name, body=line)


class UDP(asyncio.DatagramProtocol):
    def __init__(self, conn):
        self.conn = conn

    def datagram_received(self, data, addr):
        for line in data.decode("utf-8", "replace").splitlines():
            store(self.conn, line)


async def main():
    config.setup_logging()
    conn = db.connect()
    db.init(conn)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: UDP(conn), local_addr=("0.0.0.0", PORT))

    async def tcp_client(reader, writer):
        peer = writer.get_extra_info("peername")
        log.info("TCP sender connected: %s", peer)
        try:
            while line := await reader.readline():
                store(conn, line.decode("utf-8", "replace"))
        finally:
            writer.close()

    server = await asyncio.start_server(tcp_client, "0.0.0.0", PORT)
    log.info("listening for syslog on UDP and TCP port %d", PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
