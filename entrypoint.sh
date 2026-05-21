#!/bin/sh
set -e

if [ -n "$DATABASE_URL" ]; then
  echo "Waiting for database to be ready..."
  sleep 5

  if [ "${RUN_MIGRATIONS:-true}" = "true" ] && [ -f "/app/alembic.ini" ]; then
    echo "Running Alembic migrations..."
    alembic upgrade head
  elif [ "${RUN_MIGRATIONS:-true}" = "true" ] && [ -n "${MIGRATION_SQL_PATH:-}" ] && [ -f "$MIGRATION_SQL_PATH" ]; then
    echo "Running migrations from $MIGRATION_SQL_PATH..."
    psql "$PSQL_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$MIGRATION_SQL_PATH"
  else
    echo "Skipping migrations."
  fi

  if [ "${RUN_SEED:-true}" = "true" ]; then
    echo "Seeding data..."
    python -m app.seed
  else
    echo "Skipping seed."
  fi
else
  echo "DATABASE_URL is not set; skipping migrations and seed."
fi

if [ "${ENV:-production}" = "development" ]; then
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
else
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
fi
