#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if docker compose version >/dev/null 2>&1; then
  exec docker compose -p lan-share -f compose.yaml "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -p lan-share -f compose.yaml "$@"
else
  echo '需要安装 Docker Engine 和 Docker Compose。' >&2
  exit 1
fi
