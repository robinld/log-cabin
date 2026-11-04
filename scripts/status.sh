#!/bin/bash
# The box's home-events services and their last 15 minutes of logs, over SSM,
# as the deploy user (see deploy.sh).
exec env DOCUMENT_OUTPUT=status_document "$(dirname "$0")/deploy.sh"
