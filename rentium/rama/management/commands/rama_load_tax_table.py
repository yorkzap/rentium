"""
Load a dated tax rate table.

Deliberately a human-run command rather than anything automatic. These numbers
change annually, they are the basis of advice a landlord may act on, and
`tax.marginal_rate_estimate` refuses to fall back to a previous year — so the
Treasurer going quiet about tax in January is the intended, visible signal that
someone needs to load the new year.

    python manage.py rama_load_tax_table \\
        --jurisdiction CA-BC --year 2027 --kind PERSONAL_INCOME_BRACKETS \\
        --file brackets.json --source-url https://...
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from rentium.rama.models import TaxRateTable


class Command(BaseCommand):
    help = "Load or replace a dated tax rate table used for planning estimates."

    def add_arguments(self, parser):
        parser.add_argument("--jurisdiction", required=True, help='e.g. "CA-FED", "CA-BC"')
        parser.add_argument("--year", required=True, type=int)
        parser.add_argument(
            "--kind",
            required=True,
            choices=[c for c, _ in TaxRateTable.Kind.choices],
        )
        parser.add_argument("--file", required=True, help="JSON payload path.")
        parser.add_argument("--source-url", default="", help="Where these came from.")
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Overwrite an existing table for this jurisdiction/year/kind.",
        )

    def handle(self, *args, **options):
        path = Path(options["file"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")
        try:
            payload = json.loads(path.read_text())
        except ValueError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc

        if options["kind"] == TaxRateTable.Kind.PERSONAL_INCOME_BRACKETS:
            self._validate_brackets(payload)

        existing = TaxRateTable.objects.filter(
            jurisdiction=options["jurisdiction"],
            tax_year=options["year"],
            kind=options["kind"],
        ).first()
        if existing and not options["replace"]:
            raise CommandError(
                f"{options['jurisdiction']} {options['year']} "
                f"{options['kind']} is already loaded. Pass --replace to overwrite."
            )

        TaxRateTable.objects.update_or_create(
            jurisdiction=options["jurisdiction"],
            tax_year=options["year"],
            kind=options["kind"],
            defaults={
                "payload": payload,
                "source_url": options["source_url"],
                "source_fetched_at": timezone.now(),
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {options['jurisdiction']} {options['year']} {options['kind']}."
            )
        )

    def _validate_brackets(self, payload):
        """A malformed bracket list would produce a confidently wrong rate."""
        brackets = payload if isinstance(payload, list) else payload.get("brackets")
        if not isinstance(brackets, list) or not brackets:
            raise CommandError(
                'Brackets must be a non-empty list of {"upto": <number|null>, '
                '"rate": <0-1>} objects, ordered lowest to highest.'
            )
        last_upto = None
        for i, bracket in enumerate(brackets, start=1):
            if "rate" not in bracket:
                raise CommandError(f"Bracket {i} has no rate.")
            rate = float(bracket["rate"])
            if not 0 <= rate <= 1:
                raise CommandError(
                    f"Bracket {i} rate {rate} must be a fraction (0.15), not a "
                    f"percentage (15)."
                )
            upto = bracket.get("upto")
            if upto is not None and last_upto is not None and upto <= last_upto:
                raise CommandError(f"Bracket {i} is not in ascending order.")
            last_upto = upto
        if brackets[-1].get("upto") is not None:
            raise CommandError('The final bracket must have "upto": null.')
