#!/usr/bin/env bash
set -Eeuo pipefail
export REPO_URL=${REPO_URL:-https://github.com/chenyme/grok2api.git}
export APP_DIR=${APP_DIR:-/opt/grok2api-official}
exec bash "$(dirname "$0")/install.sh"
