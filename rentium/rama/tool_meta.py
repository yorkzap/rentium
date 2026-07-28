"""
Per-tool metadata for RAMA's write surface — the policy layer plans run on.

Every mutating tool gets one declarative entry:

- ``risk``: low | medium | high | legal — how bad a wrong execution is.
  Documentation plus a cross-check: an OPT_IN tool that isn't ``low`` fails an
  import-time test, so the two fields can never quietly disagree.
- ``own_confirm``: True → inside a multi-step plan this step ALWAYS pauses
  for its own explicit confirmation (tiered confirm UX). Server-side policy;
  the model can never set or unset it.
- ``blockers``: optional cheap precheck ``fn(landlord, **step_args) ->
  list[dict]`` sharing the SAME implementation the single tool enforces at
  execution time (factored into domain_crud), so plan partitioning and
  execution can never disagree about why something is blocked.
- ``autonomy``: whether this tool may ever run without the landlord saying
  yes. See below — this is the load-bearing field, NOT ``risk``.
- ``auto_category``: which Constitution opt-in category authorises it.
- ``undo``: the tool's deterministic inverse. REQUIRED for OPT_IN.
- ``auto_guard``: extra per-argument refusal, for arguments that make an
  otherwise-reversible call irreversible.

Tools registered in registry.py but missing here are treated as
own_confirm=True, autonomy=NEVER — new write tools are maximally cautious
until a human classifies them.

Why ``autonomy`` and not ``risk``
---------------------------------
``risk`` conflates two independent questions: how bad is it if this is wrong,
and can I take it back. Those come apart constantly. ``schedule_viewing``
creates one small calendar row (feels cheap) but emails a stranger and asks a
sitting tenant for consent (cannot be unsent). ``delete_inventory_item``
destroys a record worth twenty dollars (feels cheap) and there is no way back.

So autonomy is gated on *reversibility, proven by construction*: a tool may
only be OPT_IN if someone has written its exact inverse. If you cannot write
``undo``, the tool does not get to run by itself. That rule is enforced by
``test_autonomy.py`` at import time, not by reviewer memory.

A tool with no ``confirm`` parameter at all (e.g. ``log_capability_gap``) is
already frictionless and never reaches this gate — it produces no preview, so
there is nothing to auto-approve. Such tools do not need an ``autonomy``
setting and should not be given one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .domain_crud import (
    delete_draft_lease_blockers,
    delete_property_blockers,
    terminate_lease_blockers,
)


class Autonomy(str, Enum):
    """Whether a tool may execute without the landlord confirming it."""

    CONFIRM = "confirm"  # today's behaviour — the landlord must say yes
    OPT_IN = "opt_in"    # may auto-run IF the Constitution enables its category
    NEVER = "never"      # may never auto-run, whatever the Constitution says


# Inverses for the OPT_IN tier. Signature:
#   undo(arguments, result) -> (tool_name, arguments) | None
# `arguments` is what was called (confirm already stripped), `result` is what
# the execution returned. Returning None means "no longer reversible" and the
# receipt is recorded as such rather than offering an undo that would fail.

# Tool-argument name -> model field the impl writes it to.
_INVENTORY_ARG_FIELDS = {
    "name": "name",
    "quantity": "quantity",
    "condition": "condition",
    "location": "location_description",
    "description": "description",
}


def _undo_update_inventory_item(
    arguments: dict, result: dict
) -> tuple[str, dict] | None:
    """Restore the field values update_inventory_item overwrote."""
    previous = (result or {}).get("previous") or {}
    if not previous:
        return None
    # The item may have just been renamed, so target its CURRENT name —
    # arguments["item_name"] is the pre-change name and would no longer match.
    current_name = ((result or {}).get("item") or {}).get("name")
    args: dict = {
        "property_query": arguments.get("property_query", ""),
        "item_name": current_name or arguments.get("item_name", ""),
    }
    for arg, field in _INVENTORY_ARG_FIELDS.items():
        if field in previous:
            args[arg] = str(previous[field])
    if len(args) <= 2:
        return None
    return ("update_inventory_item", args)


def _undo_triage_capability_gap(
    arguments: dict, result: dict
) -> tuple[str, dict] | None:
    """Put the gap back on the status it held before triage."""
    from_status = (result or {}).get("from_status")
    gap_id = (result or {}).get("gap_id")
    if not from_status or not gap_id:
        return None
    return ("triage_capability_gap", {"gap_query": str(gap_id), "status": from_status})


def _undo_remember(arguments: dict, result: dict) -> tuple[str, dict] | None:
    """Undo a remember: restore what it replaced, or drop it if it was new."""
    subject = (result or {}).get("subject") or arguments.get("subject") or ""
    if not subject:
        return None
    replaced = (result or {}).get("replaced") or ""
    if replaced:
        return ("remember", {"subject": str(subject), "fact": str(replaced)})
    return ("forget", {"subject": str(subject)})


def _undo_forget(arguments: dict, result: dict) -> tuple[str, dict] | None:
    """Undo a forget: put the preference back verbatim."""
    subject = (result or {}).get("subject") or arguments.get("subject") or ""
    fact = (result or {}).get("fact") or ""
    if not subject or not fact:
        return None
    return ("remember", {"subject": str(subject), "fact": str(fact)})


def _guard_triage_capability_gap(landlord, **step_args) -> str | None:
    """Prioritising a gap is one-way — there is no un-prioritise path — so a
    triage call that also prioritises is not reversible and never auto-runs."""
    if str(step_args.get("prioritise") or "").strip().lower() in (
        "1", "true", "yes", "y", "on",
    ):
        return "Prioritising a gap can't be undone, so it needs your confirmation."
    return None


@dataclass(frozen=True)
class ToolMeta:
    risk: str = "medium"
    own_confirm: bool = False
    blockers: Callable[..., list[dict]] | None = None
    autonomy: str = Autonomy.CONFIRM
    auto_category: str = ""
    undo: Callable[[dict, dict], tuple[str, dict] | None] | None = None
    auto_guard: Callable[..., str | None] | None = None
    # True when each call creates a NEW record and sibling calls cannot
    # interfere. plan_runner groups steps by item_key so that a failed rename
    # skips the group-assignment that depended on it — but that grouping keys
    # off the shared property, so two independent expenses on one address were
    # being treated as chained, and a failure in the first silently SKIPPED the
    # second. Independent writers get their own key instead.
    independent_writes: bool = False


# Safe default for anything unclassified: confirms, and never auto-runs.
DEFAULT_META = ToolMeta(risk="high", own_confirm=True, autonomy=Autonomy.NEVER)

# Constitution opt-in categories. A category not listed here is rejected at
# amendment time, so a typo disables autonomy rather than silently widening it.
AUTO_CATEGORIES = frozenset({"admin", "inventory", "memory"})

TOOL_META: dict[str, ToolMeta] = {
    # ------------------------------------------------------- maintenance
    "create_work_order": ToolMeta(risk="low"),
    "transition_work_order": ToolMeta(risk="low"),
    "update_work_order": ToolMeta(risk="low"),
    "complete_work_order": ToolMeta(risk="low"),
    # Attributing damage to a person decides who pays; a wrong name costs
    # somebody money, so it confirms on its own inside a plan.
    "attribute_work_order": ToolMeta(risk="high", own_confirm=True),
    # Renders on the work order the TENANT sees (WorkOrderComment has no
    # is_internal flag), so it is outbound, not a private note.
    "add_work_order_comment": ToolMeta(risk="medium", autonomy=Autonomy.NEVER),
    # ------------------------------------------------------ communication
    # Flips the lead to REPLIED via mark_replied(); the tool exposes no way to
    # set a status back, so there is no inverse to declare. Confirms until a
    # reopen path exists.
    "mark_inquiry_replied": ToolMeta(risk="low"),
    # Outbound to a human. Unsendable.
    "send_tenant_message": ToolMeta(risk="high", autonomy=Autonomy.NEVER),
    "mark_messages_read": ToolMeta(risk="low"),
    # Publishes appointment.scheduled / appointment.tenant_review — emails the
    # viewer and asks the sitting tenant for consent. Irreversible outbound.
    "schedule_viewing": ToolMeta(risk="high", autonomy=Autonomy.NEVER),
    "respond_to_viewing_request": ToolMeta(risk="high", autonomy=Autonomy.NEVER),
    "set_viewing_availability": ToolMeta(risk="low"),
    # ------------------------------------------------------------ money
    # Two expenses on one address are two separate costs, not a chain.
    "create_expense": ToolMeta(risk="medium", independent_writes=True),
    # Voids a posted expense and re-posts it elsewhere. Never autonomous: the
    # landlord decides where a cost belongs, and the whole reason this tool
    # exists is that guessing produced a mis-scoped charge to a tenant.
    "reallocate_expense": ToolMeta(risk="high", autonomy=Autonomy.NEVER),
    # Can post an immutable ledger expense.
    "catalog_business_document": ToolMeta(risk="medium", autonomy=Autonomy.NEVER),
    # -------------------------------------------------------- inventory
    "bulk_add_inventory": ToolMeta(risk="low"),
    # Flips property.is_furnished, which shows on the public listing — a
    # public side-effect of an otherwise private register.
    "create_inventory_item": ToolMeta(risk="low", autonomy=Autonomy.NEVER),
    "update_inventory_item": ToolMeta(
        risk="low",
        autonomy=Autonomy.OPT_IN,
        auto_category="inventory",
        undo=_undo_update_inventory_item,
    ),
    # Hard delete. No soft-delete, no recovery path.
    "delete_inventory_item": ToolMeta(risk="medium", autonomy=Autonomy.NEVER),
    "create_shared_inventory_item": ToolMeta(risk="low"),
    "delete_shared_inventory_item": ToolMeta(risk="medium", autonomy=Autonomy.NEVER),
    # ------------------------------------------------------- properties
    # Creates a PUBLIC listing, and delete_property is blocked once anything
    # attaches to it — so "create it and remove it later" is not available.
    "create_property": ToolMeta(risk="medium", autonomy=Autonomy.NEVER),
    "duplicate_listing": ToolMeta(risk="medium", autonomy=Autonomy.NEVER),
    # Changes what the public sees and consumes a single-use RamaUpload.
    "attach_photo_to_listing": ToolMeta(risk="medium", autonomy=Autonomy.NEVER),
    # Takes no confirm at all, so it is already frictionless and never reaches
    # the autonomy gate — we WANT RAMA to record what it can't do rather than
    # fail silently.
    "log_capability_gap": ToolMeta(risk="low"),
    "update_property": ToolMeta(risk="low"),
    "update": ToolMeta(risk="medium"),  # generic manifest write (previewed)
    "delete_property": ToolMeta(
        risk="high", blockers=delete_property_blockers
    ),
    "create_property_group": ToolMeta(risk="low"),
    # Records a decision about the backlog; touches no landlord data, and the
    # preview carries from_status so the inverse is exact.
    "triage_capability_gap": ToolMeta(
        risk="low",
        autonomy=Autonomy.OPT_IN,
        auto_category="admin",
        undo=_undo_triage_capability_gap,
        auto_guard=_guard_triage_capability_gap,
    ),
    "assign_property_to_group": ToolMeta(risk="low"),
    "create_holding": ToolMeta(risk="low"),
    "assign_property_to_holding": ToolMeta(risk="low"),
    # A wrong "auto-corrected" balance is worse than a stale one — always
    # its own explicit confirmation, even inside a larger plan.
    "update_bank_balance": ToolMeta(risk="medium", own_confirm=True),
    "setup_room_tenancy": ToolMeta(risk="medium"),
    "create_house_layout": ToolMeta(risk="medium"),
    "create_property_structure": ToolMeta(risk="medium"),
    "update_unit_layout": ToolMeta(risk="low"),
    # Reshapes what is on the market, so it pauses for its own confirmation
    # inside a multi-step plan even though it deletes nothing.
    "set_unit_rental_mode": ToolMeta(risk="high", own_confirm=True),
    "create_group_room": ToolMeta(risk="medium"),
    # ----------------------------------------------------------- leases
    "create_lease": ToolMeta(risk="medium"),
    "update_lease": ToolMeta(risk="medium"),
    "delete_draft_lease": ToolMeta(
        risk="medium", blockers=delete_draft_lease_blockers
    ),
    # Legal/financial: these always pause for their own confirm in a plan.
    "terminate_lease": ToolMeta(
        risk="legal", own_confirm=True, blockers=terminate_lease_blockers
    ),
    "landlord_sign_lease": ToolMeta(risk="legal", own_confirm=True),
    # ------------------------------------------------------- lease roster
    "invite_tenant_to_lease": ToolMeta(risk="medium"),
    "resend_lease_invite": ToolMeta(risk="low"),
    "cancel_lease_invite": ToolMeta(risk="medium"),
    "replace_lease_invite": ToolMeta(risk="medium"),
    "add_roommate_to_lease": ToolMeta(risk="medium"),
    "add_co_host_to_lease": ToolMeta(risk="low"),
    # Grants portfolio ACCESS — own confirm, never silent inside a plan.
    "add_co_landlord": ToolMeta(risk="high", own_confirm=True),
    "rebalance_lease_rents": ToolMeta(risk="medium"),
    # ------------------------------------------------------ inspections
    "create_condition_inspection": ToolMeta(risk="low"),
    # ------------------------------------------------ property financials
    # Facts about the asset, not money movements — nothing posts to the
    # ledger. A valuation ADDS to a history; a mortgage supersedes rather
    # than edits, so neither destroys what came before.
    # Records a belief, not a transaction — nothing posts to the ledger, and
    # the reconciliation check means it cannot silently inflate a total.
    "record_treasurer_fact": ToolMeta(risk="low"),
    "record_holding_financials": ToolMeta(risk="low"),
    "record_valuation": ToolMeta(risk="low"),
    "record_mortgage": ToolMeta(risk="medium"),
    # ---------------------------------------------------------- memory
    # Landlord-private, single target, no money, no outbound, and the inverse
    # is exact — the cleanest member of the autonomy tier.
    "remember": ToolMeta(
        risk="low",
        autonomy=Autonomy.OPT_IN,
        auto_category="memory",
        undo=_undo_remember,
    ),
    "forget": ToolMeta(
        risk="low",
        autonomy=Autonomy.OPT_IN,
        auto_category="memory",
        undo=_undo_forget,
    ),
    # ----------------------------------------------------- constitution
    # Policy is what everything else obeys — amendments always get their own
    # explicit confirmation, even inside a larger plan.
    "amend_constitution": ToolMeta(risk="high", own_confirm=True),
}


def meta_for(tool_name: str) -> ToolMeta:
    """Metadata for a tool; unclassified tools come back maximally cautious."""
    return TOOL_META.get(tool_name, DEFAULT_META)


def blockers_for(tool_name: str, landlord, **step_args) -> list[dict]:
    """Run the tool's cheap blocker precheck (empty list = clear to run)."""
    meta = meta_for(tool_name)
    if meta.blockers is None:
        return []
    return meta.blockers(landlord, **step_args)
