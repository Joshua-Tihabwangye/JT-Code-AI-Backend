"""Add an existing local login to another user's organization (for trying sharing locally).

    python manage.py join_local_organization viewer@example.com --owner you@example.com
    JOIN_PASSWORD=… python manage.py join_local_organization viewer@example.com \
        --owner you@example.com --create

The owner's login must exist (see ``create_local_account``). With ``--create`` the member's login
is made too, with the password from the ``JOIN_PASSWORD`` environment variable, and the owner's
organization is their only one (so the app opens it). The member gets no admin role, so they see
what a colleague would: shared dashboards as a viewer. Development only: the command refuses to
run unless DEBUG is on.
"""

from __future__ import annotations

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.identity.models import Organization, User, UserOrganization
from apps.identity.supabase_admin import SupabaseAdminError, create_auth_user


class Command(BaseCommand):
    help = "Add a local login to another user's organization (DEBUG only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("email")
        parser.add_argument("--owner", required=True, help="Email of the organization's owner.")
        parser.add_argument("--org", default="", help="Organization name (default: the owner's first).")
        parser.add_argument(
            "--create", action="store_true", help="Create the member's login (JOIN_PASSWORD)."
        )

    def handle(self, *args, **options) -> None:
        if not settings.DEBUG:
            raise CommandError("join_local_organization only runs with DEBUG on (local development).")
        email = options["email"].strip().lower()
        if options["create"]:
            password = os.environ.get("JOIN_PASSWORD", "")
            if len(password) < 8:
                raise CommandError("Set JOIN_PASSWORD (8+ characters) to create the login.")
            try:
                supabase_user_id, _ = create_auth_user(email, password)
            except (SupabaseAdminError, ValueError) as exc:
                raise CommandError(str(exc)) from exc
            User.objects.get_or_create(supabase_user_id=supabase_user_id, defaults={"email": email})
        member = User.objects.filter(email__iexact=email).first()
        owner = User.objects.filter(email__iexact=options["owner"].strip()).first()
        if member is None or owner is None:
            raise CommandError("Both logins must exist; create them with create_local_account first.")
        organizations = Organization.objects.filter(owner=owner).order_by("created_at")
        if options["org"]:
            organizations = organizations.filter(name=options["org"])
        organization = organizations.first()
        if organization is None:
            raise CommandError(f"{owner.email} owns no organization with that name.")
        _, created = UserOrganization.objects.get_or_create(user=member, organization=organization)
        state = "joined" if created else "was already in"
        self.stdout.write(self.style.SUCCESS(f"{member.email} {state} '{organization.name}'."))
