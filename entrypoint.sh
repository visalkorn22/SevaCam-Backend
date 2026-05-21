#!/bin/sh
set -e

if [ -n "$DATABASE_URL" ]; then
  PSQL_DATABASE_URL=$(echo "$DATABASE_URL" | sed 's/^postgresql+psycopg2:/postgresql:/')

  echo "Waiting for database to be ready..."
  until pg_isready -d "$PSQL_DATABASE_URL" >/dev/null 2>&1; do
    sleep 1
  done

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

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
