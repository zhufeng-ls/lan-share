#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$#" -ne 1 ]; then
  echo '用法：./backup.sh /绝对路径/备份文件.tar.gz' >&2
  exit 1
fi
case "$1" in /*) ;; *) echo '请使用绝对路径。' >&2; exit 1;; esac
if [ -e "$1" ] || [ -e "$1.partial" ]; then
  echo '目标文件已存在，拒绝覆盖。' >&2
  exit 1
fi
backup_parent=$(dirname -- "$1")
backup_filename=$(basename -- "$1")
test -d "$backup_parent"
container_id=$(./manage.sh ps -q lan-share)
test -n "$container_id"
was_running=$(docker inspect --format '{{.State.Running}}' "$container_id")
resume_service() {
  if [ "$was_running" = true ]; then
    ./manage.sh start >/dev/null
  fi
}
trap resume_service EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [ "$was_running" = true ]; then
  ./manage.sh stop
fi
docker run --rm --user 0:0 --network none \
  -v lan-share_lan-share-data:/data:ro \
  -v "$backup_parent:/backup" \
  lan-share:1.0.0 sh -c 'umask 077; tar -czf "$1" -C /data . && chown "$2:$3" "$1"' \
  sh "/backup/$backup_filename.partial" "$(id -u)" "$(id -g)"
mv -- "$1.partial" "$1"
echo "备份已保存到 $1"
