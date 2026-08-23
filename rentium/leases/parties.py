"""Canonical lease parties and signature state.

The owner, co-landlords, and tenants used to be assembled independently by the
agreement renderer, form binder, REST serializer, and RAMA.  That allowed the
PDF to name a co-landlord while RAMA's tenant-only roster said that person was
not on the lease.  This projection is now the one domain-owned answer to "who
is a party and who has signed?".
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from rentium.core.evidence import Coverage
from rentium.core.evidence import EvidenceGap
from rentium.core.evidence import Fact
from rentium.core.evidence import FactPacket
from rentium.core.evidence import FactSource


@dataclass(frozen=True)
class LeaseSigningParty:
    id: str
    role: str
    role_label: str
    name: str
    email: str
    phone: str
    expected_to_sign: bool
    has_signed: bool
    signed_at: str | None
    required_individually_for_activation: bool
    activation_requirement: str
    blocks_activation_now: bool
    is_primary_tenant: bool = False
    invite_lifecycle: dict[str, Any] | None = None
    source_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _source(model: str, record_id, field: str) -> FactSource:
    return FactSource(model=model, record_id=str(record_id), field=field)


def lease_signing_parties(lease) -> list[LeaseSigningParty]:
    """Return every non-cancelled person named as a signer on ``lease``.

    Activation currently requires the owner, every co-landlord, and at least
    one tenant.  Every named tenant is nevertheless an expected signer.  Those
    two statements are represented separately so clients cannot turn the group
    rule into the false claim that an individual unsigned tenant is irrelevant.
    """
    from rentium.leases.services import invite_lifecycle

    owner_user = lease.landlord.user
    parties = [
        LeaseSigningParty(
            id=str(lease.landlord_id),
            role="LANDLORD",
            role_label="Landlord",
            name=(owner_user.name or owner_user.email or "").strip(),
            email=(owner_user.email or "").strip(),
            phone=(getattr(owner_user, "phone", "") or "").strip(),
            expected_to_sign=True,
            has_signed=bool(lease.landlord_signed),
            signed_at=_iso(lease.landlord_signed_date),
            required_individually_for_activation=True,
            activation_requirement="ALL_NAMED_LANDLORDS",
            blocks_activation_now=not bool(lease.landlord_signed),
            source_model="leases.Lease.landlord",
        ),
    ]

    signatories = lease.landlord_signatories.select_related("member").order_by(
        "created_at", "id",
    )
    for signatory in signatories:
        member = signatory.member
        parties.append(
            LeaseSigningParty(
                id=str(signatory.pk),
                role="CO_LANDLORD",
                role_label="Co-landlord",
                name=(signatory.display_name or "").strip(),
                email=(
                    signatory.email
                    or (member.email if member is not None else "")
                    or ""
                ).strip(),
                phone=(
                    signatory.phone
                    or (getattr(member, "phone", "") if member is not None else "")
                    or ""
                ).strip(),
                expected_to_sign=True,
                has_signed=bool(signatory.has_signed),
                signed_at=_iso(signatory.signed_date),
                required_individually_for_activation=True,
                activation_requirement="ALL_NAMED_LANDLORDS",
                blocks_activation_now=not bool(signatory.has_signed),
                source_model="leases.LeaseLandlordSignatory",
            ),
        )

    tenants = (
        lease.lease_tenants.filter(declined=False)
        .select_related("tenant__user")
        .order_by("created_at", "id")
    )
    for slot in tenants:
        user = slot.tenant.user if slot.tenant_id else None
        parties.append(
            LeaseSigningParty(
                id=str(slot.pk),
                role="TENANT",
                role_label=("Primary tenant" if slot.is_primary_tenant else "Tenant"),
                name=(slot.display_name or "").strip(),
                email=(
                    slot.invited_email or (user.email if user else "") or ""
                ).strip(),
                phone=(
                    slot.invited_phone
                    or (getattr(user, "phone", "") if user else "")
                    or ""
                ).strip(),
                expected_to_sign=True,
                has_signed=bool(slot.has_signed),
                signed_at=_iso(slot.signed_date),
                required_individually_for_activation=False,
                activation_requirement="AT_LEAST_ONE_TENANT",
                # No one particular tenant is the group blocker.  The summary
                # below reports the missing tenant-signature requirement.
                blocks_activation_now=False,
                is_primary_tenant=bool(slot.is_primary_tenant),
                invite_lifecycle=invite_lifecycle(slot),
                source_model="leases.LeaseTenant",
            ),
        )
    return parties


def lease_signing_roster(lease) -> dict[str, Any]:
    parties = lease_signing_parties(lease)
    landlord_parties = [p for p in parties if p.role != "TENANT"]
    tenant_parties = [p for p in parties if p.role == "TENANT"]
    any_tenant_signed = any(p.has_signed for p in tenant_parties)
    blockers = [
        {
            "kind": "party_signature",
            "party_id": p.id,
            "role": p.role,
            "name": p.name,
        }
        for p in landlord_parties
        if p.blocks_activation_now
    ]
    if tenant_parties and not any_tenant_signed:
        blockers.append(
            {
                "kind": "tenant_signature_requirement",
                "requirement": "AT_LEAST_ONE_TENANT",
                "eligible_party_ids": [p.id for p in tenant_parties],
            },
        )

    return {
        "lease_id": str(lease.pk),
        "lease_number": lease.lease_number or "",
        "lease_status": lease.status,
        "parties": [party.to_dict() for party in parties],
        "landlords": [party.to_dict() for party in landlord_parties],
        "tenants": [party.to_dict() for party in tenant_parties],
        "expected_signer_count": len(parties),
        "signed_count": sum(1 for party in parties if party.has_signed),
        "unsigned_expected_signers": [
            party.to_dict() for party in parties if not party.has_signed
        ],
        "activation_requirements": {
            "all_named_landlords_signed": all(
                party.has_signed for party in landlord_parties
            ),
            "at_least_one_tenant_signed": any_tenant_signed,
            "attached_blocking_forms_complete": None,
        },
        "signature_activation_blockers": blockers,
        "signature_requirements_satisfied": not blockers,
        "policy_note": (
            "Every named party is expected to sign. Activation requires the owner, "
            "every co-landlord, and at least one tenant; unsigned named tenants may "
            "still sign after activation under the current product policy."
        ),
    }


def lease_signing_fact_packet(lease) -> FactPacket:
    roster = lease_signing_roster(lease)
    missing: list[EvidenceGap] = []
    facts: list[Fact] = []
    for index, party in enumerate(roster["parties"]):
        model = party["source_model"]
        record_id = party["id"]
        prefix = f"parties.{index}"
        facts.extend(
            Fact(
                key=f"{prefix}.{field_name}",
                value=party[field_name],
                value_type=type(party[field_name]).__name__,
                source=_source(model, record_id, field_name),
            )
            for field_name in (
                "role",
                "role_label",
                "name",
                "email",
                "expected_to_sign",
                "has_signed",
                "signed_at",
                "required_individually_for_activation",
                "activation_requirement",
                "blocks_activation_now",
                "is_primary_tenant",
            )
        )
        if not party["name"]:
            missing.append(
                EvidenceGap(
                    key=f"{prefix}.name",
                    reason="The signer record has no display or legal name.",
                    source=_source(model, record_id, "name"),
                ),
            )

    coverage = Coverage.PARTIAL if missing else Coverage.COMPLETE
    allowed: list[str] = []
    if coverage == Coverage.COMPLETE:
        if not roster["unsigned_expected_signers"]:
            allowed.append("no_unsigned_expected_signers")
        if not roster["landlords"][1:]:
            allowed.append("no_co_landlords")

    return FactPacket(
        subject={
            "type": "lease",
            "id": str(lease.pk),
            "label": lease.lease_number or str(lease.pk),
        },
        facet="lease.signing_roster",
        as_of=timezone.now().isoformat(),
        coverage=coverage,
        facts=tuple(facts),
        missing=tuple(missing),
        allowed_negative_conclusions=tuple(allowed),
        metadata=roster,
    )
