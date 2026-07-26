"""Seed default areas wherever the creation signal never fired.

Usage: python manage.py backfill_areas [--dry-run]

This command used to also convert legacy PropertyArea rows into a separate
`Area` model. That conversion is gone: the two models were merged into
PropertyArea (which held the real data and the legally load-bearing
shared_with_landlord/shared_by fields), so there is nothing left to convert —
only gaps to fill.

The gaps are real. The signals that seed areas on group/unit creation live in
rentium/ledger/signals.py, which was never imported by LedgerConfig.ready(),
so every group and complete unit created before that fix has no areas at all.
That is why a smoke-test inspection came back with zero shared sections. This
backfills them. Idempotent by (parent, name), so it is safe to re-run.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from rentium.properties.areas import seed_default_areas
from rentium.properties.models import Property
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyUnit


class Command(BaseCommand):
    help = "Seed default areas for groups, units and standalone listings. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry = options["dry_run"]
        seeded = {"unit": 0, "group": 0, "listing": 0}

        def _seed(label, kwargs, name):
            if dry:
                self.stdout.write(f"[dry] would seed defaults for {label} '{name}'")
            else:
                created = seed_default_areas(**kwargs)
                self.stdout.write(
                    f"Seeded {len(created)} default areas for {label} '{name}'."
                )
            seeded[label] += 1

        for unit in PropertyUnit.objects.filter(areas__isnull=True).distinct():
            _seed("unit", {"unit": unit}, unit.name)

        for group in PropertyGroup.objects.filter(areas__isnull=True).distinct():
            _seed("group", {"group": group}, group.name)

        # Standalone complete units: a listing with no unit and no group has
        # nowhere else for its areas to hang.
        standalone = Property.objects.filter(
            property_category=Property.PropertyCategory.COMPLETE_UNIT,
            unit__isnull=True,
            group__isnull=True,
            primary_area_associations__isnull=True,
        ).distinct()
        for listing in standalone:
            _seed("listing", {"property": listing}, listing.name)

        summary = (
            f"Units seeded: {seeded['unit']} | Groups seeded: {seeded['group']} | "
            f"Standalone listings seeded: {seeded['listing']}"
        )
        if dry:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING(f"DRY RUN — {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
