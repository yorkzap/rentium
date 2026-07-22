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
class LinkSpec:
    """How to produce a clickable in-app deep link (and note downloads) for an
    instance of this entity — Phase 2. `page` is a frontend path templated with
    `{id}`; `lookup` are the fields a text query resolves against; `label_field`
    names the instance in the reply; `downloads` are artifacts (e.g. the lease
    PDF) available on that page."""

    page: str
    lookup: tuple[str, ...]
    label_field: str
    downloads: tuple[str, ...] = ()


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
    links: LinkSpec | None = None

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
    links=LinkSpec(
        page="/dashboard/properties/{id}",
        lookup=("name", "address"),
        label_field="name",
        downloads=("photos & full details",),
    ),
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
    links=LinkSpec(
        page="/dashboard/leases/{id}",
        lookup=("lease_number",),
        label_field="lease_number",
        downloads=("signed PDF",),
    ),
)

LEASE_TENANT = EntitySpec(
    key="lease_tenant",
    model="leases.LeaseTenant",
    label="Tenant on a lease",
    scope_path="lease__landlord",  # secrets (invite_token) are simply not declared
    fields=[
        FieldSpec("invited_name", "Name"),
        FieldSpec("invited_email", "Email"),
        FieldSpec("invited_phone", "Phone"),
        FieldSpec("rent_amount", "Rent share", "money"),
        FieldSpec("cleaning_fee", "Cleaning fee", "money"),
        FieldSpec("is_primary_tenant", "Primary tenant", "bool"),
        FieldSpec("has_signed", "Signed", "bool"),
        FieldSpec("signed_date", "Signed on", "date"),
        FieldSpec("declined", "Declined", "bool"),
        FieldSpec("individual_start_date", "Their start date", "date"),
        FieldSpec("individual_end_date", "Their end date", "date"),
        FieldSpec("tenant_notes", "Notes", filterable=False),
    ],
)

WORK_ORDER = EntitySpec(
    key="work_order",
    model="maintenance.WorkOrder",
    label="Maintenance work order",
    scope_path="property__landlord",
    fields=[
        FieldSpec("title", "Title"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("priority", "Priority", "enum", display="get_priority_display"),
        FieldSpec("category", "Category", "enum", display="get_category_display"),
        FieldSpec("origin", "Origin", "enum", display="get_origin_display"),
        FieldSpec("contractor_name", "Contractor"),
        FieldSpec("contractor_phone", "Contractor phone"),
        FieldSpec("scheduled_date", "Scheduled", "date"),
        FieldSpec("completed_date", "Completed", "date"),
        FieldSpec("cost", "Cost", "money"),
        FieldSpec("sla_due_at", "SLA due", "date"),
        FieldSpec("description", "Description", filterable=False),
    ],
)

INQUIRY = EntitySpec(
    key="inquiry",
    model="showcase.Inquiry",
    label="Prospect inquiry / lead",
    scope_path="landlord",
    fields=[
        FieldSpec("name", "Name"),
        FieldSpec("email", "Email"),
        FieldSpec("phone", "Phone"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("move_in_target", "Wants to move in", "date"),
        FieldSpec("responded_at", "Responded", "date"),
        FieldSpec("message", "Message", filterable=False),
        FieldSpec("landlord_notes", "Your notes", filterable=False),
    ],
)

APPOINTMENT = EntitySpec(
    key="appointment",
    model="appointments.Appointment",
    label="Viewing / visit",
    scope_path="landlord",  # public_token is a secret → not declared
    fields=[
        FieldSpec("kind", "Kind", "enum", display="get_kind_display"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("starts_at", "Starts", "date"),
        FieldSpec("ends_at", "Ends", "date"),
        FieldSpec("time_class", "Timing", "enum", display="get_time_class_display"),
        FieldSpec("contact_name", "Contact"),
        FieldSpec("contact_email", "Contact email"),
        FieldSpec("contact_phone", "Contact phone"),
        FieldSpec("tenant_consent", "Tenant consent", "bool"),
        FieldSpec("notes", "Notes", filterable=False),
    ],
)

LEDGER_ENTRY = EntitySpec(
    key="ledger_entry",
    model="ledger.LedgerEntry",
    label="Ledger entry (charge / payment / expense)",
    scope_path="landlord",  # idempotency_key / metadata are internal → not declared
    fields=[
        FieldSpec("entry_type", "Type", "enum", display="get_entry_type_display"),
        FieldSpec("amount", "Amount", "money"),
        FieldSpec("due_date", "Due date", "date"),
        FieldSpec("effective_date", "Effective date", "date"),
        FieldSpec("paid_on", "Paid on", "date"),
        FieldSpec("payment_method", "Method"),
        FieldSpec("reference_number", "Reference"),
        FieldSpec("category", "Category"),
        FieldSpec("vendor", "Vendor"),
        FieldSpec("description", "Description", filterable=False),
    ],
)

INSPECTION = EntitySpec(
    key="inspection",
    model="leases.ConditionInspection",
    label="Move-in / move-out condition inspection",
    scope_path="lease__landlord",
    fields=[
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("possession_date", "Possession date", "date"),
        FieldSpec("move_in_inspection_date", "Move-in inspection", "date"),
        FieldSpec("move_out_inspection_date", "Move-out inspection", "date"),
        FieldSpec("move_out_date", "Move-out date", "date"),
        FieldSpec("deduction_security_deposit", "Deposit deduction", "money"),
        FieldSpec("deduction_pet_deposit", "Pet-deposit deduction", "money"),
        FieldSpec("tenant_responsible_damage", "Tenant-caused damage", "bool"),
        FieldSpec("repairs_required_at_start", "Repairs needed at start", "bool"),
        FieldSpec("tenant_forwarding_address", "Forwarding address", filterable=False),
    ],
)

INVENTORY = EntitySpec(
    key="inventory",
    model="properties.InventoryItem",
    label="Furnishing / inventory item",
    scope_path="property__landlord",
    fields=[
        FieldSpec("name", "Item"),
        FieldSpec("quantity", "Quantity", "number"),
        FieldSpec("condition", "Condition", "enum", display="get_condition_display"),
        FieldSpec("location_description", "Location"),
        FieldSpec("description", "Description", filterable=False),
    ],
)

CONVERSATION = EntitySpec(
    key="conversation",
    model="messaging.Conversation",
    label="Message thread",
    scope_path="landlord",  # access_token is a secret → not declared
    fields=[
        FieldSpec("subject", "Subject"),
        FieldSpec("prospect_name", "Prospect name"),
        FieldSpec("prospect_email", "Prospect email"),
    ],
)

PROPERTY_GROUP = EntitySpec(
    key="property_group",
    model="properties.PropertyGroup",
    label="Property group / unit",
    scope_path="landlord",
    fields=[
        FieldSpec("name", "Name"),
        FieldSpec("description", "Description", filterable=False),
    ],
    links=LinkSpec(
        page="/dashboard/properties/view-group/{id}",
        lookup=("name",),
        label_field="name",
    ),
)

MANIFEST: dict[str, EntitySpec] = {
    e.key: e
    for e in (
        PROPERTY, LEASE, LEASE_TENANT, WORK_ORDER, INQUIRY, APPOINTMENT,
        LEDGER_ENTRY, INSPECTION, INVENTORY, CONVERSATION, PROPERTY_GROUP,
    )
}


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
