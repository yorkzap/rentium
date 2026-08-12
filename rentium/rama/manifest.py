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
    # Phase 3: whether the generic `update` primitive may set this field.
    # DEFAULT-DENY — a field is never writable unless this is explicitly True and
    # the entity's edit_guard (below) allows editing in the instance's state.
    editable: bool = False
    # Phase 5 — aggregation. All default-deny, for the same reason `editable`
    # is: a field reachable by accident is a field reported by accident.
    #
    # sum/avg/min/max. Only meaningful on a quantity.
    aggregatable: bool = False
    # A legal group_by key. Restricted to bounded-cardinality types, because
    # grouping by a free-text field produces one group per row and reads like
    # a table that means something.
    groupable: bool = False
    # Name of a queryset ANNOTATION rather than a model field. `charge_state`
    # and `outstanding` do not exist in the database; they are computed by the
    # entity's `annotate` methods. Declared here so filtering and grouping
    # reach them by the name a landlord would use.
    source: str = ""
    # Whether "is empty" / "is set" are legal on this field. Most fields in
    # this codebase are `blank=True, default=""` rather than nullable, so
    # isnull would silently match nothing; the read layer checks both.
    nullable: bool = False

    @property
    def lookup_path(self) -> str:
        """What the ORM should be handed for this field."""
        return self.source or self.name


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
    # Phase 3: a predicate (instance) -> (allowed: bool, reason: str) gating ALL
    # edits to an instance by its current state (e.g. a locked lease). None = no
    # state gate (individual fields still need editable=True).
    edit_guard: object = None
    # How `update` (and `link`) resolve ONE instance from a text query. Falls back
    # to the LinkSpec so entities with a detail page don't repeat themselves;
    # entities without one (work orders, inquiries, inventory) set these to be
    # editable-by-name.
    lookup: tuple[str, ...] = ()
    label_field: str = ""
    # Zero-argument QuerySet methods applied to EVERY read of this entity,
    # before any user filter — the entity's definition of "the rows that
    # count". `read` was the only ledger reader in the codebase that did not
    # start from .not_voided(), so it reported reversed charges as live and
    # would have summed them once aggregation arrived. Structural, because a
    # rule the model has to remember to pass is a rule it will eventually
    # forget.
    base_queryset: tuple[str, ...] = ()
    # Human-readable note appended to results, explaining what base_queryset
    # removed. A total the model cannot explain is a total it will misreport.
    scope_note: str = ""
    # Zero-argument QuerySet methods producing the fields declared with a
    # `source`. Applied only when such a field is actually referenced, so an
    # ordinary read does not pay for the join.
    annotate: tuple[str, ...] = ()
    # The date field that `month=` / `year=` / `between=` narrow. Without one,
    # every time question has to be spelled as two explicit bounds.
    date_field: str = ""

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}

    def aggregatable_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields if f.aggregatable}

    def groupable_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields if f.groupable}

    def _apply(self, queryset, names, kind):
        for name in names:
            method = getattr(queryset, name, None)
            if method is None or not callable(method):
                raise AttributeError(
                    f"{self.key}.{kind} names {name!r}, which is not a "
                    f"method on {type(queryset).__name__}.",
                )
            queryset = method()
        return queryset

    def base_queryset_for(self, manager):
        """Apply the entity's standing filters, failing loudly on a typo."""
        return self._apply(manager, self.base_queryset, "base_queryset")

    def annotate_for(self, queryset):
        """Add the derived fields. Only called when one is referenced."""
        return self._apply(queryset, self.annotate, "annotate")

    def editable_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields if f.editable}

    def resolve_lookup(self) -> tuple[str, ...]:
        return self.lookup or (self.links.lookup if self.links else ())

    def resolve_label(self) -> str:
        return self.label_field or (self.links.label_field if self.links else "pk")


def _lease_edit_guard(inst):
    """A lease's fields are editable only while it isn't locked (ACTIVE+). Mirrors
    the LeaseNotLocked permission so RAMA can never rewrite an executed lease."""
    if inst.is_locked():
        return False, "This lease is active/executed and can no longer be edited."
    return True, ""


def _lease_tenant_edit_guard(inst):
    """Same gate, reached through the parent lease — LeaseNotLocked resolves a
    LeaseTenant to its lease for exactly this reason."""
    return _lease_edit_guard(inst.lease)


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
        FieldSpec("name", "Name", editable=True),
        FieldSpec("status", "Status", "enum", display="get_status_display",
                  editable=True),
        FieldSpec("property_category", "Category", "enum",
                  display="get_property_category_display", editable=True),
        FieldSpec("room_type", "Room type", "enum", display="get_room_type_display"),
        FieldSpec("unit_type", "Unit type", "enum", display="get_unit_type_display"),
        FieldSpec("bedrooms", "Bedrooms", "number"),
        FieldSpec("bathrooms", "Bathrooms", "number"),
        FieldSpec("max_occupancy", "Maximum occupancy", "number"),
        FieldSpec("square_footage", "Square footage", "number"),
        FieldSpec("address", "Address", editable=True),
        FieldSpec("city", "City", editable=True),
        FieldSpec("province", "Province", "enum", editable=True),
        FieldSpec("postal_code", "Postal code", editable=True),
        FieldSpec("asking_rent", "Asking rent", "money", editable=True),
        FieldSpec("is_publicly_visible", "Publicly visible", "bool", editable=True),
        FieldSpec("neighbourhood", "Neighbourhood", editable=True),
        FieldSpec("available_from", "Available from", "date", editable=True),
        FieldSpec("description", "Description", filterable=False, editable=True),
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
    lookup=("lease_number", "property__name"),  # target by number OR its listing
    label_field="lease_number",
    fields=[
        FieldSpec("lease_number", "Lease number"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("lease_type", "Agreement type", "enum",
                  display="get_lease_type_display"),
        FieldSpec("start_date", "Start date", "date", editable=True),
        FieldSpec("end_date", "End date", "date", editable=True),
        FieldSpec("is_month_to_month", "Month-to-month", "bool", editable=True),
        FieldSpec("total_rent", "Total monthly rent", "money"),  # rebalances → bespoke
        FieldSpec("security_deposit", "Security deposit", "money", editable=True),
        FieldSpec("pet_deposit", "Pet deposit", "money", editable=True),
        FieldSpec("cleaning_deposit", "Cleaning deposit", "money", editable=True),
        FieldSpec("pets_allowed", "Pets allowed", "bool", editable=True),
        FieldSpec("smoking_allowed", "Smoking allowed", "bool", editable=True),
        FieldSpec("parking_included", "Parking included", "bool", editable=True),
        FieldSpec("parking_description", "Parking details", editable=True),
        FieldSpec("parking_extra_charge", "Parking charge", "money", editable=True),
        FieldSpec("rent_due_day", "Rent due day", "number", editable=True),
        FieldSpec("pets_terms", "Pet terms", filterable=False, editable=True),
        FieldSpec("smoking_terms", "Smoking terms", filterable=False, editable=True),
        FieldSpec("special_terms", "Special terms", filterable=False, editable=True),
        FieldSpec(
            "services_and_facilities",
            "Services and facilities",
            filterable=False,
            editable=True,
        ),
        FieldSpec("landlord_signed", "Landlord signed", "bool"),
        FieldSpec("move_in_date", "Move-in date", "date", editable=True),
        FieldSpec("move_out_date", "Move-out date", "date", editable=True),
        FieldSpec("etransfer_email", "e-Transfer email", editable=True),
        FieldSpec("landlord_service_email", "Landlord notice email",
                  editable=True),
        FieldSpec("landlord_service_address", "Landlord notice address",
                  editable=True),
        FieldSpec("landlord_daytime_phone", "Landlord daytime phone",
                  editable=True),
        FieldSpec("landlord_other_phone", "Landlord other phone", editable=True),
        FieldSpec("landlord_fax", "Landlord fax", editable=True),
        FieldSpec("common_space_shared_with", "Shared areas used by", "json",
                  filterable=False),
    ],
    edit_guard=_lease_edit_guard,
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
        # Editable while the lease itself is: a roommate's share and their own
        # cleaning deposit are lease terms, and refusing them here left the
        # landlord with Django admin as the only route.
        FieldSpec("rent_amount", "Rent share", "money", editable=True),
        FieldSpec("cleaning_deposit", "Cleaning deposit", "money", editable=True),
        FieldSpec("is_primary_tenant", "Primary tenant", "bool"),
        FieldSpec("has_signed", "Signed", "bool"),
        FieldSpec("signed_date", "Signed on", "date"),
        FieldSpec("declined", "Declined", "bool"),
        FieldSpec("individual_start_date", "Their start date", "date"),
        FieldSpec("individual_end_date", "Their end date", "date"),
        FieldSpec("tenant_notes", "Notes", filterable=False),
    ],
    # Mirrors LeaseNotLocked, which walks a LeaseTenant up to its lease: a
    # roommate row on an executed lease is part of an executed lease.
    edit_guard=_lease_tenant_edit_guard,
    lookup=("invited_name", "invited_email", "tenant__user__email"),
    label_field="display_name",
)

WORK_ORDER = EntitySpec(
    key="work_order",
    model="maintenance.WorkOrder",
    label="Maintenance work order",
    scope_path="property__landlord",
    lookup=("title",),
    label_field="title",
    fields=[
        FieldSpec("title", "Title", editable=True),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("priority", "Priority", "enum", display="get_priority_display",
                  editable=True),
        FieldSpec("category", "Category", "enum", display="get_category_display",
                  editable=True),
        FieldSpec("origin", "Origin", "enum", display="get_origin_display"),
        FieldSpec("contractor_name", "Contractor", editable=True),
        FieldSpec("contractor_phone", "Contractor phone", editable=True),
        FieldSpec("scheduled_date", "Scheduled", "date", editable=True),
        FieldSpec("completed_date", "Completed", "date"),
        FieldSpec("cost", "Cost", "money", editable=True),
        FieldSpec("sla_due_at", "SLA due", "date"),
        FieldSpec("description", "Description", filterable=False, editable=True),
    ],
)

INQUIRY = EntitySpec(
    key="inquiry",
    model="showcase.Inquiry",
    label="Prospect inquiry / lead",
    scope_path="landlord",
    lookup=("name", "email"),
    label_field="name",
    fields=[
        FieldSpec("name", "Name"),
        FieldSpec("email", "Email"),
        FieldSpec("phone", "Phone"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("move_in_target", "Wants to move in", "date"),
        FieldSpec("responded_at", "Responded", "date"),
        FieldSpec("message", "Message", filterable=False),
        FieldSpec("landlord_notes", "Your notes", filterable=False, editable=True),
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
    # A voided entry is a reversal, not money owed. Every other ledger reader
    # (union.month_money, domain_reads.charge_schedule) starts from
    # .not_voided(); the generic read did not, so it counted reversed charges
    # as live.
    base_queryset=("not_voided",),
    scope_note="voided/reversed entries excluded",
    annotate=("with_charge_state",),
    date_field="due_date",
    default_order="due_date",
    fields=[
        # Without a way to narrow to ONE tenancy, every read of this entity was
        # portfolio-wide. Asked why a Room C balance looked wrong, RAMA read
        # every rent charge the landlord had, found another lease's $800, and
        # explained the discrepancy with charges that belong to somebody else.
        # Money questions are almost always about one lease; this is how you say
        # which.
        FieldSpec("lease__lease_number", "Lease number"),
        FieldSpec("property__name", "Property / listing"),
        FieldSpec(
            "entry_type", "Type", "enum",
            display="get_entry_type_display", groupable=True,
        ),
        FieldSpec("amount", "Amount", "money", aggregatable=True),
        FieldSpec("due_date", "Due date", "date"),
        FieldSpec("effective_date", "Effective date", "date"),
        # ---- derived (queryset annotations, not columns) -------------------
        # Whether a charge has been paid is NOT stored and is NOT `paid_on`:
        # clean() rejects paid_on on anything but an EXPENSE, so a rent
        # charge's is always empty. Grouping rents by paid_on reports every
        # rent as unpaid, confidently. The truth is the settlements FK, which
        # `with_charge_state` reduces to one enum.
        FieldSpec(
            "charge_state", "Charge state", "enum",
            source="charge_state", groupable=True,
        ),
        FieldSpec(
            "settled_amount", "Paid so far", "money",
            source="settled_amount", aggregatable=True,
        ),
        FieldSpec(
            "outstanding", "Still owed", "money",
            source="outstanding", aggregatable=True,
        ),
        # A FEE_CHARGE is a late fee (income) or damage recovery (not income),
        # told apart only by a work order. Declared so it can be excluded on
        # purpose rather than silently — see ledger/models.py damage_claims().
        FieldSpec(
            "is_damage_claim", "Damage-recovery claim", "bool",
            source="is_damage_claim", groupable=True,
        ),
        # Genuinely nullable, and the one place "is empty" means something:
        # an expense that has not yet cleared the bank.
        FieldSpec("paid_on", "Bank-cleared on (expenses only)", "date",
                  nullable=True),
        FieldSpec(
            "payment_method", "Method", "enum",
            display="get_payment_method_display", groupable=True,
        ),
        FieldSpec("reference_number", "Reference"),
        # A real choices field (ExpenseCategory), so "what did I spend on
        # maintenance this year?" is one grouped read rather than a tool.
        FieldSpec(
            "category", "Expense category", "enum",
            display="get_category_display", groupable=True,
        ),
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
    lookup=("name",),
    label_field="name",
    fields=[
        FieldSpec("name", "Item", editable=True),
        FieldSpec("quantity", "Quantity", "number", editable=True),
        FieldSpec("condition", "Condition", "enum", display="get_condition_display",
                  editable=True),
        FieldSpec("location_description", "Location", editable=True),
        FieldSpec("description", "Description", filterable=False, editable=True),
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

BUSINESS_DOCUMENT = EntitySpec(
    key="business_document",
    model="rama.RamaDocument",
    label="Business document",
    scope_path="landlord",
    fields=[
        FieldSpec("title", "Title"),
        FieldSpec("original_filename", "Original filename"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("kind", "Document type", "enum", display="get_kind_display"),
        FieldSpec("issuer", "Issuer"),
        FieldSpec("document_date", "Document date", "date"),
        FieldSpec("due_date", "Due date", "date"),
        FieldSpec("canonical_filename", "Canonical filename"),
    ],
    links=LinkSpec(
        page="/dashboard/documents?document={id}",
        lookup=("id", "title", "original_filename", "canonical_filename"),
        label_field="title",
        downloads=("archival PDF/A",),
    ),
)

LEASE_FORM = EntitySpec(
    key="lease_form",
    model="leases.LeaseForm",
    label="Lease form (RTB-8, addendum)",
    scope_path="lease__landlord",
    lookup=("title", "lease__lease_number", "template__code"),
    label_field="title",
    # Every field is read-only. A form's title, status and stage are all facts
    # about a document people sign; changing any of them through a generic
    # `update` would edit the paperwork out from under a signature. Writes go
    # through manage_lease_forms, which previews and confirms.
    fields=[
        FieldSpec("title", "Title"),
        FieldSpec("status", "Status", "enum", display="get_status_display"),
        FieldSpec("blocks_activation", "Holding up the lease", "bool"),
        FieldSpec("required", "Required", "bool"),
        FieldSpec("completed_at", "Fully signed on", "date"),
        FieldSpec("created_at", "Attached on", "date"),
    ],
    links=LinkSpec(
        page="/dashboard/leases/{lease_id}",
        lookup=("id", "title"),
        label_field="title",
        downloads=("signed PDF",),
    ),
)

MANIFEST: dict[str, EntitySpec] = {
    e.key: e
    for e in (
        PROPERTY, LEASE, LEASE_TENANT, WORK_ORDER, INQUIRY, APPOINTMENT,
        LEDGER_ENTRY, INSPECTION, INVENTORY, CONVERSATION, PROPERTY_GROUP,
        BUSINESS_DOCUMENT, LEASE_FORM,
    )
}


def capability_digest() -> str:
    """A compact, MANIFEST-DERIVED summary of what the generic read/update/link
    tools can reach, injected into the system prompt (Phase 4). Because it's
    generated from the manifest, adding an entity/field never requires editing the
    persona prose — capabilities come from data, the persona keeps only behaviour."""
    readable = ", ".join(MANIFEST.keys())
    editable_bits = []
    linkable = []
    for key, spec in MANIFEST.items():
        em = list(spec.editable_map())
        if em:
            shown = ", ".join(em[:6]) + ("…" if len(em) > 6 else "")
            editable_bits.append(f"{key} ({shown})")
        if spec.links is not None:
            linkable.append(key)
    from .links import DASHBOARD_COLLECTIONS

    # Counting/totalling is advertised the same way everything else here is:
    # generated from the manifest, so declaring a field aggregatable makes it
    # discoverable the same day, with no persona prose to update.
    totalling = []
    for key, spec in MANIFEST.items():
        sums = ", ".join(spec.aggregatable_map())
        groups = ", ".join(spec.groupable_map())
        if not (sums or groups):
            continue
        bits = []
        if sums:
            bits.append(f"total {sums}")
        if groups:
            bits.append(f"group by {groups}")
        if spec.date_field:
            bits.append(f"month=/year=/between= on {spec.date_field}")
        totalling.append(f"{key} ({'; '.join(bits)})")

    lines = [
        "## DATA SURFACE (manifest-derived — reach it generically; don't say "
        "'not supported' or log a gap for these)",
        f"- READ/FILTER any of: {readable}. Field names come from "
        "data_catalogue; use the `read` tool for specific/combined questions.",
    ]
    if totalling:
        lines.append(
            "- COUNT/TOTAL with `read`'s aggregate= and group_by= — computed "
            "over every matching row, not just the page. Do this instead of "
            "listing rows and counting them yourself: "
            + "; ".join(totalling)
            + ".",
        )
    lines.append(
        f"- EDIT via `update` (previews, then confirm): {'; '.join(editable_bits)}.",
    )
    lines.append(
        f"- DEEP-LINK / attachments via `link`: {', '.join(linkable)}. "
        f"Dashboard collections: {', '.join(DASHBOARD_COLLECTIONS)}.",
    )
    return "\n".join(lines)


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
