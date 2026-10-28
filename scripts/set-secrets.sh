#!/bin/bash
# Prompt for each secret and store it in SSM Parameter Store. Values are read
# without echo and never pass through Terraform state or shell history.
# Usage: REGION=us-west-2 NAME=log-cabin scripts/set-secrets.sh
set -euo pipefail
REGION=${REGION:-us-west-2}
NAME=${NAME:-log-cabin}

for p in yolink/uaid yolink/secret; do
  read -rsp "/$NAME/$p (blank to skip): " value; echo
  [ -z "$value" ] && continue
  aws ssm put-parameter --region "$REGION" --name "/$NAME/$p" --type SecureString \
    --value "$value" --overwrite >/dev/null
  echo "  set"
done
