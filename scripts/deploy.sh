#!/bin/bash
# Tell the box to download the latest GitHub release and reinstall it, over
# SSM. Publish a release first; local edits and unreleased commits on main are
# not deployed. No SSH, no open ports.
#
# Runs as the deploy user (infra/main.tf), whose access key is in the macOS
# Keychain, read through the AWS CLI profile log-cabin-deploy:
#
#   [profile log-cabin-deploy]
#   region = us-west-2
#   credential_process = /usr/bin/security find-generic-password -s log-cabin-deploy -a aws -w
#
# AWS_PROFILE overrides it. scripts/status.sh shows the services afterwards.
set -euo pipefail
cd "$(dirname "$0")/../infra"
export AWS_PROFILE="${AWS_PROFILE:-log-cabin-deploy}"

INSTANCE=$(terraform output -raw instance_id)
REGION=$(terraform output -raw region)
DOCUMENT=$(terraform output -raw "${DOCUMENT_OUTPUT:-deploy_document}")

CMD_ID=$(aws ssm send-command --region "$REGION" \
  --instance-ids "$INSTANCE" \
  --document-name "$DOCUMENT" \
  --comment "$DOCUMENT" \
  --query Command.CommandId --output text)

echo "waiting for $CMD_ID ..."
aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE" || true
aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$INSTANCE" \
  --query '{status:Status,out:StandardOutputContent,err:StandardErrorContent}' --output yaml
