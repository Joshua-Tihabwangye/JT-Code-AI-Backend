"""Create or verify the private Supabase Storage bucket used by assets."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.assets.supabase_storage import SupabaseStorageError, ensure_private_bucket


class Command(BaseCommand):
    help = "Create or verify the private Supabase Storage asset bucket and its limits."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            ensure_private_bucket()
        except SupabaseStorageError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified private Supabase Storage bucket {settings.SUPABASE_STORAGE_BUCKET!r}."
            )
        )
