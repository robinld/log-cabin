#!/bin/bash
# Push code changes: re-zip and upload the app (terraform apply), then tell
# the box to pull and reinstall it over SSM. No SSH, no open ports.
set -euo pipefail
cd "$(dirname "$0")/../infra"

terraform apply "$@"

INSTANCE=$(terraform output -raw instance_id)
REGION=$(terraform output -raw region)

CMD_ID=$(aws ssm send-command --region "$REGION" \
  --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript \
  --comment "home-events deploy" \
  --parameters 'commands=["/usr/local/bin/home-events-update"]' \
  --query Command.CommandId --output text)

echo "waiting for $CMD_ID ..."
aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE" || true
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE" \
  --query '{status:Status,out:StandardOutputContent,err:StandardErrorContent}' --output yaml
