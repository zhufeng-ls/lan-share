#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec ./manage.sh exec lan-share python -m app.admin_password
