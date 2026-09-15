#!/usr/bin/env bash
# Development setup script for JT-Code API

set -e

echo "🚀 Setting up JT-Code API development environment..."

# Check for required tools
command -v python3 >/dev/null 2>&1 || { echo "❌ Python 3 is required but not installed."; exit 1; }
command -v poetry >/dev/null 2>&1 || { echo "❌ Poetry is required but not installed. Install from https://python-poetry.org/"; exit 1; }

# Install dependencies
echo "📦 Installing Python dependencies..."
poetry install --with dev

# Setup environment file
if [ ! -f .env ]; then
    echo "📝 Creating .env file from .env.example..."
    cp .env.example .env
    echo "⚠️  Please update .env with your local configuration"
fi

# Run migrations
echo "🗄️  Running database migrations..."
poetry run python manage.py migrate

# Create superuser if it doesn't exist
echo "👤 Creating superuser (if needed)..."
poetry run python manage.py shell -c "
from apps.identity.models import User
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@localhost', 'admin')
    print('Superuser created: admin/admin')
else:
    print('Superuser already exists')
"

# Collect static files
echo "📁 Collecting static files..."
poetry run python manage.py collectstatic --noinput

echo "✅ Development setup complete!"
echo ""
echo "To start the development server:"
echo "  poetry run python manage.py runserver"
echo ""
echo "To start Celery worker:"
echo "  poetry run celery -A config worker -l info"
echo ""
echo "To start Celery beat:"
echo "  poetry run celery -A config beat -l info"
echo ""
echo "To run tests:"
echo "  poetry run pytest"