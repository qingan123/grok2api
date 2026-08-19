#!/usr/bin/env bash
set -Eeuo pipefail
APP_DIR=${APP_DIR:-}; PORT=${GROK2API_PORT:-}; SERVICE=${GROK2API_SERVICE:-grok2api}
fail(){ echo "ERROR: $*" >&2; exit 1; }
find_instances(){
  local cid dir svc port
  for dir in /opt/* /root/*; do
    [[ -d "$dir/.git" && -f "$dir/docker-compose.yml" ]] || continue
    git -C "$dir" remote -v 2>/dev/null | grep -qi 'grok2api' || continue
    port=$(grep -oE '[0-9]+:8000' "$dir/docker-compose.yml" | head -1 | cut -d: -f1)
    printf '%s\t%s\tdocker\t%s\n' "${port:-unknown}" "$dir" "$SERVICE"
  done
  command -v docker >/dev/null 2>&1 || return 0
  while read -r cid; do
    dir=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid" 2>/dev/null || true)
    svc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$cid" 2>/dev/null || true)
    [[ "$svc" == "$SERVICE" && -n "$dir" ]] || continue
    while read -r port; do [[ "$port" =~ ^[0-9]+$ ]] && printf '%s\t%s\tdocker\t%s\n' "$port" "$dir" "$svc"; done < <(docker port "$cid" 2>/dev/null | sed -nE 's/.*:([0-9]+)$/\1/p')
  done < <(docker ps --format '{{.ID}}' 2>/dev/null)
}
if [[ -z "$APP_DIR" ]]; then
  [[ -t 0 ]] || fail '非交互模式请设置 APP_DIR 和 GROK2API_PORT'
  mapfile -t found < <(find_instances | awk -F '\t' '!seen[$1 FS $2 FS $4]++'); ((${#found[@]})) || fail '未检测到grok2api实例'
  echo '检测到实例（端口 目录 运行方式 服务）:'; printf '%s\n' "${found[@]}" | nl -v1 -w2 -s') '
  read -r -p '选择编号、端口或目录: ' choice
  if [[ "$choice" =~ ^[0-9]+$ && "$choice" -le ${#found[@]} ]]; then row=${found[$((choice-1))]}; else row=$(printf '%s\n' "${found[@]}" | awk -F '\t' -v p="$choice" '$1==p{print;exit}'); [[ -n "$row" ]] || { APP_DIR=$choice; row=""; }; fi
  if [[ -n "$row" ]]; then PORT=$(printf '%s' "$row" | cut -f1); APP_DIR=$(printf '%s' "$row" | cut -f2); SERVICE=$(printf '%s' "$row" | cut -f4); fi
fi
[[ -d "$APP_DIR/.git" ]] || fail "不是Git仓库: $APP_DIR"
cd "$APP_DIR"; [[ -z "$(git status --porcelain)" ]] || fail '工作树有未提交修改，停止更新。'
[[ -f config.yaml ]] && cp -a config.yaml "config.yaml.backup.$(date +%Y%m%d-%H%M%S)"
git fetch --depth 1 origin main; git reset --hard origin/main
docker compose pull; docker compose up -d --force-recreate --no-build "$SERVICE"
PORT=${PORT:-8000}; curl -fsS --max-time 30 "http://127.0.0.1:$PORT/healthz" >/dev/null
docker compose ps
