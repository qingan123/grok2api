#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=${APP_DIR:-/opt/grok2api}; port=${GROK2API_PORT:-8000}
fail(){ echo "ERROR: $*" >&2; exit 1; }; [[ $EUID -eq 0 ]] || fail '请使用 sudo/root'; command -v docker >/dev/null || fail '请先安装 Docker'; docker compose version >/dev/null || fail '需要 Docker Compose v2'
if [[ -t 0 ]]; then read -r -p "端口 [$port]: " v; port=${v:-$port}; fi
[[ $port =~ ^[0-9]+$ && $port -ge 1 && $port -le 65535 ]] || fail '端口无效'
mkdir -p "$APP_DIR"; if [[ -d "$APP_DIR/.git" ]]; then git -C "$APP_DIR" fetch --depth 1 origin main; git -C "$APP_DIR" reset --hard origin/main; else git clone --depth 1 https://github.com/qingan123/grok2api.git "$APP_DIR"; fi
cd "$APP_DIR"; mkdir -p data logs; cp -n config.example.yaml config.yaml 2>/dev/null || true
sed -i -E "0,/127.0.0.1:8000/s//0.0.0.0:$port/" config.yaml
docker compose up -d --pull always
curl -fsS --max-time 30 "http://127.0.0.1:$port/healthz" >/dev/null
printf '部署完成: http://服务器IP:%s/\n' "$port"
