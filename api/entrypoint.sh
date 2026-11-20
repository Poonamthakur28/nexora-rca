#!/bin/sh
# Apply database migrations, then start the API (or the worker when given "worker").
set -e
for i in $(seq 1 30); do
  if alembic upgrade head; then break; fi
  echo "waiting for database ($i/30)..."; sleep 2
done
if [ "$1" = "worker" ]; then
  exec python -m app.worker
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
