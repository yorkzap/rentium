"""
THE LEASE DOCUMENT REGISTRY.

One rendering of a lease, consumed by everything: the tenant's sign gate, the
landlord's lease page, and the PDF. Previously there were TWO independent
implementations of "what does this lease say" — src/lib/leaseFormats.ts on the
frontend and build_lease_pdf() on the backend — which meant a tenant could sign
one document on screen and download a different one. They were already drifting.

Adding Ontario later is: one new LeaseFormat subclass in this file, one line in
REGISTRY, its clause text in clauses.py, and any new nullable fields in
agreement.py. Zero React changes. Zero PDF changes. That is the whole reason
this file exists.

Structure:

    Row      one label/value line, or a block of prose
    Section  a titled group of rows + standing clause text (from clauses.py)
    Document what render(lease) returns — pure data, JSON-serialisable

Nothing here queries the network, mutates anything, or knows about HTTP. It is
a pure function of a Lease.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from decimal import Decimal

from .agreement import ServiceOrFacility
from .clauses import OFFICIAL_TEXT_LOADED
from .clauses import clauses_for


# --------------------------------------------------------------- primitives
@dataclass
class Row:
    label: str
    value: str
    block: bool = False  # render the value on its own line (long prose)


@dataclass
class Section:
    id: str
    title: str
    rows: list[Row] = field(default_factory=list)
    clauses: list[str] = field(default_factory=list)  # standing legal text
    note: str = ""  # small explanatory line under the title


@dataclass
class Document:
    format_id: str
    name: str
    subtitle: str
    legal_note: str
    official_text_loaded: bool
    sections: list[Section]

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        """Rebuild a Document from a stored snapshot (see capture_signed_document)."""
        sections = [
            Section(
                id=s["id"],
                title=s["title"],
                rows=[Row(**r) for r in s.get("rows", [])],
                clauses=list(s.get("clauses", [])),
                note=s.get("note", ""),
            )
            for s in data.get("sections", [])
        ]
        return cls(
            format_id=data["format_id"],
            name=data["name"],
            subtitle=data.get("subtitle", ""),
            legal_note=data.get("legal_note", ""),
            official_text_loaded=data.get("official_text_loaded", False),
            sections=sections,
        )


# ----------------------------------------------------------------- helpers
def money(v) -> str:
    if v in (None, ""):
        return "—"
    return f"${Decimal(str(v)):,.2f}"


def fmt_date(d: date | None) -> str:
    return d.strftime("%B %-d, %Y") if d else "—"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def tenant_contact(lt) -> str:
    """
    The email and phone that go on the agreement for one tenant slot.

    Both fields have existed on the models since the beginning; nothing ever
    populated the phone, so it printed blank on every agreement this app produced.
    It's now captured at the moment of signing (see the `sign` action in
    leases/api/views.py) and carried here.

    Reads through the same fallback chain as display_name: the linked account is
    authoritative, the landlord-entered invite details are the fallback for a slot
    that hasn't registered yet.
    """
    email = (lt.tenant.user.email if lt.tenant_id else lt.invited_email) or ""

    phone = ""
    if lt.tenant_id:
        phone = getattr(lt.tenant.user, "phone", "") or ""
    phone = phone or lt.invited_phone or ""

    return " · ".join(part for part in (email, phone) if part)


def tenant_rows(lease) -> list[Row]:
    """
    Every non-declined tenant slot, with the contact details a tenancy agreement is
    supposed to state.

    The name follows LeaseTenant.display_name's chain: the linked account's own
    name if they have one, else the full legal name the landlord typed on the
    invite. An invited-but-unregistered tenant prints as a person, not an email
    address.
    """
    slots = [lt for lt in lease.lease_tenants.all() if not lt.declined]
    rows = []
    for i, lt in enumerate(slots, start=1):
        label = "Tenant" if len(slots) == 1 else f"Tenant {i}"
        contact = tenant_contact(lt)
        rows.append(
            Row(label, f"{lt.display_name}{f' — {contact}' if contact else ''}")
        )
    return rows


def co_host_rows(lease) -> list[Row]:
    """Co-landlords on the lease — additional landlord parties shown on the
    agreement. Prefers the real signatory records (co-signers) and falls back to
    the lightweight co_hosts JSON (name-only, legacy)."""
    rows = []
    seen = set()
    for sig in lease.landlord_signatories.all():
        name = (sig.display_name or "").strip()
        if not name:
            continue
        seen.add(name.lower())
        contact = " · ".join(p for p in [sig.email, sig.phone] if p)
        rows.append(
            Row("Co-landlord", f"{name}{f' — {contact}' if contact else ''}")
        )
    for h in lease.co_hosts or []:
        name = (h.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        contact = " · ".join(p for p in [h.get("email", ""), h.get("phone", "")] if p)
        rows.append(Row("Co-landlord", f"{name}{f' — {contact}' if contact else ''}"))
    return rows


def signature_rows(lease) -> list[Row]:
    rows = [
        Row(
            f"Landlord — {lease.landlord.user.name}",
            f"Signed {fmt_date(lease.landlord_signed_date.date())}"
            if lease.landlord_signed and lease.landlord_signed_date
            else "Awaiting signature",
        )
    ]
    for lt in lease.lease_tenants.all():
        if lt.declined:
            rows.append(Row(f"Tenant — {lt.display_name}", "Declined"))
        elif lt.has_signed and lt.signed_date:
            rows.append(
                Row(
                    f"Tenant — {lt.display_name}",
                    f"Signed {fmt_date(lt.signed_date.date())}",
                )
            )
        else:
            rows.append(Row(f"Tenant — {lt.display_name}", "Awaiting signature"))
    return rows


def services_row(lease) -> Row:
    labels = dict(ServiceOrFacility.choices)
    included = [str(labels.get(s, s)) for s in (lease.services_and_facilities or [])]
    if not included:
        return Row(
            "Included in the rent",
            "No services or facilities are included in the rent.",
            block=True,
        )
    return Row("Included in the rent", ", ".join(included), block=True)


def parking_rows(lease) -> list[Row]:
    if lease.parking_included:
        return [Row("Parking", f"Included. {lease.parking_description or ''}".strip())]
    if lease.parking_extra_charge:
        return [
            Row(
                "Parking",
                f"{money(lease.parking_extra_charge)} per month, extra. "
                f"{lease.parking_description or ''}".strip(),
            )
        ]
    return [Row("Parking", "Not included.")]


def clause_context(lease) -> dict:
    return {
        "landlord": lease.landlord.user.name,
        "tenants": ", ".join(
            lt.display_name for lt in lease.lease_tenants.all() if not lt.declined
        ),
        "unit": lease.property.name
        if lease.property_id
        else (lease.group.name if lease.group_id else ""),
        "rent": money(lease.total_rent),
        "rent_due_day": ordinal(lease.rent_due_day or 1),
        "start_date": fmt_date(lease.start_date),
    }


# ------------------------------------------------------------------ formats
class LeaseFormat:
    id: str = "GENERIC"
    name: str = "Lease Agreement"
    subtitle: str = ""
    legal_note: str = ""

    def clauses(self, lease, section_id: str) -> list[str]:
        return clauses_for(self.id, section_id, clause_context(lease))

    def sections(self, lease) -> list[Section]:  # pragma: no cover - abstract
        raise NotImplementedError

    def render(self, lease) -> Document:
        return Document(
            format_id=self.id,
            name=self.name,
            subtitle=self.subtitle,
            legal_note=self.legal_note,
            official_text_loaded=OFFICIAL_TEXT_LOADED.get(self.id, False),
            sections=self.sections(lease),
        )


class BCResidentialFormat(LeaseFormat):
    """A whole self-contained unit in BC. Shaped after RTB-1."""

    id = "BC_RESIDENTIAL"
    name = "Residential Tenancy Agreement"
    subtitle = "British Columbia · Residential Tenancy Act"
    legal_note = (
        "This agreement is governed by the Residential Tenancy Act of British "
        "Columbia. Any term that contradicts the Act is void. The landlord must "
        "give the tenant a copy within 21 days of it being entered into."
    )

    def sections(self, lease) -> list[Section]:
        contact = lease.get_effective_landlord_contact()
        prop = lease.property
        out: list[Section] = []

        # 1. Parties
        out.append(
            Section(
                id="parties",
                title="1. Parties to this Agreement",
                rows=[
                    Row("Landlord", lease.landlord.user.name),
                    *co_host_rows(lease),
                    Row("Address for service", contact["address"] or "—"),
                    Row("Daytime phone", contact["daytime_phone"] or "—"),
                    Row("Email for service", contact["email"] or "—"),
                    *tenant_rows(lease),
                ],
            )
        )

        # 2. The rental unit
        unit_rows = [
            Row(
                "Rental unit",
                prop.name if prop else (lease.group.name if lease.group_id else "—"),
            )
        ]
        if prop:
            unit_rows.append(Row("Address", prop.address))
            if prop.bedrooms is not None:
                unit_rows.append(
                    Row("Size", f"{prop.bedrooms} bed · {prop.bathrooms or '—'} bath")
                )
        if lease.occupants:
            unit_rows.append(
                Row(
                    "Other occupants",
                    ", ".join(str(o) for o in lease.occupants)
                    + " — these people will live in the unit but are not tenants "
                    "under this agreement and have no tenancy rights or rent "
                    "obligation under it.",
                    block=True,
                )
            )

        out.append(
            Section(id="rental_unit", title="2. The Rental Unit", rows=unit_rows)
        )

        # 3. Term
        if lease.is_month_to_month:
            term = "Month-to-month, continuing until ended in accordance with the Act."
        elif lease.end_date:
            term = (
                f"Fixed term ending {fmt_date(lease.end_date)}. Unless both parties "
                "agree otherwise, the tenancy continues month-to-month on the same "
                "terms after that date."
            )
        else:
            term = "Continues until ended in accordance with the Act."

        term_rows = [
            Row("Tenancy begins", fmt_date(lease.start_date)),
            Row("Term", term, block=True),
        ]
        if lease.move_in_date and lease.move_in_date != lease.start_date:
            term_rows.insert(1, Row("Move-in date", fmt_date(lease.move_in_date)))
        if lease.fixed_term_end_reason:
            term_rows.append(
                Row(
                    "Tenant must vacate because",
                    lease.fixed_term_end_reason,
                    block=True,
                )
            )

        out.append(
            Section(
                id="term",
                title="3. Beginning and Length of the Tenancy",
                rows=term_rows,
                clauses=self.clauses(lease, "term"),
            )
        )

        # 4. Rent
        out.append(
            Section(
                id="rent",
                title="4. Rent",
                rows=[
                    Row("Rent", f"{money(lease.total_rent)} per month"),
                    Row(
                        "Payable on",
                        f"The {ordinal(lease.rent_due_day or 1)} day of each month",
                    ),
                    Row(
                        "Pay by",
                        f"e-Transfer to {lease.get_effective_etransfer_email()}",
                    ),
                    services_row(lease),
                    *parking_rows(lease),
                ],
                clauses=self.clauses(lease, "rent"),
            )
        )

        # 5. Deposits
        #
        # The RECEIVED dates matter and are not decoration: the date the landlord
        # receives a deposit starts the statutory clock for returning it at the end
        # of the tenancy. Nothing populated these until the ledger's payment handler
        # started stamping them, so every agreement previously said "Not yet
        # received" forever, including for deposits paid months earlier.
        dep_rows = [
            Row("Security deposit", money(lease.security_deposit)),
            Row(
                "Received on",
                fmt_date(lease.security_deposit_received_date)
                if lease.security_deposit_received_date
                else "Not yet received",
            ),
        ]
        if Decimal(str(lease.pet_deposit or 0)) > 0:
            dep_rows += [
                Row("Pet damage deposit", money(lease.pet_deposit)),
                Row(
                    "Received on",
                    fmt_date(lease.pet_deposit_received_date)
                    if lease.pet_deposit_received_date
                    else "Not yet received",
                ),
            ]
        if Decimal(str(lease.cleaning_fee or 0)) > 0:
            dep_rows.append(Row("Cleaning fee", money(lease.cleaning_fee)))

        out.append(
            Section(
                id="deposits",
                title="5. Security Deposit and Pet Damage Deposit",
                rows=dep_rows,
                clauses=self.clauses(lease, "deposits"),
            )
        )

        # 6. Condition inspections
        out.append(
            Section(
                id="inspections",
                title="6. Condition Inspections",
                clauses=self.clauses(lease, "inspections"),
            )
        )

        # 7. Pets, smoking, conduct
        conduct = [
            Row(
                "Pets",
                "Allowed. " + (lease.pets_terms or "No further conditions.")
                if lease.pets_allowed
                else "Not permitted.",
                block=True,
            ),
            Row(
                "Smoking",
                "Permitted. " + (lease.smoking_terms or "")
                if lease.smoking_allowed
                else "Not permitted anywhere in the rental unit or on the property.",
                block=True,
            ),
        ]
        out.append(
            Section(
                id="conduct",
                title="7. Pets and Smoking",
                rows=conduct,
                clauses=self.clauses(lease, "pets"),
            )
        )

        # 8. Occupants and guests
        out.append(
            Section(
                id="occupants_guests",
                title="8. Occupants and Guests",
                clauses=self.clauses(lease, "occupants_guests"),
            )
        )

        # 9. Standard terms
        out.append(
            Section(
                id="standard_terms",
                title="9. Standard Terms of Every Tenancy",
                note=(
                    "These terms apply to every residential tenancy in BC and cannot "
                    "be changed by agreement."
                ),
                clauses=self.clauses(lease, "standard_terms"),
            )
        )

        # 10. Ending the tenancy
        out.append(
            Section(
                id="ending",
                title="10. Ending the Tenancy",
                clauses=self.clauses(lease, "ending"),
            )
        )

        # 11. Service of documents
        out.append(
            Section(
                id="service_of_documents",
                title="11. Service of Documents",
                clauses=self.clauses(lease, "service_of_documents"),
            )
        )

        # 12. Additional terms
        if lease.special_terms:
            out.append(
                Section(
                    id="additional_terms",
                    title="12. Additional Terms",
                    rows=[
                        Row(
                            "Agreed between the parties",
                            lease.special_terms,
                            block=True,
                        )
                    ],
                    clauses=self.clauses(lease, "additional_terms"),
                )
            )

        # 13. Application of the Residential Tenancy Act
        out.append(
            Section(
                id="act_prevails",
                title="13. Application of the Residential Tenancy Act",
                clauses=self.clauses(lease, "act_prevails"),
            )
        )

        # 14. Signatures
        out.append(
            Section(
                id="signatures",
                title="14. Signatures",
                note="By signing, the landlord and each tenant are bound by the terms above.",
                rows=signature_rows(lease),
            )
        )

        return out


class GenericResidentialFormat(BCResidentialFormat):
    """
    Any complete unit outside BC/SK. Same document shape — the substantive terms of a
    residential tenancy are broadly the same everywhere — with the BC-specific
    statutory clauses omitted (clauses.py has no entries under this format id, so
    clauses_for() returns nothing and the sections render from the lease's own data).
    Slot the province's real clauses into clauses.py when you add one.
    """

    id = "GENERIC_RESIDENTIAL"
    name = "Residential Tenancy Agreement"
    subtitle = ""
    legal_note = (
        "This agreement is subject to the residential tenancy legislation of the "
        "province in which the rental unit is located. Any term that contradicts "
        "that legislation is unenforceable."
    )


class SKResidentialFormat(GenericResidentialFormat):
    id = "SK_RESIDENTIAL"
    subtitle = "Saskatchewan · Residential Tenancies Act"


class RoommateFormat(LeaseFormat):
    """
    One room in a shared home. A different document from a tenancy agreement, not a
    shortened one.

    What it states, and why: the facts of the SPACE. A person taking a room needs to
    know exactly which room is theirs, what furniture comes with it, which areas they
    share and with whom — including whether the landlord themselves is one of the
    people they'll share the kitchen with, because that single fact determines whether
    the provincial tenancy act applies to them at all.

    What it deliberately does NOT do is import the obligations of a full tenancy
    agreement. That restraint is intentional and load-bearing: a room in someone's
    house is not a self-contained unit, and pretending otherwise on paper creates
    duties nobody agreed to. Everything below is a fact about the space or a term both
    parties actually set.
    """

    id = "GENERIC_ROOMMATE"
    name = "Standard Roommate Agreement"
    subtitle = "One room in a shared home"
    legal_note = ""

    def sections(self, lease) -> list[Section]:
        from rentium.leases.tenancy_rules import rules_for_lease

        out: list[Section] = []
        rules = rules_for_lease(lease)

        slots = [lt for lt in lease.lease_tenants.all() if not lt.declined]
        room = None
        for lt in slots:
            if lt.room_id:
                room = lt.room
                break
        if room is None and lease.property_id:
            room = lease.property

        # 1. Parties
        contact = lease.get_effective_landlord_contact()
        out.append(
            Section(
                id="parties",
                title="1. Who this Agreement is Between",
                rows=[
                    Row("Landlord", lease.landlord.user.name),
                    *co_host_rows(lease),
                    Row("Contact", contact["daytime_phone"] or contact["email"] or "—"),
                    *tenant_rows(lease),
                ],
                clauses=self.clauses(lease, "nature"),
            )
        )

        # 2. Your room
        if room is not None:
            room_rows = [
                Row("Room", room.name),
                Row(
                    "Type",
                    "A private room — yours alone."
                    if room.room_type == "PRIVATE"
                    else "A shared room — you share this room itself with another person.",
                ),
                Row("Home", f"{room.address}, {room.city}"),
            ]
            if room.square_footage:
                room_rows.append(
                    Row("Approximate size", f"{room.square_footage} sq ft")
                )
            room_rows.append(
                Row(
                    "Furnished",
                    "Yes — see what's included below."
                    if room.is_furnished
                    else "No — the room comes unfurnished.",
                )
            )
            out.append(Section(id="your_room", title="2. Your Room", rows=room_rows))

            # 3. What's in your room — straight from inventory, no re-typing.
            summary = room.furnishing_summary()
            contents = (
                summary["sleeping"] + summary["furniture"] + summary["appliances"]
            )
            if contents:
                out.append(
                    Section(
                        id="room_contents",
                        title="3. What Comes With Your Room",
                        note=(
                            "Taken from the property's inventory. Condition is recorded "
                            "in the move-in inspection."
                        ),
                        rows=[Row("Included", ", ".join(contents), block=True)],
                    )
                )

        # 4. Shared areas — and, crucially, WHO you share them with.
        shared_rows = []
        if lease.group_id or (room and room.group_id):
            from rentium.properties.models import PropertyArea

            group_id = lease.group_id or room.group_id
            areas = (
                PropertyArea.objects.filter(property__group_id=group_id)
                .distinct()
                .order_by("area_type")
            )

            seen, names, landlord_shared = set(), [], []
            for area in areas:
                if area.area_type in seen:
                    continue
                seen.add(area.area_type)
                names.append(area.get_area_type_display())
                if area.shared_with_landlord:
                    landlord_shared.append(area.get_area_type_display())

            if names:
                shared_rows.append(Row("Shared areas", ", ".join(names), block=True))
            if landlord_shared:
                shared_rows.append(
                    Row(
                        "Shared with the landlord",
                        f"The landlord (or their relatives) also lives in this home and "
                        f"uses: {', '.join(landlord_shared)}.",
                        block=True,
                    )
                )

        if lease.get_common_space_clause_text():
            shared_rows.append(
                Row(
                    "Who else uses them",
                    lease.get_common_space_clause_text(),
                    block=True,
                )
            )

        group = lease.group or (room.group if room and room.group_id else None)
        if group:
            shared_items = [
                f"{i.name}{f' ×{i.quantity}' if i.quantity > 1 else ''}"
                for i in group.group_shared_inventory.all()
            ]
            if shared_items:
                shared_rows.append(
                    Row("Shared furnishings", ", ".join(shared_items), block=True)
                )

        if shared_rows:
            out.append(Section(id="shared", title="4. Shared Areas", rows=shared_rows))

        # 5. Money
        this_tenant = slots[0] if len(slots) == 1 else None
        money_rows = [
            Row("Rent", f"{money(lease.total_rent)} per month"),
            Row(
                "Payable on",
                f"The {ordinal(lease.rent_due_day or 1)} day of each month",
            ),
            Row("Pay by", f"e-Transfer to {lease.get_effective_etransfer_email()}"),
        ]
        if Decimal(str(lease.security_deposit or 0)) > 0:
            money_rows.append(Row("Security deposit", money(lease.security_deposit)))
            money_rows.append(
                Row(
                    "Deposit received on",
                    fmt_date(lease.security_deposit_received_date)
                    if lease.security_deposit_received_date
                    else "Not yet received",
                )
            )
        if this_tenant and Decimal(str(this_tenant.cleaning_fee or 0)) > 0:
            money_rows.append(Row("Cleaning fee", money(this_tenant.cleaning_fee)))
        if lease.get_bills_summary():
            money_rows.append(Row("Utilities", lease.get_bills_summary(), block=True))

        out.append(
            Section(
                id="money",
                title="5. Rent and Money",
                rows=money_rows,
                clauses=(
                    self.clauses(lease, "deposit_terms")
                    if Decimal(str(lease.security_deposit or 0)) > 0
                    else []
                ),
            )
        )

        # 6. Looking after the place
        out.append(
            Section(
                id="care",
                title="6. Looking After the Place",
                clauses=self.clauses(lease, "care"),
            )
        )

        # 7. House rules (only if the landlord actually set any)
        rules_rows = []
        if lease.house_rules:
            rules_rows.append(Row("Agreed rules", lease.house_rules, block=True))
        rules_rows.append(
            Row(
                "Pets",
                ("Allowed. " + (lease.pets_terms or "")).strip()
                if lease.pets_allowed
                else "Not permitted.",
                block=True,
            )
        )
        rules_rows.append(
            Row(
                "Smoking",
                ("Permitted. " + (lease.smoking_terms or "")).strip()
                if lease.smoking_allowed
                else "Not permitted anywhere in the home.",
                block=True,
            )
        )
        out.append(
            Section(
                id="house_rules",
                title="7. House Rules",
                rows=rules_rows,
                clauses=self.clauses(lease, "house_rules"),
            )
        )

        # 8. Ending it — read straight out of tenancy_rules, so this section can
        #    never disagree with what the move-out screen actually enforces.
        out.append(
            Section(
                id="ending",
                title="8. Ending this Agreement",
                rows=[
                    Row(
                        "Notice you must give",
                        f"{rules.tenant_notice_months} clear month(s), in writing.",
                    ),
                    Row(
                        "Notice the landlord must give",
                        f"{rules.landlord_notice_months} clear month(s), in writing."
                        if rules.landlord_notice_months
                        else "No statutory minimum applies to this arrangement.",
                    ),
                    Row("Why these periods apply", rules.summary, block=True),
                    Row(
                        "Ending early",
                        "Both parties may agree in writing to end on any date "
                        f"({rules.mutual_agreement_form}). Neither is obliged to agree.",
                        block=True,
                    ),
                ],
                clauses=self.clauses(lease, "ending_terms"),
            )
        )

        # 9. Anything else
        if lease.special_terms:
            out.append(
                Section(
                    id="additional_terms",
                    title="9. Anything Else Agreed",
                    rows=[Row("Additional terms", lease.special_terms, block=True)],
                )
            )

        # 10. Signatures
        out.append(
            Section(id="signatures", title="10. Signatures", rows=signature_rows(lease))
        )

        return out


# ----------------------------------------------------------------- registry
# ADD A JURISDICTION HERE. That is the entire integration surface.
REGISTRY: dict[str, LeaseFormat] = {
    "BC_RESIDENTIAL": BCResidentialFormat(),
    "SK_RESIDENTIAL": SKResidentialFormat(),
    "GENERIC_RESIDENTIAL": GenericResidentialFormat(),
    "GENERIC_ROOMMATE": RoommateFormat(),
    # Retired lease types, kept so historical leases still render. Both were room
    # agreements before the "one roommate agreement everywhere" change.
    "BC_ROOMMATE": RoommateFormat(),
    "SK_ROOMMATE": RoommateFormat(),
}

FALLBACK = GenericResidentialFormat()


def get_format(lease) -> LeaseFormat:
    return REGISTRY.get(lease.lease_type, FALLBACK)


def _canonical_json(data: dict) -> str:
    """Stable serialization for checksumming — key order can't change the hash."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def document_sha256(data: dict) -> str:
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()


def render_lease(lease) -> Document:
    """The one entry point. Screen, PDF, and API all call this.

    Once a lease is signed and activated, its TERMS are frozen: we return the
    document captured at activation (see capture_signed_document) so that later
    edits to clauses.py can never retroactively change what a tenant signed.
    Only the signatures section is re-rendered live, because who has signed is
    current status, not a term of the agreement (late joint-and-several signers
    must still appear). Drafts render live.
    """
    snapshot = getattr(lease, "signed_document", None)
    if snapshot:
        doc = Document.from_dict(snapshot)
        live_signatures = signature_rows(lease)
        for section in doc.sections:
            if section.id == "signatures":
                section.rows = live_signatures
        return doc
    return get_format(lease).render(lease)


def capture_signed_document(lease) -> bool:
    """Freeze the rendered agreement on the Lease at activation.

    Idempotent: only captures the first time (a signed document is immutable —
    re-capturing would defeat the point). Returns True if it captured now.
    Stores the full rendered Document plus a SHA-256 of it for tamper-evidence.
    """
    if getattr(lease, "signed_document", None):
        return False
    data = get_format(lease).render(lease).as_dict()
    lease.signed_document = data
    lease.signed_document_sha256 = document_sha256(data)
    lease.save(
        update_fields=["signed_document", "signed_document_sha256", "updated_at"]
    )
    return True
