"""Small V3 orchestration seam.

The legacy turn engine still owns its mature deterministic adapters.  New
surfaces enter through this typed envelope and an explicit sequence so routing,
resolution, policy, execution, verification, and rendering can evolve without
adding more persona-specific chat endpoints.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from dataclasses import field
from difflib import SequenceMatcher

from django.db.models import Prefetch

from rentium.core.evidence import Coverage


@dataclass(frozen=True)
class TurnEnvelope:
    message: str
    conversation_id: uuid.UUID
    message_id: uuid.UUID = field(default_factory=uuid.uuid4)
    target_role: str = "auto"
    reply_to_message_id: uuid.UUID | None = None
    attachment_batch_id: uuid.UUID | None = None
    client_context: dict = field(default_factory=dict)
    channel: str = "web"


@dataclass(frozen=True)
class RoutingDecision:
    role: str
    reason: str
    requires_deliberation: bool = False


@dataclass(frozen=True)
class IntentEnvelope:
    name: str
    domain_lane: str
    operation: str
    requested_facet: str
    entity_hint: str = ""


@dataclass(frozen=True)
class AnswerEnvelope:
    reply: str
    answer_mode: str
    coverage: str
    evidence: dict
    trace_id: uuid.UUID = field(default_factory=uuid.uuid4)
    subject: dict = field(default_factory=dict)
    intent: IntentEnvelope | None = None


_LEASE_NUMBER = re.compile(r"\bRMT[A-Z0-9-]+\b", re.IGNORECASE)
_SIGNING_QUESTION = re.compile(
    r"\b(?:sign(?:ed|er|ers|ing|ature|atures)?|unsigned|hasn['\u2019]?t signed|"
    r"have not signed|who (?:is|are|was) (?:on|party)|parties|party|"
    r"co[- ]?landlords?|landlords? on (?:that|the|this) lease|supposed to sign)\b",
    re.IGNORECASE,
)
_LEASE_IDENTITY_QUESTION = re.compile(
    r"\b(?:what|which|find|give|tell|show)\b[^?\n]{0,100}\blease (?:number|id)\b|"
    r"\bwho(?:m)? is on (?:that|the|this) lease\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z]+")
_NAME_SIMILARITY_THRESHOLD = 0.84
_MIN_NAME_TOKEN_LENGTH = 3
_MAX_AMBIGUOUS_MATCHES = 5
_INVALID_TARGET = "target_role must be auto, ops, chief, or treasurer"
_NAME_STOPWORDS = {
    "what",
    "which",
    "whose",
    "lease",
    "number",
    "signed",
    "sign",
    "signer",
    "signers",
    "landlord",
    "landlords",
    "tenant",
    "tenants",
    "party",
    "parties",
    "and",
    "the",
    "their",
    "has",
    "have",
    "yet",
    "supposed",
}


def classify_core_read(
    message: str,
    *,
    has_lease_context: bool = False,
) -> IntentEnvelope | None:
    text = str(message or "").strip()
    if _SIGNING_QUESTION.search(text) and (
        _LEASE_NUMBER.search(text) or has_lease_context
    ):
        return IntentEnvelope(
            name="lease_signing_roster",
            domain_lane="leases",
            operation="read",
            requested_facet="lease.signing_roster",
        )
    if _LEASE_IDENTITY_QUESTION.search(text):
        return IntentEnvelope(
            name="lease_identity",
            domain_lane="leases",
            operation="read",
            requested_facet="lease.signing_roster",
        )
    return None


def _reference_lease_id(reference: dict) -> str:
    payload = reference.get("semantic_payload") or {}
    subject = payload.get("subject") or {}
    if subject.get("type") == "lease" and subject.get("id"):
        return str(subject["id"])
    for entity in reference.get("entity_refs") or []:
        if entity.get("type") == "lease" and entity.get("id"):
            return str(entity["id"])
    return ""


def _similar_name_token(candidate: str, message_words: set[str]) -> bool:
    if candidate in message_words:
        return True
    return any(
        SequenceMatcher(None, candidate, word).ratio()
        >= _NAME_SIMILARITY_THRESHOLD
        for word in message_words
    )


def _resolve_lease_for_core_read(  # noqa: C901
    landlord,
    message: str,
    reference: dict,
):
    from rentium.leases.models import Lease
    from rentium.leases.models import LeaseTenant

    qs = Lease.objects.filter(landlord=landlord).select_related(
        "landlord__user", "property", "group",
    ).prefetch_related(
        "landlord_signatories__member",
        Prefetch(
            "lease_tenants",
            queryset=LeaseTenant.objects.select_related(
                "tenant__user",
            ).prefetch_related("invite_events"),
        ),
    )
    exact = _LEASE_NUMBER.search(message or "")
    if exact:
        lease = qs.filter(lease_number__iexact=exact.group(0)).first()
        gaps = [] if lease else [f"No lease matches {exact.group(0).upper()}."]
        return lease, gaps

    reference_id = _reference_lease_id(reference)
    if reference_id:
        lease = qs.filter(pk=reference_id).first()
        if lease:
            return lease, []

    message_words = {
        word
        for word in _WORD.findall((message or "").casefold())
        if word not in _NAME_STOPWORDS
    }
    if not message_words:
        return None, ["Name the lease number, property, or a party on the lease."]
    ranked = []
    # Search the landlord's complete lease set. A silent "latest 200" cutoff
    # turns an older real lease into a false absence claim as portfolios grow.
    for lease in qs.order_by("-created_at"):
        score = 0
        matched_people = 0
        for slot in lease.lease_tenants.all():
            name_words = [
                word
                for word in _WORD.findall((slot.display_name or "").casefold())
                if len(word) >= _MIN_NAME_TOKEN_LENGTH
            ]
            matched = sum(
                _similar_name_token(word, message_words) for word in name_words
            )
            if matched:
                matched_people += 1
                score += matched
        if score:
            ranked.append((matched_people, score, lease))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked:
        return None, ["I could not resolve a lease from the named parties."]
    best_people, best_score, lease = ranked[0]
    tied = [row for row in ranked if row[:2] == (best_people, best_score)]
    if len(tied) > 1:
        return None, [
            "Several leases match those names; provide the exact lease number.",
            *[
                row[2].lease_number or str(row[2].pk)
                for row in tied[:_MAX_AMBIGUOUS_MATCHES]
            ],
        ]
    return lease, []


def resolve_lease_from_message(landlord, message: str, reference: dict | None = None):
    """Resolve one lease from an exact number, visible reference, or parties.

    This is deliberately shared by deterministic reads and writes.  A write
    router must not grow its own tenant-name matching rules and then target a
    different lease from the read router.  Ambiguity remains a hard stop and
    is returned as structured human-readable gaps.
    """
    return _resolve_lease_for_core_read(landlord, message, reference or {})


def _render_signing_answer(lease, intent: IntentEnvelope, packet) -> str:
    roster = packet.metadata
    if intent.name == "lease_identity":
        tenants = [row["name"] for row in roster["tenants"]]
        names = " and ".join(tenants) if tenants else "The named parties"
        place = (
            lease.property.name
            if lease.property_id
            else (lease.group.name if lease.group_id else "the rental")
        )
        return f"{names} are on lease {lease.lease_number} ({place})."

    lines = [
        f"Lease {lease.lease_number} — {lease.status}.",
        "",
        "Parties expected to sign:",
    ]
    for party in roster["parties"]:
        status = "signed" if party["has_signed"] else "not signed"
        lines.append(f"• {party['name']} — {party['role_label']} — {status}")
    unsigned = roster["unsigned_expected_signers"]
    lines.extend(["", "Still unsigned:"])
    if unsigned:
        lines.extend(
            f"• {party['name']} — {party['role_label']}" for party in unsigned
        )
    else:
        lines.append("• Nobody; every expected signer has signed.")
    lines.extend(["", roster["policy_note"]])
    return "\n".join(lines)


def prepare_core_read(
    landlord,
    message: str,
    reference: dict | None = None,
) -> AnswerEnvelope | None:
    """Resolve and answer a trusted operational read without model prose."""
    from rentium.leases.parties import lease_signing_fact_packet

    reference = reference or {}
    intent = classify_core_read(
        message,
        has_lease_context=bool(_reference_lease_id(reference)),
    )
    if intent is None:
        return None
    lease, gaps = _resolve_lease_for_core_read(landlord, message, reference)
    if lease is None:
        return AnswerEnvelope(
            reply="I can't answer that safely yet. " + " ".join(gaps),
            answer_mode="ABSTAINED",
            coverage=Coverage.PARTIAL.value,
            evidence={"missing": gaps, "facet": intent.requested_facet},
            intent=intent,
        )
    packet = lease_signing_fact_packet(lease)
    return AnswerEnvelope(
        reply=_render_signing_answer(lease, intent, packet),
        answer_mode=(
            "CERTIFIED" if packet.coverage == Coverage.COMPLETE else "ABSTAINED"
        ),
        coverage=packet.coverage.value,
        evidence=packet.to_dict(),
        subject=packet.subject,
        intent=intent,
    )


def run_core_read_turn(  # noqa: PLR0913
    *,
    landlord,
    message: str,
    conversation_id: uuid.UUID,
    channel: str,
    message_id: uuid.UUID | str | None = None,
    reply_to_message_id: uuid.UUID | str | None = None,
    external_key: str = "",
    external_message_id: str = "",
    reply_to_external_id: str = "",
):
    """Persist one evidence-first visible turn and return a normal TurnResult."""
    from .conversations import record_visible_message
    from .conversations import reference_context
    from .models import RamaAudit
    from .models import RamaMessage
    from .service import TurnResult

    inbound = record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.INBOUND,
        text=message,
        channel=channel,
        role="corporal",
        message_id=message_id,
        external_key=external_key,
        external_message_id=external_message_id,
        reply_to_message_id=reply_to_message_id,
        reply_to_external_id=reply_to_external_id,
    )
    answer = prepare_core_read(landlord, message, reference_context(inbound))
    if answer is None:
        # The caller performs classification first; retain this guard for direct use.
        return None
    semantic_payload = {
        "answer_mode": answer.answer_mode,
        "coverage": answer.coverage,
        "trace_id": str(answer.trace_id),
        "subject": answer.subject,
        "intent": answer.intent.name if answer.intent else "",
    }
    outbound = record_visible_message(
        landlord=landlord,
        conversation_id=conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
        text=answer.reply,
        channel=channel,
        role="corporal",
        external_key=external_key,
        semantic_payload=semantic_payload,
        entity_refs=([answer.subject] if answer.subject else []),
    )
    from .evals import record_evidence_trace

    roster = answer.evidence.get("metadata") or {}
    known_values = [
        value
        for party in roster.get("parties") or []
        for value in (party.get("name"), party.get("email"), party.get("phone"))
        if value
    ]
    record_evidence_trace(
        landlord=landlord,
        conversation_id=conversation_id,
        message_id=outbound.pk,
        engine_mode="v3",
        answer_mode=answer.answer_mode,
        coverage=answer.coverage,
        provider="deterministic",
        model="evidence-core-v3",
        intent=answer.intent.name if answer.intent else "",
        subject=answer.subject,
        packet=answer.evidence,
        raw_input=message,
        known_values=known_values,
    )
    RamaAudit.objects.create(
        landlord=landlord,
        conversation_id=conversation_id,
        kind=RamaAudit.Kind.ASSISTANT_MESSAGE,
        provider="deterministic",
        model="evidence-core-v3",
        content={**semantic_payload, "facet": answer.evidence.get("facet")},
    )
    return TurnResult(
        conversation_id=conversation_id,
        episode_id=outbound.episode_id,
        message_id=outbound.pk,
        reply=answer.reply,
        provider="deterministic",
        model="evidence-core-v3",
        tools_used=[answer.intent.requested_facet] if answer.intent else [],
        deterministic=True,
        answer_mode=answer.answer_mode,
        coverage=answer.coverage,
        evidence=answer.evidence,
        trace_id=answer.trace_id,
        engine_mode="v3",
    )


_STRATEGIC_FINANCE = re.compile(
    r"\b(?:best investment|should i (?:buy|sell|refinance|renovate)|roi|"
    r"return on investment|cash flow strategy|financing option|where (?:can|should) "
    r"i invest|increase (?:profit|revenue)|reduce costs?|portfolio strategy)\b",
    re.IGNORECASE,
)
_FINANCE = re.compile(
    r"\b(?:rent|payment|deposit|expense|invoice|ledger|balance|payout|arrears|"
    r"cash flow|income|tax|mortgage|equity|profit|revenue|cost)\b",
    re.IGNORECASE,
)
_OPERATION = re.compile(
    r"\b(?:create|add|update|change|rename|activate|terminate|record|post|send|"
    r"schedule|upload|file|move|remove|mark|invite|show|list|open)\b",
    re.IGNORECASE,
)


def routing_decision(message: str, target_role: str = "auto") -> RoutingDecision:
    target = (target_role or "auto").strip().casefold()
    explicit = {
        "ops": "corporal",
        "corporal": "corporal",
        "chief": "general",
        "general": "general",
        "treasurer": "treasurer",
    }
    if target in explicit:
        role = explicit[target]
        return RoutingDecision(
            role=role,
            reason="explicit user target",
            requires_deliberation=(
                role == "treasurer" and bool(_STRATEGIC_FINANCE.search(message))
            ),
        )
    if target != "auto":
        raise ValueError(_INVALID_TARGET)
    if _STRATEGIC_FINANCE.search(message):
        return RoutingDecision(
            role="treasurer",
            reason="strategic financial question",
            requires_deliberation=True,
        )
    if _OPERATION.search(message):
        return RoutingDecision("corporal", "concrete operational request")
    if _FINANCE.search(message):
        return RoutingDecision("treasurer", "financial analysis request")
    return RoutingDecision("general", "cross-domain or conversational request")


def choose_role(message: str) -> str:
    """Compatibility helper for the unified chat endpoint."""
    return routing_decision(message).role


def run_strategic_treasurer_turn(
    *,
    landlord,
    envelope: TurnEnvelope,
):
    """Run the existing bounded deliberation pipeline for a live strategy ask."""
    from . import deliberation
    from .conversations import record_visible_message
    from .models import RamaMessage
    from .service import TurnResult

    inbound = record_visible_message(
        landlord=landlord,
        conversation_id=envelope.conversation_id,
        direction=RamaMessage.Direction.INBOUND,
        text=envelope.message,
        channel=envelope.channel,
        role="treasurer",
        message_id=envelope.message_id,
        reply_to_message_id=envelope.reply_to_message_id,
        semantic_payload={"client_context": envelope.client_context},
    )
    row = deliberation.run(
        landlord,
        topic="everything",
        question=envelope.message,
        trigger="live_chat",
        dedupe_key=f"live:{envelope.message_id}",
    )
    options = list(row.options.order_by("rank", "catalogue_key"))
    scoreable = [item for item in options if item.scores.get("payback_years")]
    lines = ["I ran the structured Treasurer analysis against your current records."]
    if scoreable:
        best = scoreable[0]
        lines.append(f"Best supported option: {best.label}.")
        details = []
        if best.scores.get("annual_saving"):
            details.append(f"annual saving {best.scores['annual_saving']}")
        if best.scores.get("payback_years"):
            details.append(f"payback {best.scores['payback_years']} years")
        if best.scores.get("ten_year_net"):
            details.append(f"10-year net {best.scores['ten_year_net']}")
        if details:
            lines.append("Supported figures: " + ", ".join(details) + ".")
    else:
        lines.append(
            "There is not enough verified cost-and-benefit data to rank an "
            "option safely.",
        )
    open_requests = row.requests.filter(status="OPEN").count()
    if open_requests:
        lines.append(
            f"I need {open_requests} missing figure(s) before treating the "
            "result as final.",
        )
    lines.append(f"Analysis ID: {row.pk}")
    reply = "\n".join(lines)
    outbound = record_visible_message(
        landlord=landlord,
        conversation_id=envelope.conversation_id,
        direction=RamaMessage.Direction.OUTBOUND,
        text=reply,
        channel=envelope.channel,
        role="treasurer",
        semantic_payload={
            "deliberation_id": str(row.pk),
            "status": row.status,
            "source_message_id": str(inbound.pk),
        },
    )
    return TurnResult(
        conversation_id=envelope.conversation_id,
        episode_id=outbound.episode_id,
        message_id=outbound.pk,
        reply=reply,
        provider="hybrid",
        model="treasurer-deliberation-v1",
        tools_used=["structured_treasurer_deliberation"],
        deterministic=True,
    )
