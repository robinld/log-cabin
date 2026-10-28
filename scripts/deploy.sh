#!/bin/bash
# Tell the box to download the latest GitHub release and reinstall it, over
# SSM. Publish a release first; local edits and unreleased commits on main are
# not deployed. No SSH, no open ports.
set -euo pipefail
cd "$(dirname "$0")/../infra"

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
