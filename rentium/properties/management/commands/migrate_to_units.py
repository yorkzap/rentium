"""Restructure the portfolio around PropertyUnit.

Usage:
    python manage.py migrate_to_units --dry-run     # report only, writes nothing
    python manage.py migrate_to_units               # apply

What this fixes
---------------
Every self-contained floor was stored as a PropertyGroup full of individually-
rentable ROOM listings, whether or not it is actually let room by room. So a
3-bedroom floor let to one family looked identical to 3 rooms let to 3
strangers, and the dashboard reported 14 rooms where the portfolio really has
9 physical units.

After this runs:
    3 holdings, 9 units, 12 active offerings
    (7 whole-unit listings + the 5 McKenzie rooms, which genuinely ARE let
     room by room and are left exactly as they are)

Safety
------
- Nothing is deleted. Room listings for floors that become WHOLE_UNIT are
  PARKED (is_active_offering=False) and stay attached to their unit, so
  switching that unit back to BY_ROOM reuses them with their history intact.
- Groups are kept and linked one-to-one to their unit for the same reason.
- Idempotent: keyed on (holding, unit name) and listing name, so a re-run is a
  no-op. Safe to dry-run, apply, and re-apply.
- Layout facts are never invented. A unit whose bathrooms we do not know is
  created with layout_complete=False and a note saying what is missing.

Verified before writing this: none of the Wascana or McCaughey room listings
has a lease, rent, photo, inventory item or work order, so parking them loses
nothing. Room C's lease and all five McKenzie room listings are untouched.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from rentium.properties.areas import seed_default_areas
from rentium.properties.models import Property
from rentium.properties.models import PropertyArea
from rentium.properties.models import PropertyGroup
from rentium.properties.models import PropertyHolding
from rentium.properties.models import PropertyUnit

WHOLE = PropertyUnit.RentalMode.WHOLE_UNIT
BY_ROOM = PropertyUnit.RentalMode.BY_ROOM

BED = PropertyArea.AreaType.BEDROOM
BATH = PropertyArea.AreaType.BATHROOM
KITCHEN = PropertyArea.AreaType.KITCHEN
LIVING = PropertyArea.AreaType.LIVING_ROOM
OFFICE = PropertyArea.AreaType.OFFICE

PRIVATE = PropertyArea.Kind.PRIVATE
COMMON = PropertyArea.Kind.COMMON


# --------------------------------------------------------------------- spec
# Declarative target state. `layout` lists only what we actually know; where a
# floor's bathrooms were never recorded, layout_complete is False and the note
# says so rather than guessing a number.
HOLDINGS = {
    "mckenzie": {
        "name": "950 McKenzie Ave",
        "address": "950 McKenzie Ave",
        "city": "Victoria",
        "province": "bc",
    },
    "wascana": {
        "name": "3213 Wascana St",
        "address": "3213 Wascana St",
        "city": "Victoria",
        # Recorded as Saskatchewan with a Regina postal code and Regina
        # coordinates, while the holding said Victoria. Corrected here; the
        # listings are marked unverified so the geocoder re-derives the rest.
        "province": "bc",
        "postal_code": "V9A 1B5",
        "fix_location": True,
    },
    "mccaughey": {
        "name": "5654 McCaughey Street",
        "address": "5654 McCaughey Street",
        "city": "Regina",
        "province": "sk",
        "postal_code": "S4W 0L5",
    },
}

UNITS = [
    # --- McKenzie: genuinely mixed. Left alone apart from gaining a unit. ---
    {
        "holding": "mckenzie",
        "name": "Garden Suite",
        "unit_type": PropertyUnit.UnitType.GARDEN_SUITE,
        "mode": WHOLE,
        "existing_listing": "Garden Suite",
        "layout_complete": False,
        "missing": "Room and bathroom counts for the garden suite were never recorded.",
        "layout": [],
    },
    {
        "holding": "mckenzie",
        "name": "Basement",
        "unit_type": PropertyUnit.UnitType.BASEMENT,
        "mode": BY_ROOM,
        "group": "McKenzie Basement",
        "layout_complete": False,
        "missing": "Shared bathroom/kitchen for the basement rooms were never recorded.",
        "layout": [],
    },
    {
        "holding": "mckenzie",
        "name": "Upstairs",
        "unit_type": PropertyUnit.UnitType.OTHER,
        "mode": BY_ROOM,
        "group": "Upstairs McKenzie",
        "layout_complete": False,
        "missing": "Shared bathroom/kitchen for the upstairs rooms were never recorded.",
        "layout": [],
    },
    # --- Wascana: four floors, each one whole unit. ---
    {
        "holding": "wascana",
        "name": "Basement",
        "unit_type": PropertyUnit.UnitType.BASEMENT,
        "mode": WHOLE,
        "group": "Wascana Basement",
        "listing_name": "Wascana Basement",
        "bedrooms": 2,
        "layout_complete": False,
        "missing": "Bathroom count not recorded.",
        "layout": [
            ("Bedroom 1", BED, PRIVATE),
            ("Bedroom 2", BED, PRIVATE),
            ("Kitchen", KITCHEN, COMMON),
            ("Living Room", LIVING, COMMON),
            ("Office / Den", OFFICE, COMMON),
        ],
    },
    {
        "holding": "wascana",
        "name": "Main Floor",
        "unit_type": PropertyUnit.UnitType.MAIN_FLOOR,
        "mode": WHOLE,
        "group": "Wascana Main Floor",
        "listing_name": "Wascana Main Floor",
        "bedrooms": 2,
        "layout_complete": False,
        "missing": "Bathroom count not recorded.",
        "layout": [
            ("Bedroom 1", BED, PRIVATE),
            ("Bedroom 2", BED, PRIVATE),
            ("Kitchen", KITCHEN, COMMON),
            ("Living Room", LIVING, COMMON),
        ],
    },
    {
        "holding": "wascana",
        "name": "Upstairs",
        "unit_type": PropertyUnit.UnitType.OTHER,
        "mode": WHOLE,
        "group": "Wascana Upstairs",
        "listing_name": "Wascana Upstairs",
        "bedrooms": 2,
        "layout_complete": False,
        "missing": "Bathroom and living-room details not recorded.",
        "layout": [
            ("Bedroom 1", BED, PRIVATE),
            ("Bedroom 2", BED, PRIVATE),
            ("Kitchen", KITCHEN, COMMON),
        ],
    },
    {
        "holding": "wascana",
        "name": "Garden Suite",
        "unit_type": PropertyUnit.UnitType.GARDEN_SUITE,
        "mode": WHOLE,
        "group": "Wascana Garden Suite",
        "listing_name": "Wascana Garden Suite",
        "layout_complete": False,
        "missing": "Nothing was ever recorded for this suite — the group was empty.",
        "layout": [],
    },
    # --- McCaughey: the clearest case. One floor, three bedrooms, let whole. ---
    {
        "holding": "mccaughey",
        "name": "Main Floor",
        "unit_type": PropertyUnit.UnitType.MAIN_FLOOR,
        "mode": WHOLE,
        "group": "McCaughey Main Floor",
        "listing_name": "McCaughey Main Floor",
        "bedrooms": 3,
        "bathrooms": "2.0",
        "layout_complete": True,
        "missing": "",
        "layout": [
            ("Master Bedroom", BED, PRIVATE),
            ("Bedroom 2", BED, PRIVATE),
            ("Bedroom 3", BED, PRIVATE),
            # The master's ensuite serves only the master; the second bathroom
            # serves the other two bedrooms.
            ("Master Ensuite", BATH, PRIVATE, ["Master Bedroom"]),
            ("Second Bathroom", BATH, COMMON, ["Bedroom 2", "Bedroom 3"]),
            ("Kitchen", KITCHEN, COMMON),
            ("Living Room", LIVING, COMMON),
        ],
    },
    {
        "holding": "mccaughey",
        "name": "Basement",
        "unit_type": PropertyUnit.UnitType.BASEMENT,
        "mode": WHOLE,
        "group": "McCaughey Basement",
        "listing_name": "McCaughey Basement",
        "layout_complete": False,
        "missing": "Nothing was ever recorded for this basement — the group was empty.",
        "layout": [],
    },
]


class Command(BaseCommand):
    help = "Restructure the portfolio around PropertyUnit. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the full plan without writing anything.",
        )

    def _say(self, msg, style=None):
        self.stdout.write(style(msg) if style else msg)

    @transaction.atomic
    def handle(self, *args, **options):
        dry = options["dry_run"]
        self.dry = dry
        landlords = {
            p.landlord for p in Property.objects.select_related("landlord")
        }
        if len(landlords) != 1:
            self._say(
                self.style.ERROR(
                    f"Expected exactly one landlord in this portfolio, found "
                    f"{len(landlords)}. Refusing to guess."
                )
            )
            return
        landlord = landlords.pop()

        holdings = self._ensure_holdings(landlord)
        stats = {"units": 0, "activated": 0, "parked": 0, "areas": 0}

        for spec in UNITS:
            self._migrate_unit(landlord, holdings, spec, stats)

        self._fix_wascana_location(landlord)
        self._report(landlord)

        summary = (
            f"Units: {stats['units']} | Offerings activated: {stats['activated']} | "
            f"Room listings parked: {stats['parked']} | Layout areas: {stats['areas']}"
        )
        if dry:
            transaction.set_rollback(True)
            self._say(self.style.WARNING(f"\nDRY RUN — nothing written. {summary}"))
        else:
            self._say(self.style.SUCCESS(f"\n{summary}"))

    # ------------------------------------------------------------ holdings
    def _ensure_holdings(self, landlord):
        out = {}
        for key, spec in HOLDINGS.items():
            holding = PropertyHolding.objects.filter(
                landlord=landlord, name=spec["name"]
            ).first()
            if holding is None:
                self._say(f"  + holding {spec['name']!r} (new)")
                if not self.dry:
                    holding = PropertyHolding.objects.create(
                        landlord=landlord,
                        name=spec["name"],
                        address=spec["address"],
                        city=spec["city"],
                    )
            else:
                self._say(f"  = holding {spec['name']!r} (exists)")
            out[key] = holding
        return out

    # ---------------------------------------------------------------- units
    def _migrate_unit(self, landlord, holdings, spec, stats):
        holding = holdings[spec["holding"]]
        label = f"{spec['holding']}/{spec['name']}"
        mode = spec["mode"]

        unit = None
        if holding is not None:
            unit = PropertyUnit.objects.filter(
                holding=holding, name=spec["name"]
            ).first()
        verb = "=" if unit else "+"
        self._say(
            f"\n{verb} unit {label!r} [{mode}] "
            f"{'complete' if spec['layout_complete'] else 'LAYOUT INCOMPLETE'}"
        )
        if spec["missing"]:
            self._say(f"    missing: {spec['missing']}")

        if not self.dry and unit is None:
            unit = PropertyUnit.objects.create(
                landlord=landlord,
                holding=holding,
                name=spec["name"],
                unit_type=spec.get("unit_type", ""),
                rental_mode=mode,
                layout_complete=spec["layout_complete"],
                missing_layout_notes=spec["missing"],
            )
        stats["units"] += 1

        group = None
        if spec.get("group"):
            group = PropertyGroup.objects.filter(
                landlord=landlord, name=spec["group"]
            ).first()
        rooms = list(group.grouped_properties.all()) if group else []

        if group is not None:
            self._say(f"    group {group.name!r} -> linked to unit ({len(rooms)} rooms)")
            if not self.dry:
                group.unit = unit
                group.save(update_fields=["unit", "updated_at"])

        # Attach every room listing to the unit, whatever the mode. Under
        # WHOLE_UNIT they are parked but stay attached, so a switch back finds
        # them.
        for room in rooms:
            if not self.dry:
                room.unit = unit
                room.holding = holding
                room.is_active_offering = mode == BY_ROOM
                room.save(
                    update_fields=[
                        "unit", "holding", "is_active_offering", "updated_at"
                    ]
                )
            if mode == WHOLE:
                stats["parked"] += 1
        if mode == WHOLE and rooms:
            self._say(
                f"    parking {len(rooms)} room listing(s): "
                f"{', '.join(r.name for r in rooms)}"
            )
            self._say("      (kept, not deleted — a switch back reuses them)")

        self._ensure_offering(landlord, holding, unit, spec, rooms, stats)
        self._write_layout(unit, spec, stats)

    def _ensure_offering(self, landlord, holding, unit, spec, rooms, stats):
        """The listing actually on the market for this unit."""
        if spec["mode"] == BY_ROOM:
            self._say(
                f"    offerings: {len(rooms)} room listing(s) stay active "
                f"({', '.join(r.name for r in rooms)})"
            )
            stats["activated"] += len(rooms)
            return

        name = spec.get("listing_name") or spec.get("existing_listing")
        listing = Property.objects.filter(landlord=landlord, name=name).first()
        if listing is not None:
            self._say(f"    offering {name!r} (reusing existing listing)")
            if not self.dry:
                listing.unit = unit
                listing.holding = holding
                listing.is_active_offering = True
                listing.save(
                    update_fields=[
                        "unit", "holding", "is_active_offering", "updated_at"
                    ]
                )
            stats["activated"] += 1
            return

        holding_spec = HOLDINGS[spec["holding"]]
        self._say(f"    offering {name!r} (new COMPLETE_UNIT listing)")
        stats["activated"] += 1
        if self.dry:
            return
        Property.objects.create(
            landlord=landlord,
            holding=holding,
            unit=unit,
            name=name,
            property_category=Property.PropertyCategory.COMPLETE_UNIT,
            unit_type=spec.get("unit_type") or Property.UnitType.OTHER,
            bedrooms=spec.get("bedrooms"),
            bathrooms=spec.get("bathrooms"),
            address=holding_spec["address"],
            city=holding_spec["city"],
            province=holding_spec["province"],
            postal_code=holding_spec.get("postal_code", ""),
            # Not publicly listed until the landlord decides to advertise it.
            is_publicly_visible=False,
            status=Property.PropertyStatus.AVAILABLE,
        )

    def _write_layout(self, unit, spec, stats):
        """Record the unit's internal spaces — only what is actually known."""
        layout = spec["layout"]
        if not layout:
            self._say("    layout: nothing recorded (flagged incomplete)")
        else:
            self._say(
                f"    layout: {', '.join(item[0] for item in layout)}"
            )
        stats["areas"] += len(layout)
        if self.dry or unit is None:
            return

        by_name = {}
        for item in layout:
            name, area_type, kind = item[0], item[1], item[2]
            # update_or_create, not get_or_create: creating this unit's listing
            # fires the seeding signal first, so a generic "Kitchen" placeholder
            # may already exist under this name. A space the landlord actually
            # told us about must be promoted out of scaffolding, or RAMA will
            # keep reporting it as unknown.
            area, _created = PropertyArea.objects.update_or_create(
                unit=unit,
                name=name,
                defaults={
                    "area_type": area_type,
                    "kind": kind,
                    "is_seeded_default": False,
                },
            )
            by_name[name] = area
        # Second pass: which bedrooms each bathroom serves.
        for item in layout:
            if len(item) < 4:
                continue
            area = by_name[item[0]]
            area.serves_areas.set([by_name[n] for n in item[3] if n in by_name])

        # Scaffolding for maintenance/inspections, flagged so it can never be
        # mistaken for recorded layout.
        seed_default_areas(unit=unit)

    # ------------------------------------------------------------- location
    def _fix_wascana_location(self, landlord):
        spec = HOLDINGS["wascana"]
        if not spec.get("fix_location"):
            return
        wrong = Property.objects.filter(
            landlord=landlord, address=spec["address"]
        ).exclude(province=spec["province"])
        if not wrong.exists():
            return
        self._say(
            f"\n! location fix: {wrong.count()} Wascana listing(s) recorded as "
            f"province='sk' with a Regina postal code and Regina coordinates, "
            f"while the address is in Victoria."
        )
        self._say(
            f"    -> province='{spec['province']}', postal_code="
            f"'{spec['postal_code']}', coordinates cleared, "
            f"address_verified=False (so the geocoder re-derives them)"
        )
        if self.dry:
            return
        for prop in wrong:
            prop.province = spec["province"]
            prop.city = spec["city"]
            prop.postal_code = spec["postal_code"]
            prop.latitude = None
            prop.longitude = None
            prop.address_verified = False
            prop.save(
                update_fields=[
                    "province", "city", "postal_code", "latitude", "longitude",
                    "address_verified", "updated_at",
                ]
            )

    # --------------------------------------------------------------- report
    def _report(self, landlord):
        if self.dry:
            self._say(
                "\n(dry run: the counts below reflect the CURRENT database, "
                "not the plan above)"
            )
        self._say("\n--- resulting portfolio ---")
        self._say(f"  holdings: {PropertyHolding.objects.filter(landlord=landlord).count()}")
        self._say(f"  units:    {PropertyUnit.objects.filter(landlord=landlord).count()}")
        active = Property.objects.filter(landlord=landlord, is_active_offering=True)
        parked = Property.objects.filter(landlord=landlord, is_active_offering=False)
        self._say(f"  active offerings: {active.count()}")
        self._say(f"  parked listings:  {parked.count()}")
