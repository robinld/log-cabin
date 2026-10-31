# log cabin

A place for your logs. 
Aggregate logs across various IoT systems and query them.


## Code layout

| Path | What |
|---|---|
| `infra/` | Terraform: instance, data volume + snapshot policy, SSM secrets |
| `app/home_events/db.py` | One append-only `log` table (each message as received, plus `source`, `event_type`, `event_name`, `device_id` and `ts` columns) and the SQL views that interpret it |
| `app/home_events/flumes/` | One **flume** per home system: each carries that system's messages into `log`, as received |
| `app/home_events/flumes/yolink.py` | MQTT subscriber plus a periodic `getState` poll, so changes MQTT missed still show up |
| `app/home_events/flumes/evl.py` | EnvisaLink (Honeywell alarm panel) TPI session over the WireGuard tunnel; stores every message raw; views derive zones, open intervals and troubles. Also sends three keypad actions (chime, arm stay, disarm) with a code typed for each |
| `app/home_events/flumes/unifi.py` | Syslog listener (UDP and TCP, port 5514) for the UniFi console's SIEM Server integration; stores each CEF line raw |
| `app/home_events/flumes/lutron.py` | Read-only Lutron Caseta LEAP session (dimmer and switch levels, Pico presses, occupancy); `python -m home_events.flumes.lutron pair` gets the bridge's client certificate |
| `app/home_events/flumes/lennox.py` | Read-only Lennox iComfort S30 local API session (zones, setpoints, equipment, alerts) |
| `app/install.sh` | Idempotent installer the box runs on first boot and on every deploy |
| `scripts/` | `set-secrets.sh`, `deploy.sh`, `dashboard.sh` |

## Setup

Prerequisites: AWS CLI with credentials and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html), Terraform ≥ 1.6, plus:

- **YoLink UAC**: in the YoLink app, Account → Advanced Settings → User Access Credentials. You get a UAID and a secret.
- **WireGuard client config** (required): the box has no public IPv4 address, so this tunnel carries all of its IPv4, including the GitHub download at first boot, out through the house. Create a WireGuard VPN server on the UniFi gateway, add a client, and download its `.conf`. Keep `AllowedIPs = 0.0.0.0/0` and set `Endpoint` to the gateway's IPv6 address; IPv6 (SSM, package repos) stays on the box's own connection, and `::/0` is ignored. `set-secrets.sh` uploads the file. If the home device rejects connections from other subnets, add a UniFi NAT **Masquerade** policy (source: the VPN subnet; destination: the device; interface: the LAN).
- **EnvisaLink** (optional, needs the WireGuard tunnel): its LAN address and TPI password, which is the same as its web login password. `set-secrets.sh` asks for both.
- **UniFi SIEM logs** (optional, needs the WireGuard tunnel, with the gateway's VPN address in `AllowedIPs`): in UniFi, Settings → Control Plane → Integrations → Activity Logging → SIEM Server, pointed at the box's VPN address, port 5514.
- **Lutron Caseta** (optional, needs the WireGuard tunnel): set `lutron/host` to the Smart Bridge's LAN address, deploy, then pair once while someone presses the button on the back of the bridge: run `python -m home_events.flumes.lutron pair` on the box (as root, with `/etc/home-events/env` loaded). It stores the certificate in SSM itself.
- **Lennox S30** (optional, needs the WireGuard tunnel): set `lennox/host` to the thermostat's LAN address. Its local API has no authentication; the flume only subscribes and reads, never publishes.

```bash
# 0. The box deploys the latest GitHub release, so publish one first
gh release create v0.1.0 --generate-notes

cd infra
terraform init

# 1. Create the empty secret parameters first, so the box finds real values on first boot
terraform apply -target=aws_ssm_parameter.secret
../scripts/set-secrets.sh

# 2. Everything else
terraform apply
```

Run `scripts/dashboard.sh` and open http://localhost:8001 for Datasette. With the WireGuard tunnel up, it's also at `http://<box's VPN address>:8001` from the home LAN, with no login: anyone on that network can read the log.

`/home` is a phone-sized status page: alarm state, doors, temperatures, water and feed health. Its **Live** tab (`/home#live`) streams new events one line each, newest first, with repeats left out.

Opened through Tailscale (`https://log-cabin.<tailnet>.ts.net/home`), the page also shows **Chime**, **Stay** and **Disarm** buttons. You type your alarm code with each one, as at a wall keypad; it goes to the panel as that keypress and is never stored or logged. Only those three actions exist (no raw keys, so nothing can reach installer programming), at most 5 per 10 minutes, so trying every possible code would take about two weeks. The EVL flume serves them on `127.0.0.1:8002`, and `install.sh` publishes that at `/alarm` with Tailscale Serve; over the home LAN they don't exist and the buttons don't appear. Anyone on your tailnet who knows a code can use them, so keep assistants off port 443 with an access rule before adding one.

**Tailscale** (optional): the box runs `tailscaled`, so chosen devices on your tailnet (a phone, or an assistant such as Muse through its Tailscale connector) can reach Datasette without a public endpoint. Sign a new box in once, as root over SSM: `tailscale up --hostname=log-cabin --accept-dns=false`, then open the link it prints, then `tailscale serve --bg 8001` (and run `app/install.sh` again, or `scripts/deploy.sh`, to add `/alarm`) to publish Datasette at `https://log-cabin.<tailnet>.ts.net` with a real certificate (enable Serve for the tailnet when it asks). A replacement box needs both steps again. New tailnets allow every device to reach every port, so add an access rule, like the one in `infra/tailscale-policy.hujson`, that lets assistants reach only `log-cabin:443`; port 5514 accepts UniFi log lines from anyone who can reach it.

To ship changes: push, publish a release (`gh release create vX.Y.Z --generate-notes`),
then run `scripts/deploy.sh`. Pushing to `main` alone deploys nothing.
