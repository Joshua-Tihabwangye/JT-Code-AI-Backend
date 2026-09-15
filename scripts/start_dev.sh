#!/usr/bin/env bash
# Start all development services

set -e

echo "🚀 Starting development services..."

# Check if docker-compose is available
if command -v docker-compose >/dev/null 2>&1; then
    echo "🐳 Starting Docker services (PostgreSQL, Redis, Kafka)..."
    docker-compose up -d postgres redis kafka zookeeper
    
    echo "⏳ Waiting for services to be ready..."
    sleep 5
else
    echo "⚠️  Docker Compose not found. Make sure PostgreSQL, Redis, and Kafka are running manually."
fi

# Run migrations
echo "🗄️  Running migrations..."
poetry run python manage.py migrate

echo "✅ Services ready! Start the Django server with: poetry run python manage.py runserver"
echo "   Start Celery worker with: poetry run celery -A config worker -l info"
echo "   Start Celery beat with: poetry run celery -A config beat -l info"