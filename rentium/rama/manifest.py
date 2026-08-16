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
        # The lease paperwork's record of when a deposit was marked received —
        # NOT the money. The ledger is the source of truth for that
        # (DEPOSIT_CHARGE + charge_state), and on real data these two already
        # disagree: leases with no date here have partial settlements there.
        # The labels say so, because a field called "…received date" will
        # otherwise be quoted as proof of payment. sergeants.py watches the gap.
        FieldSpec(
            "security_deposit_received_date",
            "Security deposit marked received on the lease (not the ledger)",
            "date", nullable=True,
        ),
        FieldSpec(
            "pet_deposit_received_date",
            "Pet deposit marked received on the lease (not the ledger)",
            "date", nullable=True,
        ),
        FieldSpec(
            "cleaning_deposit_received_date",
            "Cleaning deposit marked received on the lease (not the ledger)",
            "date", nullable=True,
        ),
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

# A discount in Rentium is NOT a negative ledger entry. It is a RentAdjustment
# against a LeaseTenant, which changes what gets CHARGED — the ledger only ever
# shows the adjusted figure. Asked "did I give anyone a discount this month?",
# RAMA reasoned from generic accounting, searched the ledger for `amount<0`,
# found nothing, and answered "No — I don't see any discounts recorded", while a
# $1,600 DISCOUNT sat in this table. Not being able to see a thing produced a
# confident denial that it existed, which is worse than any error message.
RENT_ADJUSTMENT = EntitySpec(
    key="rent_adjustment",
    model="leases.RentAdjustment",
    label="Rent adjustment (discount / proration / increase)",
    # NOT `created_by`, which is the shortest path to a LandlordProfile and the
    # wrong one: it records who typed the adjustment, not whose portfolio it
    # belongs to. Scope is ownership, so it is declared, never inferred.
    scope_path="lease_tenant__lease__landlord",
    date_field="effective_date",
    default_order="-effective_date",
    fields=[
        FieldSpec(
            "adjustment_type", "Kind", "enum",
            display="get_adjustment_type_display", groupable=True,
        ),
        FieldSpec(
            "calculation_method", "How it was calculated", "enum",
            display="get_calculation_method_display", groupable=True,
        ),
        # Dollars off for FLAT_AMOUNT, percent for PERCENTAGE — summing across
        # a mix of both is meaningless, so the two are told apart by grouping
        # on calculation_method.
        FieldSpec("amount", "Amount off / on", "money", aggregatable=True),
        # The figure actually charged when set, overriding the arithmetic above.
        # This is the $400 the landlord remembered.
        FieldSpec(
            "target_amount", "Rent charged instead", "money",
            aggregatable=True, nullable=True,
        ),
        FieldSpec("reason", "Reason"),
        FieldSpec("effective_date", "Effective from", "date"),
        FieldSpec("end_date", "Until", "date", nullable=True),
        FieldSpec("is_recurring", "Every cycle", "bool", groupable=True),
    ],
)

# The middle of the hierarchy (holding → unit → property). Both were invisible,
# so "which units are in the McKenzie house?" had no answer at all.
PROPERTY_HOLDING = EntitySpec(
    key="property_holding",
    model="properties.PropertyHolding",
    label="Property holding (the physical address)",
    scope_path="landlord",
    lookup=("name", "address"),
    label_field="name",
    fields=[
        FieldSpec("name", "Name"),
        FieldSpec("kind", "Kind", "enum", display="get_kind_display",
                  groupable=True),
        FieldSpec("address", "Address"),
        # Free text, so not groupable — a group per distinct spelling reads
        # like a summary and isn't one.
        FieldSpec("city", "City"),
    ],
)

PROPERTY_UNIT = EntitySpec(
    key="property_unit",
    model="properties.PropertyUnit",
    label="Unit within a holding (floor / suite)",
    scope_path="landlord",
    lookup=("name",),
    label_field="name",
    fields=[
        FieldSpec("name", "Name"),
        FieldSpec("unit_type", "Unit type", "enum",
                  display="get_unit_type_display", groupable=True),
        # WHOLE vs BY_ROOM decides whether bedrooms are layout or lettable
        # offerings — see CLAUDE.md. A question about rooms means nothing
        # without it.
        FieldSpec("rental_mode", "Offered as", "enum",
                  display="get_rental_mode_display", groupable=True),
        FieldSpec("layout_complete", "Layout recorded", "bool", groupable=True),
        FieldSpec("missing_layout_notes", "Layout gaps", filterable=False),
    ],
)

OCCUPANCY = EntitySpec(
    key="occupancy",
    model="leases.Occupancy",
    label="Who physically lives in a room, and when",
    scope_path="lease__landlord",
    date_field="move_in",
    default_order="-move_in",
    fields=[
        FieldSpec("move_in", "Moved in", "date"),
        # Genuinely nullable and meaningful: blank = still living there.
        FieldSpec("move_out", "Moved out", "date", nullable=True),
        FieldSpec("note", "Note", filterable=False),
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

# --------------------------------------------------------------------------- #
# The relation graph
#
# The manifest was a flat list of fields per entity, so a question spanning two
# of them could not be asked. Told "have I received deposits from everyone
# moving in from August?", RAMA read the ledger once PER LEASE — because
# `lease__lease_number` was filterable on ledger_entry but not groupable, and
# `lease__start_date` was not declared at all — then ran out of its 45-second
# budget. Five hand-written aliases in domain_read._RELATION_FILTERS were the
# entire cross-entity surface, each one added after a landlord hit its absence.
#
# Relations are DERIVED from the models, not declared, so the graph cannot drift
# from the schema. Three rules make that safe:
#
#   1. Only relations whose TARGET MODEL IS ITSELF A MANIFEST ENTITY. This is
#      the security boundary and it maintains itself: ledger_entry.lease
#      resolves because `lease` is an entity; ledger_entry.created_by does not,
#      because User is not one and nobody decided what of a user is safe to
#      show.
#   2. Only FORWARD relations (FK / OneToOne). A reverse or many-to-many join
#      fans the parent row out, which silently multiplies every aggregate in
#      the query — the same defect just removed from with_settlement() by
#      making it a Subquery. Forbidding to-many traversal prevents it
#      structurally rather than by remembering.
#   3. A traversed field must still be DECLARED on the target entity. Reaching
#      `lease__start_date` gives exactly what reading `lease.start_date` gives;
#      default-deny is unchanged, and no field gains exposure by being
#      approached from a second direction.
# --------------------------------------------------------------------------- #

#: Depth 1 answers every question observed so far (ledger → lease); depth 2
#: covers the obvious next one (ledger → lease → property). Past that, join
#: cost and path ambiguity grow with nothing asking for them.
MAX_RELATION_DEPTH = 2


@dataclass(frozen=True)
class RelationSpec:
    name: str  # the FK attribute, e.g. "lease"
    target: str  # manifest entity key it points at
    label: str


def _model_label(model) -> str:
    return f"{model._meta.app_label}.{model.__name__}"


def relations_for(spec: EntitySpec) -> dict[str, RelationSpec]:
    """Forward relations from `spec` to other manifest entities.

    Derived from the model and cached, so declaring a new entity makes every
    existing entity's path to it work with no further edits.
    """
    cached = _RELATION_CACHE.get(spec.key)
    if cached is not None:
        return cached

    from django.apps import apps

    by_model = {}
    for key, other in MANIFEST.items():
        by_model[other.model.lower()] = key

    model = apps.get_model(*spec.model.split("."))
    found: dict[str, RelationSpec] = {}
    for field_ in model._meta.get_fields():
        # Forward FK / O2O only — see rule 2 above.
        if not (field_.many_to_one or field_.one_to_one):
            continue
        if not getattr(field_, "concrete", False):
            continue
        target_key = by_model.get(_model_label(field_.related_model).lower())
        if target_key is None:
            continue  # rule 1: not a manifest entity, not reachable
        found[field_.name] = RelationSpec(
            name=field_.name,
            target=target_key,
            label=str(getattr(field_, "verbose_name", field_.name)).title(),
        )
    _RELATION_CACHE[spec.key] = found
    return found


def resolve_path(spec: EntitySpec, path: str, _depth: int = 0):
    """Resolve 'lease__start_date' to (orm_path, FieldSpec) or (None, error).

    Returns the TARGET entity's own FieldSpec, so everything downstream —
    filterability, type coercion, aggregatable, groupable, display — behaves
    exactly as it does when reading that entity directly.
    """
    fieldspec = spec.field_map().get(path)
    if fieldspec is not None:
        return fieldspec.lookup_path, fieldspec, None

    if "__" not in path:
        return None, None, None  # not a path; caller reports it its own way

    head, tail = path.split("__", 1)
    relation = relations_for(spec).get(head)
    if relation is None:
        return None, None, None

    if _depth + 1 >= MAX_RELATION_DEPTH and "__" in tail:
        return None, None, (
            f"{path!r} goes more than {MAX_RELATION_DEPTH} relations deep. "
            f"Read {relation.target} directly instead."
        )

    target = MANIFEST[relation.target]
    inner_path, inner_spec, error = resolve_path(target, tail, _depth + 1)
    if error:
        return None, None, error
    if inner_spec is None:
        allowed = ", ".join(
            k for k, f in target.field_map().items() if f.filterable
        )
        return None, None, (
            f"{relation.target} has no field {tail!r}. "
            f"Available via {head}__: {allowed}."
        )
    if inner_spec.source:
        # An annotation on the related entity is not produced by this query's
        # queryset, so the path would resolve to a column that isn't there.
        return None, None, (
            f"{path!r} is a computed field on {relation.target} and can't be "
            f"reached through a relation. Read {relation.target} directly."
        )
    return f"{head}__{inner_path}", inner_spec, None


def relation_paths(spec: EntitySpec) -> list[str]:
    """Every relation reachable within MAX_RELATION_DEPTH, as dotted paths.

    Listing only the immediate neighbours is how "group these adjustments by
    lease" got refused with a list that did not contain the word "lease":
    RentAdjustment hangs off LeaseTenant, and the lease is one hop further.
    """
    out: list[str] = []
    frontier = [("", spec)]
    for _ in range(MAX_RELATION_DEPTH):
        nxt = []
        for prefix, current in frontier:
            for name, relation in sorted(relations_for(current).items()):
                path = f"{prefix}{name}"
                if path in out:
                    continue
                out.append(path)
                nxt.append((f"{path}__", MANIFEST[relation.target]))
        frontier = nxt
    return out


def resolve_relation(spec: EntitySpec, path: str):
    """Resolve 'lease_tenant__lease' to (orm_prefix, target EntitySpec), or None.

    A path that ends AT a relation rather than at one of its fields. The model
    writes these because they are the shortest way to say what it means —
    "adjustments on this lease" is `lease_tenant__lease=<id>`, and RentAdjustment
    has no direct lease FK to shorten it to.
    """
    parts = [p for p in (path or "").split("__") if p]
    if not parts or len(parts) > MAX_RELATION_DEPTH:
        return None
    current = spec
    for part in parts:
        relation = relations_for(current).get(part)
        if relation is None:
            return None
        current = MANIFEST[relation.target]
    return "__".join(parts), current


def relation_label_path(spec: EntitySpec, name: str) -> str | None:
    """The human column to group a relation by — 'lease' → 'lease__lease_number'.

    Grouping by the relation itself would key the table on a UUID, which is a
    correct answer nobody can read. The manifest already knows each entity's
    human name via resolve_label().
    """
    resolved = resolve_relation(spec, name)
    if resolved is None:
        return None
    # A path, not just a neighbour: "group these adjustments by lease" is
    # `lease_tenant__lease`, because RentAdjustment hangs off the tenancy.
    name, target = resolved
    fields = target.field_map()
    label_field = target.resolve_label()
    if label_field not in fields:
        # An entity's display name is often a PROPERTY, not a column —
        # LeaseTenant.display_name picks the signed-up name over the invited
        # one. You cannot GROUP BY a property, and returning None here meant
        # "group these adjustments by tenant" was simply refused. The lookup
        # fields are declared columns chosen to identify a row to a human,
        # which is the same job.
        label_field = next(
            (f for f in target.resolve_lookup() if f in fields),
            "",
        )
    if not label_field:
        return None
    return f"{name}__{label_field}"


_RELATION_CACHE: dict[str, dict[str, RelationSpec]] = {}


MANIFEST: dict[str, EntitySpec] = {
    e.key: e
    for e in (
        PROPERTY, LEASE, LEASE_TENANT, WORK_ORDER, INQUIRY, APPOINTMENT,
        LEDGER_ENTRY, RENT_ADJUSTMENT, INSPECTION, INVENTORY, CONVERSATION,
        PROPERTY_GROUP, PROPERTY_HOLDING, PROPERTY_UNIT, OCCUPANCY,
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
    # Traversal is useless if the model cannot tell it exists. Without this
    # line it read the ledger once PER LEASE, because nothing said the two
    # entities were connected.
    edges = []
    for key, spec in MANIFEST.items():
        rels = relations_for(spec)
        if rels:
            edges.append(f"{key} → {', '.join(sorted(rels))}")
    if edges:
        lines.append(
            "- FILTER AND GROUP ACROSS RELATIONS with `rel__field` — one query, "
            "never one per row. `read(entity='ledger_entry', "
            "filters='lease__start_date=2026-08-01..2026-08-31', "
            "group_by='lease')` answers a question about leases FROM the "
            "ledger. Grouping by a relation names it in the reply (a lease "
            "number, not an id). Available: "
            + "; ".join(edges)
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


def _entity_detail(spec: EntitySpec) -> dict:
    return {
        "entity": spec.key,
        "label": spec.label,
        "relations": {
            name: rel.target for name, rel in sorted(relations_for(spec).items())
        },
        "date_field": spec.date_field,
        "scope_note": spec.scope_note,
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "type": f.type,
                "filterable": f.filterable,
                **({"aggregatable": True} if f.aggregatable else {}),
                **({"groupable": True} if f.groupable else {}),
            }
            for f in spec.fields
        ],
    }


def entity_catalogue(entity: str = "") -> list[dict]:
    """What `read` can query. An INDEX by default; one entity's fields on request.

    Listing all 13 entities' fields is already ~3,300 tokens and grows with every
    declaration. Most of that is wasted: a turn needs one entity's fields, not
    thirteen. So the default answer is an index — names, counts and the relation
    graph, which is what tells the model two entities can be queried together —
    and the detail is a second call it makes only when it needs it.
    """
    wanted = (entity or "").strip().lower()
    if wanted:
        spec = MANIFEST.get(wanted)
        if spec is None:
            return [{"error": f"Unknown entity {entity!r}.",
                     "entities": list(MANIFEST)}]
        return [_entity_detail(spec)]

    index = []
    for spec in MANIFEST.values():
        index.append(
            {
                "entity": spec.key,
                "label": spec.label,
                "field_count": len(spec.fields),
                "relations": {
                    name: rel.target
                    for name, rel in sorted(relations_for(spec).items())
                },
                "can_total": list(spec.aggregatable_map()),
                "can_group_by": list(spec.groupable_map()),
                "date_field": spec.date_field,
            },
        )
    return index
