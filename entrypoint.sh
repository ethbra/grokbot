#!/bin/sh
# Starts as root only to give the non-root user a writable folder inside the
# /data mount (the host owns the mount point itself), then drops privileges.
set -eu
if [ "$(id -u)" = 0 ]; then
  mkdir -p /data/state
  chown 1000:1000 /data/state
  exec setpriv --reuid=1000 --regid=1000 --init-groups -- "$@"
fi
exec "$@"
