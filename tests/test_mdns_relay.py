import socket

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
