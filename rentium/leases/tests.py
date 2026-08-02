"""Lease document rendering + signed-document immutability tests.

The document layer is a single source of truth (documents.render_lease); these
tests lock in (1) that the BC full-unit agreement carries the official RTB-1
standard terms with the draft banner off, (2) that the roommate agreement keeps
its landlord-protective house terms, and (3) that a signed lease's rendered
document is frozen at activation and cannot be changed by later clause edits.
"""

from __future__ import annotations

from datetime import date

import pytest

from rentium.leases.documents import render_lease


def _clause_text(doc) -> str:
    return "\n".join(c for s in doc.sections for c in s.clauses)


# --------------------------------------------------------------- BC full unit
@pytest.mark.django_db
def test_bc_full_unit_carries_official_rtb1_terms(bc_lease):
    doc = render_lease(bc_lease)

    # Banner is off — this presents as the real form.
    assert doc.official_text_loaded is True

    titles = [s.title for s in doc.sections]
    for expected in (
        "8. Occupants and Guests",
        "11. Service of Documents",
        "13. Application of the Residential Tenancy Act",
        "14. Signatures",
    ):
        assert expected in titles, f"missing section: {expected}"

    text = _clause_text(doc)
    # A few verbatim RTB-1 fingerprints across different sections.
    for fingerprint in (
        "10 Day Notice to End Tenancy",              # §7 payment of rent
        "three whole months notice",                  # §8 rent increase
        "one half of the monthly rent",               # §4 deposits
        "Guide Dog and Service Dog Act",              # §5 pets
        "between 8 a.m. and 9 p.m.",                   # §13 entry
        "1 p.m. on the day the tenancy ends",         # §14 ending
        "responsible for monitoring the email",       # §16 service of documents
    ):
        assert fingerprint in text, f"missing RTB-1 text: {fingerprint!r}"


# --------------------------------------------------------------- roommate
@pytest.fixture
def roommate_lease(landlord):
    from rentium.leases.models import Lease
    from rentium.properties.models import Property

    room = Property.objects.create(
        landlord=landlord,
        name="McKenzie Room A",
        address="950 McKenzie Ave",
        city="Victoria",
        province="BC",
        property_category=Property.PropertyCategory.ROOM,
        room_type=Property.RoomType.PRIVATE,
    )
    return Lease.objects.create(
        landlord=landlord,
        property=room,
        lease_type=Lease.LeaseType.GENERIC_ROOMMATE,
        status=Lease.LeaseStatus.DRAFT,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="800.00",
        security_deposit="400.00",
        cleaning_deposit="200.00",
    )


@pytest.mark.django_db
def test_roommate_has_landlord_protective_terms(roommate_lease):
    doc = render_lease(roommate_lease)
    text = _clause_text(doc)

    # Protective house terms the strengthened roommate agreement must carry.
    assert "no smoking and no vaping" in text
    assert "longer than one week" in text            # guest limit
    assert "sublet" in text.lower()                  # no subletting
    assert "deduct from the deposit" in text         # deposit-deduction grounds
    assert "one clear month" in text                 # notice to end
    assert "not a cleaning fee" in text
    assert "return the security deposit and any cleaning deposit separately" in text

    money = next(section for section in doc.sections if section.id == "money")
    amounts = {row.label: row.value for row in money.rows}
    assert amounts["Cleaning deposit"] == "$200.00"

    # It must NOT masquerade as a statutory tenancy form (s.4(c) restraint).
    assert doc.format_id == "GENERIC_ROOMMATE"
    assert "Residential Tenancy Act" not in text


@pytest.mark.django_db
def test_roommate_cleaning_deposit_is_a_liability_not_fee_income(roommate_lease):
    from rentium.ledger.billing import generate_initial_charges
    from rentium.ledger.models import EntryType, LedgerEntry

    generate_initial_charges(roommate_lease)

    cleaning = LedgerEntry.objects.get(
        lease=roommate_lease,
        metadata__kind="cleaning_deposit_lease",
    )
    assert cleaning.entry_type == EntryType.DEPOSIT_CHARGE
    assert not LedgerEntry.objects.filter(
        lease=roommate_lease,
        entry_type=EntryType.FEE_CHARGE,
        description__icontains="cleaning",
    ).exists()

# --------------------------------------------------------- immutability
@pytest.mark.django_db
def test_signed_document_is_frozen_against_clause_edits(bc_lease, monkeypatch):
    """The core guarantee: once captured, editing clauses.py can never change a
    signed lease's rendered document."""
    from rentium.leases import clauses, documents

    # Freeze the agreement (this is what check_and_activate does at activation).
    assert documents.capture_signed_document(bc_lease) is True
    assert bc_lease.signed_document
    assert bc_lease.signed_document_sha256 == documents.document_sha256(
        bc_lease.signed_document
    )
    frozen_text = _clause_text(documents.render_lease(bc_lease))

    # The clause library legitimately changes (e.g. for NEW leases).
    monkeypatch.setitem(
        clauses.CLAUSES, ("BC_RESIDENTIAL", "ending"), ["SENTINEL-EDITED-CLAUSE"]
    )
    # A fresh live render reflects the edit...
    live = _clause_text(documents.get_format(bc_lease).render(bc_lease))
    assert "SENTINEL-EDITED-CLAUSE" in live
    # ...but the SIGNED lease still renders exactly what was signed.
    still = _clause_text(documents.render_lease(bc_lease))
    assert "SENTINEL-EDITED-CLAUSE" not in still
    assert still == frozen_text

    # Idempotent: a signed document is never re-captured.
    assert documents.capture_signed_document(bc_lease) is False


@pytest.mark.django_db
def test_signatures_render_live_on_a_frozen_lease(bc_lease):
    """Terms are frozen, but signature status is current — a late joint-and-
    several signer must still show up on the downloaded document."""
    from rentium.leases import documents

    documents.capture_signed_document(bc_lease)
    doc = documents.render_lease(bc_lease)
    sig = next(s for s in doc.sections if s.id == "signatures")
    assert sig.rows  # rendered from current state, not the frozen JSON blob
    assert any("Landlord" in r.label for r in sig.rows)


@pytest.mark.django_db
def test_activation_captures_the_signed_document(bc_property, landlord):
    """check_and_activate must freeze the document as a side effect of the lease
    going ACTIVE."""
    from rentium.leases.models import Lease
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    lease = Lease.objects.create(
        landlord=landlord,
        property=bc_property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.PENDING_SIGNATURES,
        start_date=date.today(),
        is_month_to_month=True,
        total_rent="850.00",
    )
    tenant = TenantProfile.objects.create(user=UserFactory())
    lt = lease.lease_tenants.create(tenant=tenant, rent_amount="850.00")
    lt.has_signed = True
    lt.signed_date = date.today()
    lt.save()
    lease.landlord_signed = True
    lease.landlord_signed_date = date.today()
    lease.save()

    assert lease.check_and_activate() is True
    lease.refresh_from_db()
    assert lease.status == Lease.LeaseStatus.ACTIVE
    assert lease.signed_document, "activation must capture the signed document"
    assert len(lease.signed_document_sha256) == 64
