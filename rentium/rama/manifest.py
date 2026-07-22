"""
Domain Capability Manifest (DCM) — Phase 1 (reads).

ONE declarative source of truth for what RAMA may read, per entity. A generic
`read` primitive (rama/domain_read.py) operates over this instead of a bespoke
tool per question, so RAMA can answer composed questions ("active leases with
parking and rent over 800") without anyone hand-building a tool first.

Safety by construction:
  - Only fields declared here are ever selected or returned (default-deny).
  - Only fields marked filterable can appear in a filter.
  - Every query is scoped to the acting landlord via `scope_path` — a row that
    isn't the landlord's can never be reached, regardless of the filters asked.

This is deliberately reads-only for Phase 1: a read can't corrupt anything, so it
is the safe place to prove the manifest approach before writes (Phase 3) ride on
the same declaration. See docs/RAMA_SMARTNESS_ARCHITECTURE.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FieldSpec:
    name: str  # model field name (source of truth)
    label: str  # human label RAMA shows / reasons over
    type: str = "string"  # string | number | money | date | bool | enum | json
    filterable: bool = True
    # For enum fields, the model method that renders the display value (e.g.
    # get_status_display) so RAMA can filter/read by the readable name.
    display: str = ""


@dataclass(frozen=True)
class EntitySpec:
    key: str  # what RAMA passes as entity="…"
    model: str  # "app_label.ModelName"
    label: str
    # ORM path from the model to its owning LandlordProfile. Every query is
    # filtered by {scope_path: landlord}, so results never cross tenants.
    scope_path: str
    fields: list[FieldSpec] = field(default_factory=list)
    default_order: str = "-created_at"

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}


# --------------------------------------------------------------------------- #
# The manifest. Phase 1 covers Property + Lease — the two entities most of the
# historical read/write gaps came from.
# --------------------------------------------------------------------------- #

PROPERTY = EntitySpec(
    key="property",
    model="properties.Property",
    label="Property / listing",
    scope_path="landlord",
    fields=[
        FieldSpec("name", "Name"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("property_category", "Category", "enum",
                  display="get_property_category_display"),
        FieldSpec("room_type", "Room type", "enum", display="get_room_type_display"),
        FieldSpec("unit_type", "Unit type", "enum", display="get_unit_type_display"),
        FieldSpec("address", "Address"),
        FieldSpec("city", "City"),
        FieldSpec("province", "Province"),
        FieldSpec("asking_rent", "Asking rent", "money"),
        FieldSpec("is_publicly_visible", "Publicly visible", "bool"),
        FieldSpec("neighbourhood", "Neighbourhood"),
        FieldSpec("available_from", "Available from", "date"),
        FieldSpec("description", "Description", filterable=False),
    ],
)

LEASE = EntitySpec(
    key="lease",
    model="leases.Lease",
    label="Lease / agreement",
    scope_path="landlord",
    fields=[
        FieldSpec("lease_number", "Lease number"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("lease_type", "Agreement type", "enum",
                  display="get_lease_type_display"),
        FieldSpec("start_date", "Start date", "date"),
        FieldSpec("end_date", "End date", "date"),
        FieldSpec("is_month_to_month", "Month-to-month", "bool"),
        FieldSpec("total_rent", "Total monthly rent", "money"),
        FieldSpec("security_deposit", "Security deposit", "money"),
        FieldSpec("pet_deposit", "Pet deposit", "money"),
        FieldSpec("cleaning_fee", "Cleaning fee", "money"),
        FieldSpec("pets_allowed", "Pets allowed", "bool"),
        FieldSpec("smoking_allowed", "Smoking allowed", "bool"),
        FieldSpec("parking_included", "Parking included", "bool"),
        FieldSpec("rent_due_day", "Rent due day", "number"),
        FieldSpec("landlord_signed", "Landlord signed", "bool"),
        FieldSpec("move_in_date", "Move-in date", "date"),
        FieldSpec("move_out_date", "Move-out date", "date"),
        FieldSpec("etransfer_email", "e-Transfer email"),
        FieldSpec("common_space_shared_with", "Shared areas used by", "json",
                  filterable=False),
    ],
)

MANIFEST: dict[str, EntitySpec] = {e.key: e for e in (PROPERTY, LEASE)}


def entity_catalogue() -> list[dict]:
    """A compact description of every readable entity + its fields — handed to
    RAMA so it knows what it can query without a bespoke tool per entity."""
    out = []
    for spec in MANIFEST.values():
        out.append(
            {
                "entity": spec.key,
                "label": spec.label,
                "fields": [
                    {"name": f.name, "label": f.label, "type": f.type,
                     "filterable": f.filterable}
                    for f in spec.fields
                ],
            }
        )
    return out
