import socket
import struct

from home_events import mdns_relay


def test_incoming_takes_a_new_connection_from_the_peer_in_place_of_the_old(monkeypatch):
    monkeypatch.setattr(mdns_relay, "PORT", 0)
    link = mdns_relay.Incoming("127.0.0.1", "127.0.0.1")
    port = link.srv.getsockname()[1]
    first = socket.create_connection(("127.0.0.1", port))
    t = link.connect()
    assert t.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    assert link.replacement([]) is None
    second = socket.create_connection(("127.0.0.1", port))
    new = link.replacement([link.srv])
    assert new is not None and new.getpeername() == second.getsockname()
    for s in (first, second, t, new, link.srv):
        s.close()


def test_incoming_refuses_other_addresses(monkeypatch):
    monkeypatch.setattr(mdns_relay, "PORT", 0)
    link = mdns_relay.Incoming("127.0.0.1", "192.0.2.1")
    c = socket.create_connection(("127.0.0.1", link.srv.getsockname()[1]))
    assert link.accept() is None
    c.close()
    link.srv.close()


def record(name, rtype, rdata):
    return name + struct.pack("!HHIH", rtype, 0x8001, 120, len(rdata)) + rdata


def test_readdress_rewrites_only_a_records_for_the_old_address():
    old, new = socket.inet_aton("169.254.53.1"), socket.inet_aton("192.168.4.2")
    host = b"\x04Host\x05local\x00"
    other = record(b"\xc0\x0c", 1, socket.inet_aton("10.0.0.5"))
    txt = record(b"\xc0\x0c", 16, b"\x04" + old)  # the same bytes, but not an address
    packet = struct.pack("!6H", 0, 0x8400, 1, 2, 0, 2) + host + b"\x00\x01\x00\x01" \
        + record(b"\xc0\x0c", 1, old) + txt + other + record(host, 1, old)
    out = mdns_relay.readdress(packet, old, new)
    assert len(out) == len(packet)
    assert out.count(new) == 2 and out.count(old) == 1  # the TXT copy stays
    assert other in out


def test_readdress_leaves_a_truncated_packet_alone():
    packet = struct.pack("!6H", 0, 0x8400, 0, 3, 0, 0) + b"\x04Host"
    assert mdns_relay.readdress(packet, b"\x01\x02\x03\x04", b"\x05\x06\x07\x08") == packet
