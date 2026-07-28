"""
Erase durable memories mentioning a person.

Why this exists as a command rather than a signal: deleting a tenant's account
cascades through foreign keys, but a landlord memory is free text — "my
plumber is Bob at 250-555-0100" holds Bob's number with no relation to
anything. There is currently no `user.deleted` domain event to hang a handler
on, so an erasure request is served operationally, by a human running this.

That is a deliberate stopgap and it is documented as one. The right fix is a
deletion event the whole platform can subscribe to; this command is what makes
an erasure request answerable in the meantime.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from rentium.rama.models import RamaMemory


class Command(BaseCommand):
    help = "Erase RAMA memories mentioning a person (privacy erasure requests)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--match",
            required=True,
            help="Email, phone, or name to search memory bodies for.",
        )
        parser.add_argument(
            "--landlord",
            default="",
            help="Limit to one landlord id (default: every landlord).",
        )
        parser.add_argument(
            "--delete",
            action="store_true",
            help=(
                "Hard-delete instead of marking FORGOTTEN. Use for a genuine "
                "erasure request; the text is removed, not just hidden."
            ),
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Apply the change. Without it this is a dry run.",
        )

    def handle(self, *args, **options):
        needle = (options["match"] or "").strip()
        if len(needle) < 3:
            raise CommandError("--match needs at least 3 characters.")

        rows = RamaMemory.objects.filter(body__icontains=needle)
        if options["landlord"]:
            rows = rows.filter(landlord_id=options["landlord"])

        found = list(rows.select_related("landlord"))
        if not found:
            self.stdout.write(f"No memories mention {needle!r}.")
            return

        for row in found:
            self.stdout.write(
                f"  [{row.status}] landlord={row.landlord_id} {row.key}: {row.body[:100]}",
            )

        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDry run — {len(found)} memory/memories match. "
                    "Re-run with --yes to apply.",
                ),
            )
            return

        if options["delete"]:
            count, _ = rows.delete()
            self.stdout.write(self.style.SUCCESS(f"Erased {count} memory row(s)."))
        else:
            count = rows.update(status=RamaMemory.Status.FORGOTTEN)
            self.stdout.write(
                self.style.SUCCESS(f"Marked {count} memory row(s) FORGOTTEN."),
            )
