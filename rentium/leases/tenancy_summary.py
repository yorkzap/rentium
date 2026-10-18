"""One operational tenancy read for the dashboard and RAMA.

Presence, paperwork, termination and availability are separate facts. No
document search, listing badge or tenant-signature subset can substitute.
"""

from datetime import timedelta

from django.utils import timezone

from rentium.core.evidence import Coverage, EvidenceGap, Fact, FactPacket, FactSource

from .parties import lease_signing_roster
from .space_services import occupancy_state, scheduled_end, space_scope


def assessment_summary(lease):
    from .tenancy_rules import _jurisdiction

    assessment = lease.tenancy_assessments.first()
    if assessment:
        return {
            "id": str(assessment.pk),
            "jurisdiction": assessment.jurisdiction,
            "decision": assessment.decision,
            "facts": assessment.facts,
            "rule_version": assessment.rule_version,
            "reason": assessment.reason,
        }
    return {
        "jurisdiction": _jurisdiction(lease),
        "decision": "REVIEW",
        "reason": "Tenancy-start owner kitchen/bathroom sharing has not been explicitly reviewed.",
        "facts": {},
    }


def tenancy_summary(lease, *, as_of=None, include_deposits=True):
    day = as_of or timezone.localdate()
    roster = lease_signing_roster(lease)
    notices = list(lease.moveout_requests.order_by("-created_at", "id"))
    accepted = [notice for notice in notices if notice.status == "ACCEPTED"]
    end_dates = {
        notice.effective_end_date for notice in accepted if notice.effective_end_date
    }
    conflicts = []
    if len(end_dates) > 1:
        conflicts.append(
            "Accepted notices have different end dates; review them before promising availability."
        )
    ending = scheduled_end(lease)
    presence = occupancy_state(lease, as_of=day)
    scope = space_scope(lease)
    assessment = assessment_summary(lease)
    unsigned = [
        {"name": party["name"], "role": party["role_label"]}
        for party in roster["unsigned_expected_signers"]
    ]
    rows = [
        {
            "id": str(notice.pk),
            "kind": notice.kind,
            "label": notice.get_kind_display(),
            "status": notice.status,
            "requested_end_date": notice.requested_end_date.isoformat(),
            "effective_end_date": notice.effective_end_date.isoformat()
            if notice.effective_end_date
            else None,
            "rent_handling": notice.rent_handling,
            "rent_handling_label": notice.get_rent_handling_display(),
            "deposit_status": notice.deposit_status(),
        }
        for notice in notices
    ]
    if ending and ending < day and presence == "RECORDED_OCCUPIED":
        conflicts.append(
            "The scheduled tenancy end has passed but recorded occupancy remains open."
        )
    summary = {
        "lease_id": str(lease.pk),
        "lease_number": lease.lease_number,
        "as_of": day.isoformat(),
        "agreement_status": lease.status,
        "scope": scope,
        "occupancy": presence,
        "has_commitment": lease.status in {"PENDING", "ACTIVE"},
        "term_start": lease.start_date.isoformat(),
        "term_end": lease.end_date.isoformat() if lease.end_date else None,
        "expected_move_in": (lease.move_in_date or lease.start_date).isoformat(),
        "scheduled_end": ending.isoformat() if ending else None,
        "earliest_next_start": (ending + timedelta(days=1)).isoformat()
        if ending and not conflicts
        else None,
        "availability_confirmed": presence == "RECORDED_DEPARTED" and not conflicts,
        "unsigned": unsigned,
        "signing": roster,
        "notices": rows,
        "notice_coverage": "COMPLETE",
        "assessment": assessment,
        "conflicts": conflicts,
        "warnings": (
            []
            if presence != "UNCONFIRMED"
            else ["Actual occupancy has not been confirmed in the occupancy log."]
        )
        + (
            []
            if scope["coverage"] == "COMPLETE"
            else ["The physical spaces covered by this lease need review."]
        ),
        "links": [{"label": "View tenancy", "path": f"/dashboard/leases/{lease.pk}"}],
    }
    if include_deposits:
        from rentium.ledger.services import deposit_position

        summary["deposits"] = deposit_position(lease.landlord, lease=lease)
    return summary


def tenancy_fact_packet(lease, *, facet="tenancy.status", as_of=None):
    summary = tenancy_summary(lease, as_of=as_of)
    gaps = []
    if facet == "tenancy.occupancy" and summary["occupancy"] == "UNCONFIRMED":
        gaps.append(
            EvidenceGap(
                key="occupancy",
                reason="No confirmed current occupancy or departure record.",
            )
        )
    if facet == "tenancy.legal" and summary["assessment"]["decision"] == "REVIEW":
        gaps.append(
            EvidenceGap(key="assessment", reason=summary["assessment"]["reason"])
        )
    if summary["conflicts"]:
        gaps.extend(
            EvidenceGap(key="conflict", reason=value) for value in summary["conflicts"]
        )
    facts = [
        Fact(
            key="agreement_status",
            value=lease.status,
            source=FactSource(
                model="leases.Lease", record_id=str(lease.pk), field="status"
            ),
        )
    ]
    facts.append(
        Fact(
            key="signing",
            value=summary["signing"],
            value_type="object",
            source=FactSource(
                model="leases.Lease", record_id=str(lease.pk), field="signing_roster"
            ),
        )
    )
    facts.append(
        Fact(
            key="space_scope",
            value=summary["scope"],
            value_type="object",
            source=FactSource(
                model="leases.LeaseSpaceScope"
                if summary["scope"].get("source_id")
                else "leases.Lease",
                record_id=summary["scope"].get("source_id") or str(lease.pk),
                field="whole_unit,room_ids",
            ),
        )
    )
    if summary["assessment"].get("id"):
        facts.append(
            Fact(
                key="assessment",
                value=summary["assessment"],
                value_type="object",
                source=FactSource(
                    model="leases.TenancyAssessment",
                    record_id=summary["assessment"]["id"],
                    field="decision,facts",
                ),
            )
        )
    for event in lease.presence_events.filter(
        occurred_on__lte=as_of or timezone.localdate()
    ):
        facts.append(
            Fact(
                key=f"presence.{event.pk}",
                value={
                    "state": event.state,
                    "occurred_on": str(event.occurred_on),
                    "reason": event.reason,
                },
                value_type="object",
                source=FactSource(
                    model="leases.TenancyPresence",
                    record_id=str(event.pk),
                    field="state,occurred_on,reason",
                ),
            )
        )
    for record in lease.occupancies.all():
        facts.append(
            Fact(
                key=f"occupancy.{record.pk}",
                value={
                    "move_in": str(record.move_in),
                    "move_out": str(record.move_out) if record.move_out else None,
                },
                value_type="object",
                source=FactSource(
                    model="leases.Occupancy",
                    record_id=str(record.pk),
                    field="move_in,move_out",
                ),
            )
        )
    facts.extend(
        Fact(
            key=f"notices.{row['id']}",
            value=row,
            source=FactSource(
                model="leases.MoveOutRequest",
                record_id=row["id"],
                field="status,effective_end_date,rent_handling",
            ),
            value_type="object",
        )
        for row in summary["notices"]
    )
    return FactPacket(
        subject={"type": "lease", "id": str(lease.pk), "label": lease.lease_number},
        facet=facet,
        as_of=timezone.now().isoformat(),
        coverage=Coverage.PARTIAL if gaps else Coverage.COMPLETE,
        facts=tuple(facts),
        missing=tuple(gaps),
        metadata=summary,
        allowed_negative_conclusions=("no_recorded_notice",)
        if not summary["notices"] and not gaps
        else (),
    )


def render_tenancy_answer(summary, facet):
    notices = [row for row in summary["notices"] if row["status"] == "ACCEPTED"]
    end = summary["scheduled_end"]
    if facet == "tenancy.notice":
        if notices:
            answer = f"Yes—{notices[0]['label'].lower()} is accepted, with tenancy ending {end or 'on a date needing review'}. That schedules the end; it doesn't confirm they have moved out."
        elif summary["notices"]:
            answer = "There is a recorded notice, but it is not accepted. " + "; ".join(
                f"{row['label']}: {row['status'].lower()}" for row in summary["notices"]
            )
        else:
            answer = "No notice is recorded in this tenancy's notice history. This does not establish whether a notice was delivered outside Rentium."
    elif facet == "tenancy.deposits":
        answer = f"Recorded deposits held: ${summary['deposits']['deposit_held']}. "
        answer += (
            summary["deposits"].get("clock_note")
            or "Review the deposit settlement details before taking action."
        )
    elif facet == "tenancy.legal":
        assessment = summary["assessment"]
        answer = {
            "APPLIES": "The recorded assessment is that the tenancy act applies.",
            "EXEMPT_OWNER_SHARED": "The recorded assessment is the BC owner-shared kitchen/bathroom exemption.",
            "REVIEW": "I can't verify the legal classification yet.",
        }[assessment["decision"]]
        answer += " " + assessment["reason"]
    else:
        answer = {
            "RECORDED_OCCUPIED": "The occupancy log records tenants there now.",
            "RECORDED_DEPARTED": "The occupancy log records their departure.",
            "EXPECTED_FUTURE": f"Possession is expected from {summary['expected_move_in']}; it is not recorded as occupied now.",
            "UNCONFIRMED": "There is a lease commitment, but the records don't confirm actual occupancy today."
            if summary["has_commitment"]
            else "The records don't confirm actual occupancy today.",
        }[summary["occupancy"]]
        if notices:
            answer += f" An accepted notice schedules tenancy end for {end or 'a date needing review'}."
    if summary["unsigned"] and facet not in {"tenancy.deposits", "tenancy.legal"}:
        names = ", ".join(party["name"] for party in summary["unsigned"])
        answer += f" Signatures still pending: {names}."
    return answer
