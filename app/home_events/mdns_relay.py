"""Bonjour (mDNS) relay between the home LAN and the HomeKit bridge.

HomeKit only finds accessories by multicast on the same network, and the
gateway doesn't carry multicast over WireGuard. So this carries mDNS packets,
as received, over one TCP connection on the tunnel:

  box:  joins 224.0.0.251 on mdns0, where the bridge advertises, and listens
        on its WireGuard address, port PORT, for the LAN side. It accepts only
        the address in SSM homekit/relay_peer. Runs as home-events-mdns-relay.
  lan:  runs on any always-on machine at home:
            python3 mdns_relay.py lan <its LAN address> <box's WireGuard address>
        It needs only the standard library. It sends the box HomeKit-related
        packets (KEEP) and re-multicasts everything the box sends.

HomeKit itself (HAP) then connects to the bridge directly over the tunnel.
"""

import hashlib
import logging
import select
import socket
import struct
import sys
import time

log = logging.getLogger("mdns_relay")

GROUP, MDNS_PORT = "224.0.0.251", 5353
PORT = 1900
MDNS_IP = "169.254.53.1"  # mdns0, see install.sh
KEEP = (b"_hap", b"logcabin", b"LogCabin", b"Log Cabin", b"Log-Cabin", b"log-cabin")
ECHO_S = 5  # ignore our own re-multicasts this long
IP_MULTICAST_ALL = 49  # Linux


def multicast_socket(local_ip, only_this_interface):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    if only_this_interface:
        u.setsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL, 0)
    u.bind(("", MDNS_PORT))
    u.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(GROUP) + socket.inet_aton(local_ip))
    u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
    u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    u.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    return u


def connect_out(box_ip):
    while True:
        try:
            t = socket.create_connection((box_ip, PORT), timeout=5)
            t.settimeout(None)
            return t
        except OSError as e:
            log.warning("connect to %s failed: %s", box_ip, e)
            time.sleep(5)


def accept_in(listen_ip, peer):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_ip, PORT))
    srv.listen(1)
    try:
        while True:
            t, addr = srv.accept()
            if addr[0] == peer:
                return t
            log.warning("refused %s", addr[0])
            t.close()
    finally:
        srv.close()


def relay(u, connect, forward_all, peer):
    """Copy packets both ways until the TCP connection drops, then reconnect."""
    injected = {}  # payload hash -> time, so our own re-multicasts aren't sent back
    while True:
        t, buf = connect(), b""
        log.info("connected")
        try:
            while True:
                r, _, _ = select.select([u, t], [], [], 30)
                now = time.time()
                injected = {h: ts for h, ts in injected.items() if now - ts < ECHO_S}
                if u in r:
                    data, src = u.recvfrom(9000)
                    if src[0] != peer and hashlib.sha1(data).digest() not in injected and (
                            forward_all or any(k in data for k in KEEP)):
                        t.sendall(struct.pack("!H", len(data)) + data)
                if t in r:
                    chunk = t.recv(65536)
                    if not chunk:
                        raise ConnectionError("closed by the other side")
                    buf += chunk
                    while len(buf) >= 2 and len(buf) >= 2 + struct.unpack("!H", buf[:2])[0]:
                        n = struct.unpack("!H", buf[:2])[0]
                        data, buf = buf[2:2 + n], buf[2 + n:]
                        injected[hashlib.sha1(data).digest()] = now
                        u.sendto(data, (GROUP, MDNS_PORT))
        except OSError as e:
            log.warning("disconnected: %s", e)
        finally:
            t.close()
        time.sleep(1)


def main():
    side = sys.argv[1] if len(sys.argv) > 1 else ""
    if side == "box":
        from . import config
        from .homekit import wg_address
        config.setup_logging()
        peer = config.param("homekit/relay_peer")
        listen_ip = wg_address()
        relay(multicast_socket(MDNS_IP, True), lambda: accept_in(listen_ip, peer), True, peer)
    elif side == "lan" and len(sys.argv) == 4:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        lan_ip, box_ip = sys.argv[2], sys.argv[3]
        relay(multicast_socket(lan_ip, False), lambda: connect_out(box_ip), False, box_ip)
    else:
        sys.exit("usage: mdns_relay box | mdns_relay lan <LAN address> <box's WireGuard address>")


if __name__ == "__main__":
    main()
