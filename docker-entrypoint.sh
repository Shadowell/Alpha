#!/bin/sh
# Prepare writable mounts, then drop root privileges before running the CMD.
#
# The image starts as root only so it can repair ownership of Docker named
# volumes (created root-owned) and bind mounts. The application itself always
# runs as APP_UID:APP_GID via gosu. If the container is already started with
# `--user`, ownership repair is skipped and the command runs as-is.
set -eu

APP_USER="${APP_USER:-alpha}"
APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"
DATA_DIR="${ALPHA_DATA_DIR:-/app/data}"
LOG_DIR="${ALPHA_LOG_DIR:-/app/logs}"
HF_CACHE_DIR="${HF_HOME:-/root/.cache/huggingface}"

warn() {
    echo "[docker-entrypoint] $*" >&2
}

if [ "$#" -eq 0 ]; then
    warn "no command supplied"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    # Unprivileged start (e.g. docker run --user): volumes must already be
    # writable by the caller; do not attempt chown.
    exec "$@"
fi

# Repair ownership of writable mounts. Failures are tolerated (macOS VirtioFS
# and some bind mounts reject chown) but are reported to aid troubleshooting.
for dir in "$DATA_DIR" "$LOG_DIR"; do
    mkdir -p "$dir" 2>/dev/null || warn "cannot create $dir"
    if ! chown -R "$APP_UID:$APP_GID" "$dir" 2>/dev/null; then
        warn "cannot chown $dir to $APP_UID:$APP_GID; continuing (read-only mount or host-fixed ownership)"
    fi
done

# HuggingFace cache mount (compose named volume). Best effort: a failure only
# means model weights may be re-downloaded instead of persisted.
if [ -d "$HF_CACHE_DIR" ]; then
    chown -R "$APP_UID:$APP_GID" "$HF_CACHE_DIR" 2>/dev/null \
        || warn "cannot chown $HF_CACHE_DIR; HuggingFace cache may not be writable"
fi

if command -v gosu >/dev/null 2>&1; then
    exec gosu "$APP_UID:$APP_GID" "$@"
fi

# Fallback for images built without gosu (not expected for this Dockerfile).
if command -v setpriv >/dev/null 2>&1; then
    exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --init-groups "$@"
fi

warn "neither gosu nor setpriv is available; refusing to run as root"
exit 1
