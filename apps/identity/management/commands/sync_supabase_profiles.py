"""Fill empty Django profile fields from Supabase Auth metadata (repairs e-mail-only rows).

    python manage.py sync_supabase_profiles --dry-run
    python manage.py sync_supabase_profiles

Pages through the Supabase Admin API (``SUPABASE_SECRET_KEY``) and, for every Supabase user that has a
Django row, copies ``user_metadata`` into the row's *empty* fields only (the same rule as sign-in
provisioning). E-mail is refreshed from Supabase. Nothing is ever blanked or overwritten, and Django
rows without a Supabase user are reported, not deleted.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.identity.models import User
from apps.identity.provisioning import profile_values_from_metadata
from apps.identity.supabase_admin import SupabaseAdminError, iter_auth_users


class Command(BaseCommand):
    help = "Fill empty profile fields of local users from their Supabase Auth metadata."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        try:
            remote = {str(user["id"]): user for user in iter_auth_users() if user.get("id")}
        except SupabaseAdminError as exc:
            raise CommandError(str(exc)) from exc

        updated = unchanged = 0
        for user in User.objects.filter(supabase_user_id__in=list(remote)).order_by("created_at"):
            record = remote[user.supabase_user_id]
            values = profile_values_from_metadata(record.get("user_metadata"))
            if values.get("avatar_url") and user.avatar_asset_id:
                values.pop("avatar_url")
            changes = {field: value for field, value in values.items() if not getattr(user, field)}
            email = record.get("email") if isinstance(record.get("email"), str) else ""
            if email and email != user.email:
                changes["email"] = email
            if not changes:
                unchanged += 1
                continue
            updated += 1
            self.stdout.write(f"{user.email or user.supabase_user_id}: {', '.join(sorted(changes))}")
            if not dry_run:
                with transaction.atomic():
                    User.objects.filter(pk=user.pk).update(**changes, updated_at=timezone.now())

        local_only = User.objects.exclude(supabase_user_id__in=list(remote)).count()
        verb = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {updated} user(s); {unchanged} already complete; "
                f"{local_only} local user(s) have no Supabase account in this project."
            )
        )
