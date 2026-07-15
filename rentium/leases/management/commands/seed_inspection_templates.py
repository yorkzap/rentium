# seed_inspection_templates.py
#
# Seeds the BC Condition Inspection Report (RTB-27) template — and registers
# the same catalogue as the GENERIC fallback so every province gets a usable
# report. Idempotent: existing (province, version) pairs are left alone, so
# it's safe in deploy scripts. Bump VERSION and re-run to ship a revised
# catalogue; old inspections keep pointing at the version that seeded them.
#
# If rentium/leases/management/ doesn't exist yet, create the package
# markers first:
#   rentium/leases/management/__init__.py
#   rentium/leases/management/commands/__init__.py
#
# Usage: python manage.py seed_inspection_templates

from django.core.management.base import BaseCommand
from django.db import transaction

from rentium.leases.inspections import InspectionTemplate, InspectionTemplateItem

VERSION = 1

# Sections J–V of RTB-27 (2024/11), transcribed from the form.
RTB27_ITEMS = [
    ("Entry", [
        "Walls and Trim", "Ceilings", "Closets",
        "Lighting Fixtures/Ceiling fan/Bulbs", "Windows/Coverings/Screens",
        "Electrical Outlets", "Floor carpets",
    ]),
    ("Kitchen", [
        "Ceiling", "Walls and trim", "Floor/Carpet", "Countertop",
        "Cabinets and Doors", "Stove/Stove Top", "Oven", "Exhaust Hood and Fan",
        "Taps, Sink and Stoppers", "Refrigerator", "Refrigerator — Crisper/Shelves",
        "Refrigerator — Freezer", "Refrigerator — Door/Exterior", "Closet(s)",
        "Dishwasher", "Lighting Fixtures/Bulbs", "Windows/Coverings/Screens",
        "Electrical Outlets",
    ]),
    ("Living Room", [
        "Ceiling", "Walls and Trim", "Floor/Carpet", "Air Conditioner/Cover",
        "Fireplace", "TV Cable/Adaptor", "Closet(s)",
        "Lighting Fixtures/Ceiling Fans/Bulbs", "Window/Coverings/Screens",
        "Electrical Outlets",
    ]),
    ("Dining Room", [
        "Walls and Trim", "Ceilings", "Floor/Carpets",
        "Lighting Fixtures/Ceiling fan/Bulbs", "Windows/Coverings/Screens",
        "Electrical Outlets",
    ]),
    ("Stairwell and Hall", [
        "Treads and Landings", "Railing/Bannister", "Walls and trim", "Ceilings",
        "Closets", "Lighting Fixtures/Bulbs", "Windows/Coverings/Screens",
        "Electrical Outlets",
    ]),
    ("Main Bathroom", [
        "Ceiling", "Walls and Trim", "Floor/Carpet", "Cabinets and Mirror",
        "Tub/Shower/Taps/Stopper", "Sink/Stopper/Taps", "Toilet", "Door",
        "Lighting Fixtures/Ceiling Fans/Bulbs", "Window/Coverings/Screens",
        "Electrical Outlets",
    ]),
    ("Bedroom", [
        # RTB-27 lists "Master Bedroom (1)" and "Bedroom (2)" with the same
        # items; we keep ONE Bedroom section and the builder instantiates it
        # per room (room leases relabel it "Bedroom — <room name>").
        "Ceiling", "Walls and Trim", "Floor/Carpet", "Closet(s)", "Doors",
        "Lighting Fixtures/Ceiling Fans/Bulbs", "Window/Coverings/Screens",
        "Electrical Outlets",
    ]),
    ("Exterior", [
        "Front and Rear Entrances", "Patio/Balcony Doors", "Garbage Containers",
        "Glass and Frames", "Stucco and/or siding", "Lighting Fixtures/Bulbs",
        "Grounds and Walks", "Electrical Outlets",
    ]),
    ("Utility Room", ["Washer/Dryer", "Electrical Outlets"]),
    ("Garage or Parking Area", ["Electrical Outlets"]),
    ("Basement", [
        "Stair and Stairwell", "Walls and Floor/Carpet",
        "Furnace, Water Heater, Plumbing", "Windows/Coverings/Screens",
        "Lighting Fixtures/Bulbs", "Electrical outlets",
    ]),
    ("Storage", ["General condition"]),
]


class Command(BaseCommand):
    help = "Seed the BC (RTB-27) and GENERIC condition-inspection templates. Idempotent."

    @transaction.atomic
    def handle(self, *args, **options):
        for province, name in (
            ("BC", "BC Condition Inspection Report (RTB-27)"),
            ("GENERIC", "Standard Condition Inspection Report"),
        ):
            template, created = InspectionTemplate.objects.get_or_create(
                province=province,
                version=VERSION,
                defaults={"name": name, "is_active": True},
            )
            if not created:
                self.stdout.write(
                    f"{province} v{VERSION} already seeded "
                    f"({template.items.count()} items) — skipping."
                )
                continue
            rows, sort = [], 0
            for section, labels in RTB27_ITEMS:
                for label in labels:
                    sort += 10
                    rows.append(
                        InspectionTemplateItem(
                            template=template,
                            section=section,
                            label=label,
                            sort_order=sort,
                        )
                    )
            InspectionTemplateItem.objects.bulk_create(rows)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Seeded {province} v{VERSION}: {len(rows)} items across "
                    f"{len(RTB27_ITEMS)} sections."
                )
            )