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
| `app/home_events/flumes/evl.py` | Read-only EnvisaLink (Honeywell alarm panel) TPI session over the WireGuard tunnel; stores every message raw; views derive zones, open intervals and troubles |
| `app/home_events/flumes/unifi.py` | Syslog listener (UDP and TCP, port 5514) for the UniFi console's SIEM Server integration; stores each CEF line raw |
| `app/home_events/flumes/lutron.py` | Read-only Lutron Caseta LEAP session (dimmer and switch levels, Pico presses, occupancy); `python -m home_events.flumes.lutron pair` gets the bridge's client certificate |
| `app/install.sh` | Idempotent installer the box runs on first boot and on every deploy |
| `scripts/` | `set-secrets.sh`, `deploy.sh`, `dashboard.sh` |

## Setup

Prerequisites: AWS CLI with credentials and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html), Terraform ≥ 1.6, plus:

- **YoLink UAC**: in the YoLink app, Account → Advanced Settings → User Access Credentials. You get a UAID and a secret.
- **WireGuard client config** (optional, for reaching devices on the home LAN): create a WireGuard VPN server on the UniFi gateway, add a client, and download its `.conf`. Edit its `AllowedIPs` to the home LAN (e.g. `192.168.1.0/24`) or just the addresses the box needs; a full tunnel is refused. `set-secrets.sh` uploads the file. If the home device rejects connections from other subnets, add a UniFi NAT **Masquerade** policy (source: the VPN subnet; destination: the device; interface: the LAN).
- **EnvisaLink** (optional, needs the WireGuard tunnel): its LAN address and TPI password, which is the same as its web login password. `set-secrets.sh` asks for both.
- **UniFi SIEM logs** (optional, needs the WireGuard tunnel, with the gateway's VPN address in `AllowedIPs`): in UniFi, Settings → Control Plane → Integrations → Activity Logging → SIEM Server, pointed at the box's VPN address, port 5514.
- **Lutron Caseta** (optional, needs the WireGuard tunnel): set `lutron/host` to the Smart Bridge's LAN address, deploy, then pair once while someone presses the button on the back of the bridge: run `python -m home_events.flumes.lutron pair` on the box (as root, with `/etc/home-events/env` loaded). It stores the certificate in SSM itself.

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

To ship changes: push, publish a release (`gh release create vX.Y.Z --generate-notes`),
then run `scripts/deploy.sh`. Pushing to `main` alone deploys nothing.
