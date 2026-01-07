#!/bin/sh
set -e

echo "[entrypoint] ROLE=$ROLE"

if [ "$ROLE" = "worker" ]; then
  echo "[entrypoint] starting worker"
  exec python app/worker.py
else
  echo "[entrypoint] starting api server"
  exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
fi
