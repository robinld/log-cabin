"""Bring up the WireGuard tunnel to the home network, if one is configured.

The client config (as downloaded from the UniFi WireGuard VPN server) lives in
SSM at <PARAM_PREFIX>/wireguard/conf. Its AllowedIPs is either the home
addresses the box needs, e.g. 192.168.1.0/24, or 0.0.0.0/0 to send all of the
box's IPv4 through the house. That lets the box run without a public IPv4
address: GitHub and YoLink's cloud have no IPv6, but SSM and package repos do,
so IPv6 stays on the box's own connection. A full IPv4 tunnel therefore needs
an IPv6 Endpoint (an IPv4 one would route the tunnel into itself), and ::/0 is
dropped from AllowedIPs, as the UniFi download includes it.

The tunnel is run by systemd-networkd, which Amazon Linux 2023 already uses,
so no extra packages are needed. Run as root from install.sh; safe to re-run.
At first boot the bootstrap runs it with "-" and the config on stdin, since
the tunnel has to be up before the rest of the code can be fetched.
"""
import configparser
import grp
import logging
import os
import subprocess
import sys
from pathlib import Path

from home_events import config

log = logging.getLogger("wireguard")

IFACE = "wg0"
NETDEV = Path(f"/etc/systemd/network/50-{IFACE}.netdev")
NETWORK = Path(f"/etc/systemd/network/50-{IFACE}.network")
# AWS's link-local services (instance metadata and credentials, DNS, time)
# must stay on the box's own interface when the tunnel takes the IPv4 default
# route. A drop-in for the network file amazon-ec2-net-utils generates.
UPLINK = "ens5"
UPLINK_DROPIN = Path(f"/etc/systemd/network/70-{UPLINK}.network.d/50-aws-link-local.conf")
UPLINK_ROUTES = "[Route]\nDestination=169.254.169.0/24\nGateway=_dhcp4\n"


def parse(text):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # WireGuard keys are case-sensitive in our output
    cp.read_string(text)
    iface, peer = cp["Interface"], cp["Peer"]
    allowed = [a.strip() for a in peer["AllowedIPs"].split(",") if a.strip()]
    if "::/0" in allowed:
        log.info("ignoring ::/0 in AllowedIPs; IPv6 uses the box's own connection")
        allowed.remove("::/0")
    if "0.0.0.0/0" in allowed and not peer["Endpoint"].startswith("["):
        raise SystemExit("wireguard/conf sends all IPv4 through the tunnel, so its Endpoint must be IPv6, e.g. [2001:db8::1]:51820")
    return {
        "private_key": iface["PrivateKey"],
        "addresses": [a.strip() for a in iface["Address"].split(",") if a.strip()],
        "public_key": peer["PublicKey"],
        "preshared_key": peer.get("PresharedKey"),
        "endpoint": peer["Endpoint"],
        "allowed": allowed,
    }


def render(c):
    netdev = [
        "[NetDev]", f"Name={IFACE}", "Kind=wireguard", "",
        "[WireGuard]", f"PrivateKey={c['private_key']}", "",
        "[WireGuardPeer]", f"PublicKey={c['public_key']}",
    ]
    if c["preshared_key"]:
        netdev.append(f"PresharedKey={c['preshared_key']}")
    netdev += [
        f"Endpoint={c['endpoint']}",
        f"AllowedIPs={','.join(c['allowed'])}",
        # Keeps the UDP flow alive through the security group's and the
        # UDM's connection tracking while the TPI session is idle.
        "PersistentKeepalive=25",
    ]
    network = ["[Match]", f"Name={IFACE}", "", "[Network]"]
    network += [f"Address={a}" for a in c["addresses"]]
    for dest in c["allowed"]:
        network += ["", "[Route]", f"Destination={dest}"]
    return "\n".join(netdev) + "\n", "\n".join(network) + "\n"


def write_if_changed(path, text, mode, group):
    if path.exists() and path.read_text() == text:
        return False
    path.write_text(text)
    os.chown(path, 0, grp.getgrnam(group).gr_gid)
    os.chmod(path, mode)
    return True


def main():
    config.setup_logging()
    if sys.argv[1:] == ["-"]:
        text = sys.stdin.read()
    else:
        try:
            text = config.param("wireguard/conf")
        except SystemExit as e:
            log.warning("skipping WireGuard: %s", e)
            return
    netdev, network = render(parse(text))
    # The netdev holds the private key; systemd-networkd reads it as its group.
    changed = write_if_changed(NETDEV, netdev, 0o640, "systemd-network")
    changed |= write_if_changed(NETWORK, network, 0o644, "root")
    UPLINK_DROPIN.parent.mkdir(exist_ok=True)
    changed |= write_if_changed(UPLINK_DROPIN, UPLINK_ROUTES, 0o644, "root")
    if changed:
        # networkd won't reconfigure an existing WireGuard netdev on reload.
        subprocess.run(["ip", "link", "delete", IFACE], check=False, capture_output=True)
        subprocess.run(["networkctl", "reload"], check=True)
        log.info("WireGuard %s configured", IFACE)
    else:
        log.info("WireGuard %s unchanged", IFACE)


if __name__ == "__main__":
    main()
