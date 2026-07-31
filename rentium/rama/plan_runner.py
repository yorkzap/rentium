"""
Plan persistence + execution for RAMA's pending plans.

The confirm state machine, deterministic end to end:

- A previewed plan (from plan_operation / plan_move_tenant, or any single
  write tool's needs_confirm preview wrapped as a one-step plan) is persisted
  via save_plan()/save_single(). A row exists IFF a plan is outstanding.
- On the landlord's affirmation the backend calls run_plan() — the model
  never reconstructs tool calls. Every step runs through registry.execute()
  with confirm="yes", so each tool's own guardrails re-validate at execution
  time (state may have drifted since preview).
- Tiered confirm: in a MULTI-step plan, steps flagged requires_own_confirm
  (TOOL_META policy — lease terminations and similar) pause execution and
  demand their own explicit "yes". A single-step plan never double-asks:
  its preview asked exactly about that step.
- Failure of a step marks it FAILED and SKIPPEDs the remaining steps sharing
  its item_key; other items continue. No cross-item transaction — each tool
  is already internally atomic, and honest PARTIAL reporting beats
  all-or-nothing lock-holding.

validate_plan() is the safety valve for arbitrary step lists: today it
double-checks playbook output; later a smarter planner model can emit steps
through the SAME validation + runner without new safety code.
"""

from __future__ import annotations

import json

from django.utils import timezone

from .command_engine import create_task, record_receipt
from .models import RamaPendingPlan, RamaPlanStep, RamaTask
from .outcomes import CommandOutcome, OutcomeKind
from .registry import REGISTRY, execute
from .tool_meta import already_done_for, blockers_for, meta_for

PENDING_PLAN_TTL_SECONDS = 30 * 60


# ------------------------------------------------------------- validation
def validate_plan(steps: list[dict], landlord) -> list[str]:
    """Errors preventing this step list from being persisted ([] = valid)."""
    errors: list[str] = []
    if not steps:
        errors.append("Plan has no steps.")
    for i, step in enumerate(steps, start=1):
        tool_name = step.get("tool") or ""
        tool = REGISTRY.get(tool_name)
        if tool is None:
            errors.append(f"Step {i}: unknown tool {tool_name!r}.")
            continue
        allowed = set(tool.parameters["properties"])
        args = step.get("arguments") or {}
        unknown = [k for k in args if k not in allowed]
        if unknown:
            errors.append(
                f"Step {i} ({tool_name}): unknown arguments {', '.join(unknown)}."
            )
        missing = [
            k
            for k in tool.parameters.get("required", [])
            if k not in args and k != "confirm"
        ]
        if missing:
            errors.append(
                f"Step {i} ({tool_name}): missing required {', '.join(missing)}."
            )
        # Blocker precheck runs on the schema-allowed args only — anything
        # else (incl. a smuggled `landlord`) was already reported above.
        safe_args = {k: v for k, v in args.items() if k in allowed and k != "confirm"}
        for blocker in blockers_for(tool_name, landlord, **safe_args):
            errors.append(f"Step {i} ({tool_name}): {blocker['detail']}")
        # Second of the two sites the check runs at. Preview time catches it
        # before the landlord is ever shown the proposal; this catches the
        # window between them seeing it and saying yes — a payment can land by
        # another route in between, and confirming a stale preview would then
        # record the same money twice.
        duplicate = already_done_for(tool_name, landlord, **safe_args)
        if duplicate:
            errors.append(f"Step {i} ({tool_name}): {duplicate}")
    return errors


# ------------------------------------------------------------ persistence
def save_plan(landlord, conversation_id, plan_payload: dict) -> RamaPendingPlan:
    """Persist a playbook plan payload (latest plan per conversation wins)."""
    clear_plan(landlord, conversation_id)
    task = create_task(
        landlord=landlord,
        conversation_id=conversation_id,
        capability_key=plan_payload.get("operation") or "plan",
        inputs=plan_payload,
    )
    task.transition_to(
        RamaTask.Status.AWAITING_CONFIRMATION,
        outcome=CommandOutcome(
            OutcomeKind.PREVIEW,
            plan_payload.get("summary") or "Ready for confirmation.",
            data={"operation": plan_payload.get("operation") or "plan"},
        ).as_dict(),
    )
    plan = RamaPendingPlan.objects.create(
        conversation_id=conversation_id,
        landlord=landlord,
        task=task,
        operation=plan_payload.get("operation") or "plan",
        summary=plan_payload.get("summary") or "",
        blocked=plan_payload.get("blocked") or [],
    )
    RamaPlanStep.objects.bulk_create(
        RamaPlanStep(
            plan=plan,
            order=i,
            tool=s["tool"],
            capability_key=s.get("capability_key") or s["tool"],
            arguments=s.get("arguments") or {},
            target_label=s.get("target") or "",
            item_key=s.get("item_key") or str(i),
            requires_own_confirm=bool(s.get("requires_own_confirm")),
        )
        for i, s in enumerate(plan_payload.get("steps") or [])
    )
    return plan


def save_single(landlord, conversation_id, tool: str, arguments: dict) -> RamaPendingPlan:
    """Wrap a single previewed write tool as a one-step plan — one code path
    for every confirmation."""
    label = ""
    for key in ("property_query", "lease_number", "room_name", "name"):
        if arguments.get(key):
            label = str(arguments[key])
            break
    return save_plan(
        landlord,
        conversation_id,
        {
            "operation": "single",
            "summary": f"{tool} {label}".strip(),
            "steps": [
                {
                    "tool": tool,
                    "arguments": {
                        k: v for k, v in (arguments or {}).items() if k != "confirm"
                    },
                    "target": label,
                    "item_key": "single",
                    "requires_own_confirm": meta_for(tool).own_confirm,
                }
            ],
        },
    )


def save_batch(
    landlord,
    conversation_id,
    pending_specs: list[dict],
) -> RamaPendingPlan:
    """Persist every preview a model produced in one turn as one plan.

    Weak models sometimes preview several routine writes before asking for one
    confirmation.  Persisting only the final preview makes the displayed batch
    a lie: the landlord's ``yes`` runs one arbitrary step and the model starts
    reconstructing the rest from prose.  This collector preserves the original
    call order and stable entity ids so one confirmation runs the exact batch.
    """
    if len(pending_specs) == 1:
        spec = pending_specs[0]
        if spec["kind"] == "plan":
            return save_plan(landlord, conversation_id, spec["payload"])
        if spec.get("target"):
            return save_plan(
                landlord,
                conversation_id,
                {
                    "operation": "single",
                    "summary": (
                        f"{spec['tool']} {str(spec['target']).strip()}".strip()
                    ),
                    "steps": [
                        {
                            "tool": spec["tool"],
                            "arguments": {
                                key: value
                                for key, value in (
                                    spec.get("arguments") or {}
                                ).items()
                                if key != "confirm"
                            },
                            "target": str(spec["target"]).strip(),
                            "item_key": "single",
                            "requires_own_confirm": meta_for(
                                spec["tool"],
                            ).own_confirm,
                        },
                    ],
                },
            )
        return save_single(
            landlord,
            conversation_id,
            spec["tool"],
            spec["arguments"],
        )

    steps: list[dict] = []
    blocked: list[dict] = []
    for spec_index, spec in enumerate(pending_specs):
        if spec["kind"] == "plan":
            payload = spec["payload"]
            blocked.extend(payload.get("blocked") or [])
            for step_index, step in enumerate(payload.get("steps") or []):
                copied = dict(step)
                copied["item_key"] = (
                    copied.get("item_key")
                    or f"plan-{spec_index}-step-{step_index}"
                )
                steps.append(copied)
            continue

        arguments = {
            key: value
            for key, value in (spec.get("arguments") or {}).items()
            if key != "confirm"
        }
        target = str(spec.get("target") or "").strip()
        if not target:
            for key in (
                "property_query",
                "lease_number",
                "room_name",
                "name",
                "email",
            ):
                if arguments.get(key):
                    target = str(arguments[key])
                    break

        # Chained operations on one property share an item key.  If its rename
        # fails, its later group assignment is skipped while unrelated items
        # can still proceed and be reported honestly.
        #
        # Tools that only ever ADD a record are exempt: they share a property
        # merely as scope, never as a dependency, so grouping them would make
        # one failed expense silently swallow the next.
        if meta_for(spec["tool"]).independent_writes:
            entity_key = f"{spec['tool']}#{len(steps)}"
        else:
            entity_key = (
                arguments.get("property_query")
                or arguments.get("holding_name")
                or arguments.get("lease_number")
                or arguments.get("room_name")
                or arguments.get("name")
                or f"step-{len(steps)}"
            )
        steps.append(
            {
                "tool": spec["tool"],
                "arguments": arguments,
                "target": target,
                "item_key": f"entity:{entity_key}"[:64],
                "requires_own_confirm": meta_for(spec["tool"]).own_confirm,
            },
        )

    return save_plan(
        landlord,
        conversation_id,
        {
            "operation": "preview_batch",
            "summary": (
                f"{len(steps)} previewed changes collected from one request."
            ),
            "steps": steps,
            "blocked": blocked,
        },
    )


def load_fresh_plan(landlord, conversation_id) -> RamaPendingPlan | None:
    """The still-valid outstanding plan for this conversation, or None."""
    plan = (
        RamaPendingPlan.objects.filter(
            conversation_id=conversation_id, landlord=landlord
        )
        .select_related("task")
        .prefetch_related("steps")
        .first()
    )
    if plan is None:
        return None
    if (timezone.now() - plan.updated_at).total_seconds() > PENDING_PLAN_TTL_SECONDS:
        if plan.task_id and plan.task.status not in RamaTask.TERMINAL_STATUSES:
            plan.task.transition_to(
                RamaTask.Status.CANCELLED,
                outcome=CommandOutcome(
                    OutcomeKind.NOOP,
                    "The confirmation window expired; nothing was executed.",
                ).as_dict(),
            )
        plan.delete()
        return None
    return plan


def clear_plan(landlord, conversation_id) -> None:
    plans = RamaPendingPlan.objects.filter(
        conversation_id=conversation_id, landlord=landlord
    ).select_related("task")
    for plan in plans:
        if plan.task_id and plan.task.status not in RamaTask.TERMINAL_STATUSES:
            plan.task.transition_to(
                RamaTask.Status.CANCELLED,
                outcome=CommandOutcome(
                    OutcomeKind.NOOP,
                    "Replaced by a newer plan.",
                ).as_dict(),
            )
        plan.delete()


def plan_to_payload(plan: RamaPendingPlan) -> dict:
    """The save_plan() input that reproduces this plan — used to re-home a
    delegated sub-turn's pending plan onto the delegating conversation so the
    landlord's next "yes" runs it there."""
    return {
        "operation": plan.operation,
        "summary": plan.summary,
        "blocked": plan.blocked,
        "steps": [
            {
                "tool": s.tool,
                "arguments": s.arguments,
                "target": s.target_label,
                "item_key": s.item_key,
                "requires_own_confirm": s.requires_own_confirm,
            }
            for s in plan.steps.order_by("order")
        ],
    }


def plan_brief(plan: RamaPendingPlan) -> dict:
    """JSON-safe description of an outstanding plan (for the UI + prompt)."""
    steps = list(plan.steps.all())
    return {
        "task": {
            "id": str(plan.task_id) if plan.task_id else None,
            "status": plan.task.status if plan.task_id else None,
            "outcome": plan.task.outcome if plan.task_id else None,
        },
        "operation": plan.operation,
        "summary": plan.summary,
        "status": plan.status,
        "awaiting_own_confirm": plan.status
        == RamaPendingPlan.Status.AWAITING_STEP_CONFIRM,
        "steps": [
            {
                "n": s.order + 1,
                "tool": s.tool,
                "target": s.target_label,
                "status": s.status,
                "requires_own_confirm": s.requires_own_confirm,
            }
            for s in steps
        ],
        "blocked": plan.blocked,
    }


# --------------------------------------------------------------- execution
def _step_outcome(step: RamaPlanStep) -> dict:
    return {
        "n": step.order + 1,
        "tool": step.tool,
        # The plan row is deleted as soon as execution finishes, so anything a
        # caller needs afterwards (autonomy.record_auto_actions builds each
        # receipt's inverse from these) has to travel out with the outcome.
        "arguments": step.arguments,
        "target": step.target_label,
        "result": step.result,
    }


def run_plan(plan: RamaPendingPlan, landlord, audit=None) -> dict:
    """Execute the plan from its cursor. Returns what actually happened:

    {"executed": [...], "failed": [...], "skipped": [...],
     "awaiting": {step} | None, "status": "done"|"partial"|"awaiting_step",
     "summary": str}

    Pauses (without executing) at a requires_own_confirm step in a multi-step
    plan, unless that step is exactly what this affirmation confirms
    (plan.status == AWAITING_STEP_CONFIRM). The plan row is deleted when
    execution finishes; it survives only while paused.
    """
    steps = list(plan.steps.order_by("order"))
    task = plan.task
    if task is not None and task.status != RamaTask.Status.EXECUTING:
        task.transition_to(RamaTask.Status.EXECUTING)
    multi_step = len(steps) > 1
    # A "yes" while paused confirms exactly the step we paused on.
    step_confirmed_order = (
        plan.cursor
        if plan.status == RamaPendingPlan.Status.AWAITING_STEP_CONFIRM
        else None
    )

    executed: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    dead_items: set[str] = set()
    awaiting = None

    for step in steps:
        if step.order < plan.cursor or step.status != RamaPlanStep.Status.PENDING:
            continue
        if step.item_key in dead_items:
            step.status = RamaPlanStep.Status.SKIPPED
            step.result = {"skipped": f"earlier step for {step.target_label or step.item_key} failed"}
            step.save(update_fields=["status", "result", "updated_at"])
            skipped.append(_step_outcome(step))
            continue
        if (
            multi_step
            and step.requires_own_confirm
            and step.order != step_confirmed_order
        ):
            # Pause: this step needs its own explicit "yes".
            plan.cursor = step.order
            plan.status = RamaPendingPlan.Status.AWAITING_STEP_CONFIRM
            plan.save(update_fields=["cursor", "status", "updated_at"])
            awaiting = {
                "n": step.order + 1,
                "tool": step.tool,
                "target": step.target_label,
                "note": (
                    "This step needs its own confirmation before anything "
                    "else runs."
                ),
            }
            break

        result = execute(step.tool, {**step.arguments, "confirm": "yes"}, landlord=landlord)
        safe_result = json.loads(json.dumps(result, default=str))
        if audit is not None:
            audit(
                {
                    "tool": step.tool,
                    "arguments": {**step.arguments, "confirm": "yes"},
                    "result": safe_result,
                    "auto_confirmed": True,
                    "plan_operation": plan.operation,
                    "plan_step": step.order + 1,
                }
            )
        step.result = safe_result
        if isinstance(result, dict) and result.get("error"):
            step.status = RamaPlanStep.Status.FAILED
            dead_items.add(step.item_key)
            failed.append(_step_outcome(step))
        else:
            step.status = RamaPlanStep.Status.DONE
            if task is not None:
                receipt, _ = record_receipt(
                    task=task,
                    capability_key=step.capability_key or step.tool,
                    inputs=step.arguments,
                    effects=safe_result,
                    verification={
                        "verified": True,
                        "source": "tool_result",
                    },
                )
                step.receipt = receipt
            executed.append(_step_outcome(step))
        update_fields = ["status", "result", "updated_at"]
        if step.receipt_id:
            update_fields.append("receipt")
        step.save(update_fields=update_fields)
        plan.cursor = step.order + 1
        # An executed own-confirm step consumes its confirmation.
        if step.order == step_confirmed_order:
            step_confirmed_order = None
        plan.status = RamaPendingPlan.Status.PENDING_CONFIRM
        plan.save(update_fields=["cursor", "status", "updated_at"])

    if awaiting is None:
        # Finished (fully or partially) — the outstanding-plan row goes away.
        status = "partial" if failed or skipped else "done"
        if task is not None:
            if failed:
                task.transition_to(
                    RamaTask.Status.FAILED,
                    outcome=CommandOutcome(
                        OutcomeKind.FAILED,
                        "The plan completed with failures.",
                        data={
                            "executed": len(executed),
                            "failed": len(failed),
                            "skipped": len(skipped),
                        },
                    ).as_dict(),
                    error="One or more plan steps failed verification.",
                )
            else:
                task.transition_to(
                    RamaTask.Status.VERIFIED,
                    outcome=CommandOutcome(
                        OutcomeKind.COMPLETED,
                        "The confirmed plan completed and was verified.",
                        data={"executed": len(executed)},
                    ).as_dict(),
                )
        plan.delete()
    else:
        status = "awaiting_step"
        if task is not None:
            task.transition_to(
                RamaTask.Status.AWAITING_CONFIRMATION,
                outcome=CommandOutcome(
                    OutcomeKind.PREVIEW,
                    "The next high-risk step needs its own confirmation.",
                    data={"awaiting": awaiting},
                ).as_dict(),
            )

    bits = []
    if executed:
        bits.append(f"{len(executed)} step(s) done")
    if failed:
        bits.append(f"{len(failed)} failed")
    if skipped:
        bits.append(f"{len(skipped)} skipped")
    if awaiting:
        bits.append(f"paused at step {awaiting['n']} ({awaiting['target']}) for its own confirmation")
    summary = "; ".join(bits) or "nothing to do"

    return {
        "task": {
            "id": str(task.pk) if task is not None else None,
            "status": task.status if task is not None else None,
            "outcome": task.outcome if task is not None else None,
        },
        "receipts": [
            {
                "id": str(step.receipt_id),
                "capability": step.capability_key or step.tool,
            }
            for step in steps
            if step.receipt_id
        ],
        "executed": executed,
        "failed": failed,
        "skipped": skipped,
        "awaiting": awaiting,
        "status": status,
        "summary": summary,
    }
