#!/bin/bash
# Prompt for each secret and store it in SSM Parameter Store. Values are read
# without echo and never pass through Terraform state or shell history.
# Usage: REGION=us-west-2 NAME=log-cabin scripts/set-secrets.sh
set -euo pipefail
REGION=${REGION:-us-west-2}
NAME=${NAME:-log-cabin}

for p in yolink/uaid yolink/secret evl/host evl/password protect/api_key lutron/host lennox/host; do
  read -rsp "/$NAME/$p (blank to skip): " value; echo
  [ -z "$value" ] && continue
  aws ssm put-parameter --region "$REGION" --name "/$NAME/$p" --type SecureString \
    --value "$value" --overwrite >/dev/null
  echo "  set"
done

# The WireGuard client config is uploaded from its file. Its AllowedIPs is the
# home addresses the box needs, or 0.0.0.0/0 to send all its IPv4 through the
# house, which needs an IPv6 Endpoint. The box ignores ::/0.
read -rp "/$NAME/wireguard/conf: path to the client .conf (blank to skip): " conf
if [ -n "$conf" ]; then
  if grep -Eq '^[[:space:]]*AllowedIPs[[:space:]]*=.*0\.0\.0\.0/0' "$conf" &&
     ! grep -Eq '^[[:space:]]*Endpoint[[:space:]]*=[[:space:]]*\[' "$conf"; then
    echo "  refused: AllowedIPs sends all IPv4 through the tunnel, so Endpoint must be IPv6, e.g. [2001:db8::1]:51820" >&2
    exit 1
  fi
  aws ssm put-parameter --region "$REGION" --name "/$NAME/wireguard/conf" --type SecureString \
    --value "file://$conf" --overwrite >/dev/null
  echo "  set"
fi
