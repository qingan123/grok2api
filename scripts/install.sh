#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=${APP_DIR:-/opt/grok2api}
PORT=${GROK2API_PORT:-8000}
REPO_URL=${REPO_URL:-https://github.com/qingan123/grok2api.git}
fail(){ echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail '请使用 sudo/root'
command -v git >/dev/null || fail '请先安装 Git'
command -v docker >/dev/null || fail '请先安装 Docker'
docker compose version >/dev/null || fail '需要 Docker Compose v2'
if [[ -t 0 ]]; then read -r -p "端口 [$PORT]: " v; PORT=${v:-$PORT}; fi
[[ $PORT =~ ^[0-9]+$ && $PORT -ge 1 && $PORT -le 65535 ]] || fail '端口无效'
command -v ss >/dev/null && ss -ltn "sport = :$PORT" | grep -q LISTEN && fail "端口 $PORT 已被占用"
mkdir -p "$APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then git -C "$APP_DIR" fetch --depth 1 origin main; git -C "$APP_DIR" reset --hard origin/main; else [[ -z "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fail '目标目录非空'; git clone --depth 1 "$REPO_URL" "$APP_DIR"; fi
cd "$APP_DIR"
mkdir -p data logs
cp -n .env.example .env 2>/dev/null || true
if grep -q '^HOST_PORT=' .env; then sed -i "s/^HOST_PORT=.*/HOST_PORT=$PORT/" .env; else printf 'HOST_PORT=%s\n' "$PORT" >> .env; fi
if grep -q '^SERVER_HOST=' .env; then sed -i 's/^SERVER_HOST=.*/SERVER_HOST=0.0.0.0/' .env; else printf 'SERVER_HOST=0.0.0.0\n' >> .env; fi
if grep -q '^SERVER_PORT=' .env; then sed -i 's/^SERVER_PORT=.*/SERVER_PORT=8000/' .env; else printf 'SERVER_PORT=8000\n' >> .env; fi
chmod 600 .env
docker compose --env-file .env up -d --pull always
for _ in {1..30}; do curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null || { docker compose logs --tail=100; exit 1; }
ip="${PUBLIC_HOST:-$(curl -4fsS --max-time 5 https://api.ipify.org || true)}"
url=${ip:+http://$ip:$PORT/admin/login}; [[ -n "$url" ]] || url='公网IP探测失败，请检查安全组/UFW'
printf '部署完成。\n公网后台: %s\n本机后台: http://127.0.0.1:%s/admin/login\n端口: %s（绑定 0.0.0.0）\n目录: %s\n' "$url" "$PORT" "$PORT" "$APP_DIR"
