#!/usr/bin/env bash
set -euo pipefail

cd /opt/alpha

if [ ! -f .env ]; then
  echo "missing /opt/alpha/.env" >&2
  exit 1
fi

mkdir -p logs

echo "[deploy] docker compose build"
docker compose build

echo "[deploy] docker compose up -d"
docker compose up -d --remove-orphans

echo "[deploy] health check"
for attempt in $(seq 1 60); do
  if curl -fsS --max-time 3 http://127.0.0.1:18888/ >/dev/null 2>&1; then
    echo "[deploy] healthy after ${attempt} checks"
    docker compose ps
    exit 0
  fi
  sleep 2
done

echo "[deploy] health check failed" >&2
docker compose logs --tail=120 alpha >&2 || true
exit 1
