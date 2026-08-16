#!/bin/bash
set -e

# Wait for database if configured
DB_HOST=${POSTGRES_SERVER}
if [ -n "$DB_HOST" ]; then
  DB_PORT=${POSTGRES_PORT:-5432}
  echo "Waiting for database at $DB_HOST:$DB_PORT..."
  until pg_isready -h $DB_HOST -p $DB_PORT -U ${POSTGRES_USER:-postgres}; do
    echo "Database at $DB_HOST:$DB_PORT is not ready yet..."
    sleep 2
  done
else
  echo "POSTGRES_SERVER not set, skipping database wait loop."
fi

# Run migrations
if [ -n "$POSTGRES_SERVER" ]; then
  echo "Attempting to run database migrations..."
  alembic upgrade head
  echo "Migrations completed successfully."
else
  echo "Skipping migrations (no database configured)."
fi

# Start app
if [ "$PROCESS_TYPE" = "celery_worker" ]; then
  echo "Starting Celery worker..."
  exec celery -A app.core.celery_app worker --loglevel=info
elif [ "$RUN_MODE" = "production" ]; then
  port=${PORT:-8000}
  echo "Starting Gunicorn in production mode on port $port..."
  exec gunicorn -c app/core/gunicorn_conf.py main:app
else
  port=${PORT:-8000}
  echo "Starting Uvicorn in development mode on port $port..."
  exec uvicorn main:app --host 0.0.0.0 --port $port --reload
fi
