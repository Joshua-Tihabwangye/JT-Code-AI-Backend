#!/usr/bin/env bash
# Run Django tests with coverage

set -e

echo "🧪 Running tests with coverage..."

poetry run pytest \
    --cov=apps \
    --cov=config \
    --cov-report=term-missing \
    --cov-report=html:htmlcov \
    --cov-report=xml:coverage.xml \
    "$@"

echo "✅ Tests complete! Coverage report available in htmlcov/index.html"