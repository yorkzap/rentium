"""
Per-tool metadata for RAMA's write surface — the policy layer plans run on.

Every mutating tool gets one declarative entry:

- ``risk``: low | medium | high | legal — how bad a wrong execution is.
- ``own_confirm``: True → inside a multi-step plan this step ALWAYS pauses
  for its own explicit confirmation (tiered confirm UX). Server-side policy;
  the model can never set or unset it.
- ``blockers``: optional cheap precheck ``fn(landlord, **step_args) ->
  list[dict]`` sharing the SAME implementation the single tool enforces at
  execution time (factored into domain_crud), so plan partitioning and
  execution can never disagree about why something is blocked.

Tools registered in registry.py but missing here are treated as
own_confirm=True — new write tools are maximally cautious until a human
classifies them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .domain_crud import (
    delete_draft_lease_blockers,
    delete_property_blockers,
    terminate_lease_blockers,
)


@dataclass(frozen=True)
class ToolMeta:
    risk: str = "medium"
    own_confirm: bool = False
    blockers: Callable[..., list[dict]] | None = None


# Safe default for anything unclassified.
DEFAULT_META = ToolMeta(risk="high", own_confirm=True)

TOOL_META: dict[str, ToolMeta] = {
    # ------------------------------------------------------- maintenance
    "create_work_order": ToolMeta(risk="low"),
    "transition_work_order": ToolMeta(risk="low"),
    "update_work_order": ToolMeta(risk="low"),
    "complete_work_order": ToolMeta(risk="low"),
    "add_work_order_comment": ToolMeta(risk="low"),
    # ------------------------------------------------------ communication
    "mark_inquiry_replied": ToolMeta(risk="low"),
    "send_tenant_message": ToolMeta(risk="medium"),
    "mark_messages_read": ToolMeta(risk="low"),
    "schedule_viewing": ToolMeta(risk="low"),
    "respond_to_viewing_request": ToolMeta(risk="low"),
    "set_viewing_availability": ToolMeta(risk="low"),
    # ------------------------------------------------------------ money
    "create_expense": ToolMeta(risk="medium"),
    # -------------------------------------------------------- inventory
    "bulk_add_inventory": ToolMeta(risk="low"),
    "create_inventory_item": ToolMeta(risk="low"),
    "update_inventory_item": ToolMeta(risk="low"),
    "delete_inventory_item": ToolMeta(risk="low"),
    "create_shared_inventory_item": ToolMeta(risk="low"),
    "delete_shared_inventory_item": ToolMeta(risk="low"),
    # ------------------------------------------------------- properties
    "create_property": ToolMeta(risk="low"),
    "duplicate_listing": ToolMeta(risk="low"),
    "attach_photo_to_listing": ToolMeta(risk="low"),
    # Logging a capability gap is frictionless (no confirm) — we WANT RAMA to
    # record what it can't do rather than fail silently.
    "log_capability_gap": ToolMeta(risk="low"),
    "list_capability_gaps": ToolMeta(risk="low"),
    "update_property": ToolMeta(risk="low"),
    "update": ToolMeta(risk="medium"),  # generic manifest write (previewed)
    "delete_property": ToolMeta(
        risk="high", blockers=delete_property_blockers
    ),
    "create_property_group": ToolMeta(risk="low"),
    "assign_property_to_group": ToolMeta(risk="low"),
    "create_holding": ToolMeta(risk="low"),
    "assign_property_to_holding": ToolMeta(risk="low"),
    # A wrong "auto-corrected" balance is worse than a stale one — always
    # its own explicit confirmation, even inside a larger plan.
    "update_bank_balance": ToolMeta(risk="medium", own_confirm=True),
    "setup_room_tenancy": ToolMeta(risk="medium"),
    "create_house_layout": ToolMeta(risk="medium"),
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
    "list_co_landlords": ToolMeta(risk="low"),
    "rebalance_lease_rents": ToolMeta(risk="medium"),
    # ------------------------------------------------------ inspections
    "create_condition_inspection": ToolMeta(risk="low"),
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
