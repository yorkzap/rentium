# rentium/properties/management/commands/backfill_areas.py
#
# Phase A of the area consolidation (standardize on Area, deprecate
# PropertyArea):
#
#   1. SEED: groups and standalone complete units created BEFORE the
#      seed-on-create signals existed get their default Area set. (This is
#      why your smoke-test inspection had zero shared sections — the
#      McKenzie group predates the signal and had no Area rows to match.)
#
#   2. CONVERT: every legacy PropertyArea row gets an equivalent Area row:
#        - on a COMPLETE_UNIT           -> Area(property=unit,  COMMON)
#        - on a grouped ROOM, shared    -> Area(group=room.group, COMMON)
#        - on a grouped ROOM, private   -> Area(group=room.group, EXCLUSIVE,
#                                               exclusive_to=room)
#        - on a groupless ROOM          -> Area(property=room, COMMON)
#      count > 1 fans out into numbered rows ("Bathroom", "Bathroom 2"...).
#      PropertyArea.description has no Area counterpart and is dropped —
#      it's display prose, not behavior.
#
# Idempotent by (target, name) — safe to re-run; already-converted rows are
# skipped. PropertyArea rows are NOT deleted here (the property UI still
# reads them until Phase B swaps the serializers to Area); this command just
# makes Area the complete, authoritative set.
#
# Package markers needed if properties/management/ doesn't exist yet:
#   rentium/properties/management/__init__.py
#   rentium/properties/management/commands/__init__.py
#
# Usage: python manage.py backfill_areas [--dry-run]

from django.core.management.base import BaseCommand
from django.db import transaction

from rentium.properties.areas import Area, seed_default_areas
from rentium.properties.models import Property, PropertyArea, PropertyGroup


class Command(BaseCommand):
    help = (
        "Seed default Areas for pre-existing groups/units and convert legacy "
        "PropertyArea rows into Area rows. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        dry = options["dry_run"]
        seeded_groups = seeded_units = converted = skipped = 0

        # ---- 1. Seed defaults where the creation signal never fired ------
        for group in PropertyGroup.objects.filter(areas__isnull=True).distinct():
            if dry:
                self.stdout.write(f"[dry] would seed defaults for group {group.name}")
            else:
                created = seed_default_areas(group=group)
                self.stdout.write(
                    f"Seeded {len(created)} default areas for group '{group.name}'."
                )
            seeded_groups += 1

        standalone_units = Property.objects.filter(
            property_category=Property.PropertyCategory.COMPLETE_UNIT,
            areas__isnull=True,
        ).distinct()
        for unit in standalone_units:
            if dry:
                self.stdout.write(f"[dry] would seed defaults for unit {unit.name}")
            else:
                created = seed_default_areas(property=unit)
                self.stdout.write(
                    f"Seeded {len(created)} default areas for unit '{unit.name}'."
                )
            seeded_units += 1

        # ---- 2. Convert legacy PropertyArea rows --------------------------
        legacy_rows = PropertyArea.objects.select_related(
            "property__group"
        ).prefetch_related("shared_by")
        for pa in legacy_rows:
            prop = pa.property
            base_name = pa.get_area_type_display()

            # Resolve target + kind per the mapping above.
            if prop.property_category == Property.PropertyCategory.COMPLETE_UNIT:
                target = {"property": prop}
                kind, exclusive_to = Area.Kind.COMMON, None
            elif prop.group_id:
                shared = [p for p in pa.shared_by.all() if p.pk != prop.pk]
                if shared:
                    target = {"group": prop.group}
                    kind, exclusive_to = Area.Kind.COMMON, None
                else:
                    target = {"group": prop.group}
                    kind, exclusive_to = Area.Kind.EXCLUSIVE, prop
            else:
                target = {"property": prop}
                kind, exclusive_to = Area.Kind.COMMON, None

            for n in range(pa.count or 1):
                name = base_name if n == 0 else f"{base_name} {n + 1}"
                if dry:
                    exists = Area.objects.filter(**target, name=name).exists()
                    verb = "skip (exists)" if exists else "create"
                    self.stdout.write(
                        f"[dry] {verb}: Area(name={name!r}, kind={kind}, "
                        f"target={list(target.values())[0]}, "
                        f"exclusive_to={exclusive_to})"
                    )
                    skipped += int(exists)
                    converted += int(not exists)
                    continue
                _, was_created = Area.objects.get_or_create(
                    **target,
                    name=name,
                    defaults={"kind": kind, "exclusive_to": exclusive_to},
                )
                converted += int(was_created)
                skipped += int(not was_created)

        summary = (
            f"Groups seeded: {seeded_groups} | Units seeded: {seeded_units} | "
            f"Legacy rows converted: {converted} | Already present: {skipped}"
        )
        if dry:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING(f"DRY RUN — {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
