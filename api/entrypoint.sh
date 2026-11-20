#!/bin/sh
# Apply database migrations, then start the API (or the worker when given "worker").
# Migrations are retried while the database starts up; if they never succeed, startup stops with a
# non-zero exit instead of running the application on an unmigrated schema.
set -eu

attempts="${MIGRATION_ATTEMPTS:-30}"
i=1
until alembic upgrade head; do
  if [ "$i" -ge "$attempts" ]; then
    echo "entrypoint: database migrations failed after $attempts attempt(s); not starting" >&2
    exit 1
  fi
  echo "entrypoint: migrations failed (attempt $i/$attempts); retrying in 2 s..." >&2
  i=$((i + 1))
  sleep 2
done

if [ "${1:-}" = "worker" ]; then
  exec python -m app.worker
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
