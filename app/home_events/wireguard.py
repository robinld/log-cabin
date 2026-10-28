"""Bring up the WireGuard tunnel to the home network, if one is configured.

The client config (as downloaded from the UniFi WireGuard VPN server) lives in
SSM at <PARAM_PREFIX>/wireguard/conf. Its AllowedIPs must list only the home
addresses the box needs, e.g. the EnvisaLink's /32; a full tunnel would send
SSM, YoLink and everything else through the house, so it is refused.

The tunnel is run by systemd-networkd, which Amazon Linux 2023 already uses,
so no extra packages are needed. Run as root from install.sh; safe to re-run.
"""
import configparser
import grp
import logging
import os
import subprocess
from pathlib import Path

from home_events import config

log = logging.getLogger("wireguard")

IFACE = "wg0"
NETDEV = Path(f"/etc/systemd/network/50-{IFACE}.netdev")
NETWORK = Path(f"/etc/systemd/network/50-{IFACE}.network")
FULL_TUNNEL = {"0.0.0.0/0", "::/0"}


def parse(text):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # WireGuard keys are case-sensitive in our output
    cp.read_string(text)
    iface, peer = cp["Interface"], cp["Peer"]
    allowed = [a.strip() for a in peer["AllowedIPs"].split(",") if a.strip()]
    if FULL_TUNNEL & set(allowed):
        raise SystemExit("wireguard/conf has a full-tunnel AllowedIPs; list only home addresses, e.g. 192.168.1.10/32")
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
    try:
        text = config.param("wireguard/conf")
    except SystemExit as e:
        log.warning("skipping WireGuard: %s", e)
        return
    netdev, network = render(parse(text))
    # The netdev holds the private key; systemd-networkd reads it as its group.
    changed = write_if_changed(NETDEV, netdev, 0o640, "systemd-network")
    changed |= write_if_changed(NETWORK, network, 0o644, "root")
    if changed:
        # networkd won't reconfigure an existing WireGuard netdev on reload.
        subprocess.run(["ip", "link", "delete", IFACE], check=False, capture_output=True)
        subprocess.run(["networkctl", "reload"], check=True)
        log.info("WireGuard %s configured", IFACE)
    else:
        log.info("WireGuard %s unchanged", IFACE)


if __name__ == "__main__":
    main()
