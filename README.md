# log cabin

A place for your logs. 
Aggregate logs across various IoT systems and query them.


## Code layout

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
