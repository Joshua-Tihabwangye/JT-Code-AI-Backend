from django.core.management.base import BaseCommand, CommandError

from apps.events.consumers import replay_dead_letter


class Command(BaseCommand):
    help = "Replay one reviewed dead-letter event through the transactional outbox."

    def add_arguments(self, parser) -> None:
        parser.add_argument("dead_letter_id")
        parser.add_argument("--actor", default="")
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required acknowledgement that the source contract/handler was fixed.",
        )

    def handle(self, *args, **options) -> None:
        if not options["confirm"]:
            raise CommandError("Replay requires --confirm after reviewing the source failure.")
        try:
            dead_letter = replay_dead_letter(
                dead_letter_id=options["dead_letter_id"],
                actor=options["actor"],
            )
        except (ValueError, TypeError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Replayed dead letter {dead_letter.id}"))
