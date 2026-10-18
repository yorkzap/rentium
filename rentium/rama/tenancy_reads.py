"""Bounded tenancy reads with authoritative facts and episode-scoped subjects."""

import re

from django.core.exceptions import ValidationError

from rentium.leases.tenancy_summary import render_tenancy_answer, tenancy_fact_packet

_WRITE = re.compile(
    r"^\s*(?:please\s+)?(?:create|add|record|accept|cancel|decline|send|set|change|terminate|refund|pay|return|schedule|prepare|switch|rent out)\b",
    re.I,
)
_NOTICE = re.compile(
    r"\b(?:move[ -]?out notice|tenant notice|notice to end|sent (?:a |the |this )?(?:move[ -]?out )?notice|gave notice|given notice)\b",
    re.I,
)
_OCCUPANCY = re.compile(
    r"\b(?:rented (?:out|now|today)|occupied|vacant|living there|moved out|still there|available (?:now|today|from|after)|when .* (?:leave|leaving|move out))\b",
    re.I,
)
_DEPOSITS = re.compile(
    r"\b(?:deposit.*(?:deadline|held|return|clock|settled)|15.day clock|forwarding address)\b",
    re.I,
)
_LEGAL = re.compile(r"\b(?:rta|tenancy act|exempt|legal classification)\b", re.I)


def tenancy_facet(message):
    if _WRITE.search(message or ""):
        return None
    for pattern, facet in (
        (_NOTICE, "notice"),
        (_DEPOSITS, "deposits"),
        (_LEGAL, "legal"),
        (_OCCUPANCY, "occupancy"),
    ):
        if pattern.search(message or ""):
            return "tenancy." + facet
    return None


def resolve_tenancy(landlord, message, reference):
    from rentium.leases.models import Lease
    from .orchestrator import _LEASE_NUMBER, _reference_lease_id

    leases = Lease.objects.filter(landlord=landlord).select_related(
        "property", "group", "landlord__user"
    )
    exact = _LEASE_NUMBER.search(message or "")
    if exact:
        return leases.filter(lease_number__iexact=exact.group()).first(), []
    text = (message or "").casefold()
    # An explicitly named place takes priority over prior conversation context.
    named = []
    for lease in leases:
        place = lease.property if lease.property_id else lease.group
        if not place or not place.name or place.name.casefold() not in text:
            continue
        address = lease.property.address if lease.property_id else ""
        address_words = set(
            re.findall(r"[a-z]{4,}|\d+", (address or "").casefold())
        ) - {"street", "avenue", "road"}
        score = sum(word in text for word in address_words)
        named.append((score, lease))
    if named:
        best = max(score for score, _ in named)
        candidates = [lease for score, lease in named if score == best]
        current = [
            lease for lease in candidates if lease.status in {"PENDING", "ACTIVE"}
        ]
        candidates = current or candidates
        if len(candidates) == 1:
            return candidates[0], []
        return None, [
            {
                "id": str(lease.pk),
                "label": lease.lease_number,
                "place": lease.property.address
                if lease.property_id
                else lease.group.name,
            }
            for lease in candidates
        ]
    from rentium.properties.models import Property
    from rentium.leases.space_services import leases_for_unit, space_scope

    named_properties = [
        prop
        for prop in Property.objects.filter(landlord=landlord).select_related("unit")
        if prop.name and prop.name.casefold() in text
    ]
    if named_properties:
        candidates = {}
        for prop in named_properties:
            relevant = (
                leases_for_unit(prop.unit)
                if prop.unit_id
                else leases.filter(property=prop)
            )
            # Query the physical map explicitly rather than guessing room names.
            from rentium.properties.models import PropertyArea

            bedroom_ids = {
                str(pk)
                for pk in PropertyArea.objects.filter(
                    exclusive_to=prop, is_seeded_default=False, area_type="BEDROOM"
                ).values_list("pk", flat=True)
            }
            for row in relevant.filter(status__in=["PENDING", "ACTIVE"]):
                scope = space_scope(row)
                if (
                    row.property_id == prop.pk
                    or scope["whole_unit"]
                    or bedroom_ids & set(scope["room_ids"])
                ):
                    candidates[str(row.pk)] = row
        if len(candidates) == 1:
            return next(iter(candidates.values())), []
        return None, [
            {
                "id": str(row.pk),
                "label": row.lease_number,
                "place": row.property.name if row.property_id else row.group.name,
            }
            for row in candidates.values()
        ]
    # Do not silently answer about the previous tenancy when a newly named
    # place could not be resolved.
    explicit_place = re.search(
        r"\b(?:room|suite|unit|property|house|floor|street|avenue)\b", text
    )
    generic_place = re.search(
        r"\b(?:this|that|the current) (?:room|suite|unit|property|house|floor)\b", text
    )
    if explicit_place and not generic_place:
        return None, []
    reference = reference or {}
    context = (
        reference.get("client_context")
        or (reference.get("semantic_payload") or {}).get("client_context")
        or {}
    )
    lease_id = context.get("lease_id")
    if lease_id:
        try:
            return leases.filter(pk=lease_id).first(), []
        except (ValueError, TypeError, ValidationError):
            return None, []
    property_id = context.get("property_id")
    if property_id:
        try:
            candidates = list(
                leases.filter(property_id=property_id, status__in=["PENDING", "ACTIVE"])
            )
        except (ValueError, TypeError, ValidationError):
            return None, []
        if len(candidates) == 1:
            return candidates[0], []
        return None, [
            {
                "id": str(row.pk),
                "label": row.lease_number,
                "place": row.property.name if row.property_id else row.group.name,
            }
            for row in candidates
        ]
    unit_id = context.get("unit_id")
    if unit_id:
        from rentium.properties.models import PropertyUnit
        from rentium.leases.space_services import leases_for_unit

        try:
            unit = PropertyUnit.objects.filter(pk=unit_id, landlord=landlord).first()
            candidates = (
                list(leases_for_unit(unit).filter(status__in=["PENDING", "ACTIVE"]))
                if unit
                else []
            )
        except (ValueError, TypeError, ValidationError):
            candidates = []
        if len(candidates) == 1:
            return candidates[0], []
        return None, [
            {"id": str(row.pk), "label": row.lease_number, "place": unit.name}
            for row in candidates
        ]
    ref = _reference_lease_id(reference)
    if ref:
        try:
            return leases.filter(pk=ref).first(), []
        except (ValueError, TypeError, ValidationError):
            return None, []
    return None, []


def prepare_tenancy_read(landlord, message, reference, facet):
    from .orchestrator import AnswerEnvelope, IntentEnvelope
    from .links import url_for_path

    intent = IntentEnvelope(
        name=facet, domain_lane="leases", operation="read", requested_facet=facet
    )
    lease, choices = resolve_tenancy(landlord, message, reference)
    if lease is None:
        reply = "Which tenancy do you mean? " + (
            "Choose a lease: "
            + "; ".join(f"{row['label']} ({row['place']})" for row in choices)
            if choices
            else "Give the property name and address, or lease number."
        )
        return AnswerEnvelope(
            reply=reply,
            answer_mode="ABSTAINED",
            coverage="PARTIAL",
            evidence={"choices": choices, "facet": facet},
            intent=intent,
        )
    packet = tenancy_fact_packet(lease, facet=facet)
    summary = packet.metadata
    sources = [
        {"label": row["label"], "url": url_for_path(row["path"])}
        for row in summary["links"]
    ]
    return AnswerEnvelope(
        reply=render_tenancy_answer(summary, facet),
        answer_mode="CERTIFIED" if packet.coverage.value == "COMPLETE" else "ABSTAINED",
        coverage=packet.coverage.value,
        evidence=packet.to_dict(),
        subject=packet.subject,
        intent=intent,
        details=summary,
        sources=sources,
        warnings=summary["warnings"] + summary["conflicts"],
    )
