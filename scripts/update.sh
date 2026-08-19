#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=${APP_DIR:-/opt/grok2api}; cd "$APP_DIR"
[[ -z "$(git status --porcelain)" ]] || { echo '工作树有未提交修改，已停止更新。' >&2; exit 1; }
cp -a config.yaml "config.yaml.backup.$(date +%Y%m%d-%H%M%S)"
git fetch --depth 1 origin main
git reset --hard origin/main
docker compose pull
docker compose up -d --force-recreate --no-build grok2api
docker compose ps
