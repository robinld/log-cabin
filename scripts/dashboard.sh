#!/bin/bash
# Open Datasette at http://localhost:8001 through an SSM port-forward.
# Needs the AWS CLI and the Session Manager plugin:
# https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
set -euo pipefail
cd "$(dirname "$0")/../infra"
aws ssm start-session --region "$(terraform output -raw region)" \
  --target "$(terraform output -raw instance_id)" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8001"],"localPortNumber":["8001"]}'
