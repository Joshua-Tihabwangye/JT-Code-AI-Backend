#!/usr/bin/env bash
# Run linting and type checking

set -e

echo "🔍 Running linting..."
poetry run ruff check .

echo "🔧 Running type checking..."
poetry run mypy .

echo "✅ All checks passed!"