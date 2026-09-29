#!/usr/bin/env bash
# Prepare the Django backend for local development.

set -e

echo "🚀 Preparing JT-Code backend development..."
echo "Ensure PostgreSQL/pgvector, Redis, and Kafka are available through your managed or local services."

# Run migrations
echo "🗄️  Running migrations..."
poetry run python manage.py migrate

echo "✅ Services ready! Start the Django server with: poetry run python manage.py runserver"
echo "   Start Celery worker with: poetry run celery -A config worker -l info"
echo "   Start Celery beat with: poetry run celery -A config beat -l info"