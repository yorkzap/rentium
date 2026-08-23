from __future__ import annotations

import hashlib

import pytest
from django.db import IntegrityError
from django.db import transaction

from rentium.core.evidence import Coverage
from rentium.core.evidence import EvidenceGap
from rentium.core.evidence import FactPacket
from rentium.rama.evals import passes_certification_gates
from rentium.rama.evals import redact_text
from rentium.rama.knowledge import retrieve
from rentium.rama.knowledge import store_source
from rentium.rama.knowledge import validate_official_url
from rentium.rama.models import RamaKnowledgeSource


def test_partial_packet_cannot_authorize_negative_claim():
    with pytest.raises(ValueError, match="Negative conclusions"):
        FactPacket(
            subject={"type": "lease", "id": "x"},
            facet="lease.signing_roster",
            as_of="2026-08-22T00:00:00Z",
            coverage=Coverage.PARTIAL,
            missing=(EvidenceGap(key="parties", reason="unavailable"),),
            allowed_negative_conclusions=("no_co_landlords",),
        )


def test_redaction_removes_direct_identifiers():
    value = redact_text(
        "Raj Singh emailed raj@example.com about RMT123-ABC at +1 250-555-1234",
        known_values=["Raj Singh"],
    )
    assert "Raj Singh" not in value
    assert "raj@example.com" not in value
    assert "RMT123-ABC" not in value
    assert "250-555-1234" not in value
    assert "[ENTITY_1]" in value
    assert "[EMAIL]" in value
    assert "[LEASE_ID]" in value
    assert "[PHONE]" in value


def test_certification_gates_are_fail_closed():
    passing = {
        "attempts_complete": True,
        "attempts_per_case": 5,
        "critical_accuracy": 1.0,
        "unsupported_claims": 0,
        "overall_accuracy": 0.98,
        "retrieval_recall_at_5": 0.98,
    }
    assert passes_certification_gates(passing) is True
    assert passes_certification_gates({**passing, "critical_accuracy": 0.99}) is False
    assert passes_certification_gates({**passing, "unsupported_claims": 1}) is False


def test_official_source_allowlist_rejects_arbitrary_websites():
    validate_official_url("https://www2.gov.bc.ca/gov/content/housing-tenancy")
    with pytest.raises(ValueError, match="not an approved official authority"):
        validate_official_url("https://example.com/blog/tenancy-law")


@pytest.mark.django_db
def test_hybrid_store_keeps_citations_and_never_claims_complete(landlord):
    content = "A landlord must give a tenant a copy of the tenancy agreement."
    source, created = store_source(
        kind="OFFICIAL",
        title="BC tenancy agreement guidance",
        authority="Government of British Columbia",
        canonical_url="https://www2.gov.bc.ca/gov/content/housing-tenancy",
        jurisdiction="BC",
        version="2026-08",
        sha256=hashlib.sha256(content.encode()).hexdigest(),
        chunks=[
            {
                "heading": "Copy of agreement",
                "context": "Residential tenancy agreements in British Columbia",
                "content": content,
                "citation_label": "BC tenancy agreement guidance",
            },
        ],
    )

    result = retrieve(
        landlord=landlord,
        query="copy of the tenancy agreement",
        jurisdiction="BC",
        official_only=True,
    )

    assert created is True
    assert source.landlord_id is None
    assert result["coverage"] == "PARTIAL"
    assert result["negative_conclusions_allowed"] is False
    assert result["results"][0]["authority"] == "Government of British Columbia"
    assert result["results"][0]["url"].startswith("https://www2.gov.bc.ca/")


@pytest.mark.django_db
def test_knowledge_scope_is_enforced_by_the_database(landlord):
    with pytest.raises(IntegrityError), transaction.atomic():
        RamaKnowledgeSource.objects.create(
            kind=RamaKnowledgeSource.Kind.PRIVATE,
            landlord=None,
            title="Unscoped private document",
            sha256="a" * 64,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        RamaKnowledgeSource.objects.create(
            kind=RamaKnowledgeSource.Kind.OFFICIAL,
            landlord=landlord,
            title="Tenant-scoped official source",
            sha256="b" * 64,
        )
