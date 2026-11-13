"""Send one email through the configured SMTP server (``SMTP_*``), to check the mail setup.

python manage.py send_test_email you@example.com
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    help = "Send a test email through the configured SMTP server (SMTP_HOST, SMTP_USER, ...)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("recipient", help="Address that should receive the test email.")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.SMTP_HOST:
            raise CommandError(
                "SMTP_HOST is not set, so mail is only printed to the console. Fill in SMTP_* first."
            )
        if settings.EMAIL_HOST_USER and not settings.EMAIL_HOST_PASSWORD:
            raise CommandError("SMTP_PASSWORD is empty (for Gmail, use a Google App Password).")
        self.stdout.write(
            f"Sending through {settings.EMAIL_HOST}:{settings.EMAIL_PORT} "
            f"({'SSL' if settings.EMAIL_USE_SSL else 'STARTTLS' if settings.EMAIL_USE_TLS else 'plain'}) "
            f"as {settings.DEFAULT_FROM_EMAIL} ..."
        )
        try:
            send_mail(
                subject="JT-Code test email",
                message="This message confirms JT-Code can send email through your SMTP server.",
                from_email=None,
                recipient_list=[options["recipient"]],
            )
        except Exception as error:  # noqa: BLE001 - report the SMTP failure as a command error
            hint = (
                " For Gmail, SMTP_PASSWORD must be an App Password, not the account password."
                if "535" in str(error)
                else ""
            )
            raise CommandError(f"Sending failed: {error}.{hint}") from error
        self.stdout.write(self.style.SUCCESS(f"Sent. Check the inbox of {options['recipient']}."))
