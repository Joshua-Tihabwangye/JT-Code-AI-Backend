"""Celery task registration for the operations app (autodiscovered)."""

from apps.operations.drills import drill_ping

__all__ = ["drill_ping"]
