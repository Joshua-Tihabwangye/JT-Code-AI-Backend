"""Run the versioned RAG retrieval benchmark against the configured providers.

Example::

    python manage.py rag_evaluate --organization <uuid> --user <uuid>

Exit status is non-zero when mean recall@k or MRR is below
``RAG_EVAL_MIN_RECALL`` / ``RAG_EVAL_MIN_MRR``. Documents are ingested into a
temporary collection in the given organization and deleted afterwards.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.identity.models import Organization, User
from apps.knowledge.evaluation import load_cases, run_benchmark


class Command(BaseCommand):
    help = "Score hybrid retrieval (recall@k, MRR) on the versioned RAG evaluation dataset."

    def add_arguments(self, parser):
        parser.add_argument(
            "--organization", required=True, help="Organization id that owns the temp collection."
        )
        parser.add_argument("--user", required=True, help="Member user id used as the retrieval principal.")
        parser.add_argument(
            "--dataset", default=None, help="Path to a JSON dataset (defaults to the bundled one)."
        )
        parser.add_argument("--top-k", type=int, default=5)

    def handle(self, *args, **options):
        organization = Organization.objects.filter(id=options["organization"]).first()
        user = User.objects.filter(id=options["user"]).first()
        if organization is None or user is None:
            raise CommandError("Unknown organization or user.")
        if not user.organizations.filter(id=organization.id).exists():
            raise CommandError("The user must be a member of the organization.")
        report = run_benchmark(
            load_cases(options["dataset"]), organization=organization, user=user, top_k=options["top_k"]
        )
        self.stdout.write(json.dumps(report.as_dict(), indent=2))
        if not report.passed:
            raise CommandError("RAG retrieval quality is below the configured thresholds.")
