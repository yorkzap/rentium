"""Small V3 orchestration seam.

The legacy turn engine still owns its mature deterministic adapters.  New
surfaces enter through this typed envelope and an explicit sequence so routing,
resolution, policy, execution, verification, and rendering can evolve without
adding more persona-specific chat endpoints.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field


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
            requires_deliberation=role == "treasurer" and bool(_STRATEGIC_FINANCE.search(message)),
        )
    if target != "auto":
        raise ValueError("target_role must be auto, ops, chief, or treasurer")
    if _STRATEGIC_FINANCE.search(message):
        return RoutingDecision("treasurer", "strategic financial question", True)
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
            "There is not enough verified cost-and-benefit data to rank an option safely."
        )
    open_requests = row.requests.filter(status="OPEN").count()
    if open_requests:
        lines.append(
            f"I need {open_requests} missing figure(s) before treating the result as final."
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
