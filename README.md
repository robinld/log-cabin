# log-cabin

One stream of events from YoLink sensors, stored in
SQLite on a small EC2 box. You browse it with Datasette through an SSM port-forward.

```
YoLink cloud ──MQTT──▶ t4g.small ──▶ /data/events.db (SQLite, own EBS volume)
                                       │
                                       ├─ hourly + daily EBS snapshots (DLM)
                                       └─ Datasette (localhost) ◀──SSM port-forward── your laptop
```

No inbound ports and no third-party network. Shell and dashboard both go through AWS SSM Session Manager.

## What lives where

| Path | What |
|---|---|
| `infra/` | Terraform: instance, data volume + snapshot policy, SNS, SSM secrets, recovery alarms |
| `app/home_events/yolink_ingest.py` | MQTT subscriber plus a periodic `getState` poll that records any change MQTT missed |
| `app/install.sh` | Idempotent installer the box runs on first boot and on every deploy |
| `scripts/` | `set-secrets.sh`, `deploy.sh`, `dashboard.sh` |

## Setup

Prerequisites: AWS CLI with credentials and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html), Terraform ≥ 1.6, plus:

- **YoLink UAC**: in the YoLink app, Account → Advanced Settings → User Access Credentials. You get a UAID and a secret.

```bash
cd infra
terraform init

# 1. Create the empty secret parameters first, so the box finds real values on first boot
terraform apply -target=aws_ssm_parameter.secret
../scripts/set-secrets.sh

# 2. Everything else
terraform apply -var alert_email=you@example.com
#    then click the confirmation link SNS emails you
```

Run `scripts/dashboard.sh` and open http://localhost:8001 for Datasette.

### Day-2

- **Ship code changes**: `scripts/deploy.sh`. It re-uploads the bundle, then runs the installer over SSM.
- **Shell**: `aws ssm start-session --target $(terraform output -raw instance_id)`
- **Logs**: `journalctl -u 'home-events-*' -f`; first boot is in `/var/log/cloud-init-output.log`
- **Restore from a snapshot**: create a volume from the snapshot in the same AZ, then `terraform state rm aws_ebs_volume.data`, `terraform import aws_ebs_volume.data vol-…`, `terraform apply`. The attachment is recreated, and the bootstrap never reformats a volume that already has a filesystem.

## Design choices and their limits

- **Durability.** SQLite runs in WAL mode with `synchronous=FULL`, so every committed event is fsynced to EBS, which AWS replicates within an AZ. DLM takes snapshots hourly (48 kept) and daily (14 kept). The worst case is losing the AZ and up to an hour of events.
- **The real gap is uptime, not storage.**
  - YoLink offers personal accounts no history API (activity logs need a business CSID). Anything the subscriber misses while it is down is gone, except the current state, which the next poll picks up.
  - CloudWatch auto-recovers a failed host and reboots a wedged OS, and emails you when it does. Nothing alerts on the YoLink service itself stopping; its heartbeat is only visible in Datasette.
- **CPU credits are set to `standard`.** The T4g default, `unlimited`, bills surplus credits; `standard` throttles instead.
- **Dashboard needs a laptop.** SSM port-forwarding needs the AWS CLI, so there is no phone access. If you want it later, the options are a VPN or a public, authenticated endpoint.
