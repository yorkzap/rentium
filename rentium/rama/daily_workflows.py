"""Compose, inspect and resume daily work using the existing plan runner."""

import json

from django.db import transaction

from .workflow_contracts import WORKFLOW_TOOLS
from .workflow_contracts import enabled
from .workflow_contracts import freeze_arguments

MAX_WORKFLOW_STEPS = 12


def _invalid(message):
    raise ValueError(message)


def prepare_tenant_move(  # noqa: C901, PLR0911, PLR0913
    landlord,
    tenant: str,
    from_property: str,
    to_property: str,
    start_date: str,
    total_rent: str = "",
    confirm: str = "",
):
    """Prepare a move agreement and replacement draft lease, never terminate now.

    This ONE action creates BOTH records. Do not add a separate create_lease
    action for its destination. The replacement is a month-to-month draft.
    Approval signs/proposes the landlord's mutual move-out agreement through the
    existing service and its tenant notification. The tenant must still consent.
    Does not transfer deposits, forgive balances, or sign the replacement lease.
    """
    from datetime import date
    from datetime import timedelta

    from django.db.models import Q

    from rentium.leases.models import Lease
    from rentium.leases.models import LeaseTenant
    from rentium.leases.moveout_services import propose_mutual_moveout

    from .domain_crud import _confirmed
    from .domain_crud import _preview
    from .domain_crud import create_lease
    from .resolve import resolve_property

    source, error = resolve_property(landlord, from_property)
    if error:
        return {"error": error}
    destination, error = resolve_property(landlord, to_property)
    if error:
        return {"error": error}
    if source.pk == destination.pk:
        return {"error": "The destination is already the current offering."}
    people = list(
        LeaseTenant.objects.filter(
            lease__landlord=landlord,
            lease__property=source,
            lease__status__in=[
                Lease.LeaseStatus.ACTIVE,
                Lease.LeaseStatus.PENDING_SIGNATURES,
            ],
            declined=False,
        )
        .filter(
            Q(invited_email__iexact=tenant)
            | Q(invited_name__iexact=tenant)
            | Q(tenant__user__email__iexact=tenant)
            | Q(tenant__user__name__iexact=tenant),
        )
        .select_related("lease"),
    )
    if len(people) != 1:
        return {
            "needs_input": True,
            "question_for_user": (
                "Identify the exact tenant and current lease for this move; the "
                "supplied name did not resolve uniquely."
            ),
        }
    old = people[0].lease
    if old.lease_tenants.filter(declined=False).count() != 1:
        return {
            "needs_input": True,
            "question_for_user": (
                "This is a shared lease. Is the whole household moving, or only "
                "this tenant? A single-person move cannot end everyone's lease."
            ),
        }
    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return {
            "needs_input": True,
            "question_for_user": "What exact date should the new tenancy start?",
        }
    from django.utils import timezone

    if start <= timezone.localdate() or start <= old.start_date:
        return {
            "error": (
                "A planned move needs a future start date after the current lease"
                " begins."
            ),
        }
    if (
        destination.leases.filter(status__in=["DRAFT", "PENDING", "ACTIVE"])
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=start))
        .exists()
    ):
        return {
            "error": (
                "The destination has a potentially overlapping lease. Resolve "
                "availability before preparing this move."
            ),
        }
    if old.moveout_requests.filter(status="PENDING").exists():
        return {
            "error": (
                "A move-out agreement is already pending. Complete that existing "
                "agreement before preparing another."
            ),
        }
    rent = total_rent or str(old.total_rent)
    arguments = {
        "property_query": str(destination.pk),
        "start_date": str(start),
        "total_rent": rent,
        "is_month_to_month": "1",
    }
    lease_preview = create_lease(landlord, **arguments)
    if not lease_preview.get("needs_confirm"):
        return lease_preview
    preview = {
        "tenant": people[0].display_name,
        "current_lease": old.lease_number,
        "destination_id": str(destination.pk),
        "destination": destination.name,
        "proposed_end": str(start - timedelta(days=1)),
        "new_lease": lease_preview["preview"],
        "effects": [
            (
                "Sign and propose a mutual move-out agreement using the existing "
                "tenant-notification workflow"
            ),
            "Create the replacement draft lease",
            (
                "Wait for tenant consent and normal new-lease signing; current "
                "tenancy stays in force"
            ),
        ],
        "balances": (
            "Existing deposits and outstanding balances remain on the original lease."
        ),
    }
    if not _confirmed(confirm):
        result = _preview(
            "prepare_tenant_move",
            preview,
            (
                "Review the agreement and new lease together. This does not "
                "complete a move."
            ),
        )
        result["resolved_arguments"] = {
            "tenant": people[0].invited_email or tenant,
            "from_property": str(source.pk),
            "to_property": str(destination.pk),
            "start_date": str(start),
            "total_rent": rent,
        }
        return result
    with transaction.atomic():
        created = create_lease(landlord, **arguments, confirm="yes")
        if created.get("error"):
            transaction.set_rollback(True)
            return created
        agreement = propose_mutual_moveout(
            landlord=landlord,
            lease=old,
            requested_end=start - timedelta(days=1),
            reason=f"Proposed move to {destination.name}",
            actor=landlord.user,
        )
    return {
        "created": True,
        "lease": created["lease"],
        "moveout_id": str(agreement.pk),
        "source_lease_id": str(old.pk),
        "workflow_waiting": (
            "Awaiting agreement acceptance and replacement lease signing/activation."
        ),
    }


def onboarding_plan(landlord, values):
    """Replace the opaque composite with individually persisted dependent steps."""
    from .resolve import resolve_property

    name = values["room_name"]
    prop, error = resolve_property(landlord, name)
    if error:
        return {
            "needs_input": True,
            "question_for_user": (
                "Select the exact offering for onboarding. Create any new "
                "holding/unit/room first through the property-structure workflow."
            ),
        }
    if not values.get("start_date") or not values.get("total_rent"):
        return {
            "needs_input": True,
            "question_for_user": (
                "Provide the tenancy start date and monthly rent so I can prepare"
                " the complete onboarding."
            ),
        }
    args = {
        key: values[key]
        for key in (
            "start_date",
            "end_date",
            "total_rent",
            "security_deposit",
            "pet_deposit",
            "cleaning_deposit",
            "special_terms",
            "smoking_allowed",
            "pets_allowed",
        )
        if values.get(key) not in (None, "")
    }
    args.update(
        {
            "property_query": str(prop.pk),
            "is_month_to_month": "0" if values.get("end_date") else "1",
        },
    )
    steps = [{"step_id": "lease", "tool": "create_lease", "arguments": args}]
    email = values.get("tenant_email")
    if email:
        binding = {"$step": "lease", "path": "lease.lease_number"}
        steps.append(
            {
                "step_id": "invite",
                "tool": "invite_tenant_to_lease",
                "depends_on": ["lease"],
                "arguments": {
                    "lease_number": binding,
                    "email": email,
                    "name": values.get("tenant_name") or email,
                    "rent_amount": values["total_rent"],
                },
            },
        )
        if values.get("create_inspection") in {"1", "yes", "true"}:
            steps.append(
                {
                    "step_id": "inspection",
                    "tool": "create_condition_inspection",
                    "depends_on": ["invite"],
                    "arguments": {"lease_number": binding, "tenant_email": email},
                },
            )
    return plan_daily_workflow(
        landlord,
        json.dumps(steps),
        f"Prepare onboarding for {name}",
    )


def plan_daily_workflow(  # noqa: C901, PLR0912
    landlord,
    steps_json: str,
    summary: str,
    confirm: str = "",
) -> dict:
    """Prepare one daily workflow or combine supported actions; never executes.

    steps_json is a JSON list of {step_id, tool, arguments, depends_on}.
    Supported actions: record_payment, create_expense, reconcile_bank_statement,
    apply_rent_adjustment, prepare_tenant_move, create_lease, invite_tenant_to_lease,
    create_condition_inspection, create_property, update_property,
    create_property_structure, update_unit_layout, set_unit_rental_mode.
    Use {$step: earlier_step_id, path: lease.lease_number} for outputs of
    earlier steps. Get IDs/facts from live tools. Include invitations only if
    requested. Every independent action is previewed; related actions use the
    domain's validators after dependency resolution. A fresh plan always needs
    approval. Do not call this tool again to execute it.
    """
    from .plan_runner import _argument_step_refs
    from .plan_runner import validate_plan
    from .registry import execute
    from .tool_meta import meta_for

    if not enabled(landlord):
        return {"error": "Daily workflows are not enabled for this portfolio."}
    try:
        steps = json.loads(steps_json)
        if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
            _invalid("Provide between 1 and 12 supported actions.")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("arguments", {}), dict)
            for item in steps
        ):
            _invalid("Each action needs an object with tool and arguments.")
        errors = validate_plan(steps, landlord)
        if errors:
            _invalid(" ".join(errors))
        prepared = []
        previews = []
        if any(item.get("tool") == "create_condition_inspection" for item in steps):
            from rentium.leases.inspections import InspectionTemplate

            if not InspectionTemplate.objects.filter(is_active=True).exists():
                return {
                    "error": (
                        "Condition inspection templates are unavailable. No onboarding "
                        "actions ran; restore the templates before preparing "
                        "this workflow."
                    ),
                }
        for index, item in enumerate(steps, start=1):
            tool = item["tool"]
            if tool not in WORKFLOW_TOOLS:
                _invalid(
                    f"{tool} has no verified daily pathway. No actions were executed.",
                )
            args = dict(item.get("arguments") or {})
            args.pop("confirm", None)
            step = {
                "step_id": item.get("step_id") or f"step-{index}",
                "tool": tool,
                "arguments": args,
                "depends_on": list(item.get("depends_on") or []),
                "target": item.get("target") or tool.replace("_", " "),
                "item_key": "daily-workflow",
                "requires_own_confirm": meta_for(tool).own_confirm,
            }
            if not _argument_step_refs(args):
                preview = execute(tool, {**args, "confirm": ""}, landlord=landlord)
                if (
                    preview.get("error")
                    or preview.get("needs_input")
                    or preview.get("question_for_user")
                ):
                    return {**preview, "draft_steps": steps, "summary": summary}
                if not preview.get("needs_confirm"):
                    _invalid(f"{tool} did not provide an actionable preview.")
                args.update(preview.get("resolved_arguments") or {})
                effects = preview.get("preview") or {}
                if tool == "create_expense":
                    for field in ("amount", "effective_date", "paid_on", "category"):
                        args[field] = effects.get(field) or ""
                if tool == "record_payment" and effects.get("payment_date"):
                    args["payment_date"] = effects["payment_date"]
                step["arguments"], step["facts_hash"] = freeze_arguments(
                    tool,
                    args,
                    landlord,
                )
                previews.append(
                    {
                        "step_id": step["step_id"],
                        "effects": preview.get("preview", preview),
                    },
                )
            else:
                previews.append(
                    {
                        "step_id": step["step_id"],
                        "effects": args,
                        "after": step["depends_on"],
                    },
                )
            prepared.append(step)
        move_destinations = {
            step["arguments"].get("to_property")
            for step in prepared
            if step["tool"] == "prepare_tenant_move"
        }
        if any(
            step["tool"] == "create_lease"
            and step["arguments"].get("property_query") in move_destinations
            for step in prepared
        ):
            _invalid(
                "prepare_tenant_move already creates the replacement draft. "
                "Remove the separate create_lease for that destination and "
                "submit the complete corrected plan.",
            )
        errors = validate_plan(prepared, landlord)
        if errors:
            _invalid(" ".join(errors))
    except (ValueError, TypeError, KeyError) as exc:
        return {"error": str(exc)}
    return {
        "needs_confirm": True,
        "plan": {
            "operation": "daily_workflow",
            "summary": summary[:500],
            "steps": prepared,
            "blocked": [],
            "previews": previews,
        },
        "preview": {"actions": previews},
        "relay_instruction": (
            "Show every action and its effects, then wait for the persisted "
            "plan approval. Never say the workflow has already run."
        ),
    }


def task_payload(task):
    receipts = list(task.receipts.all())
    progress = task.progress or []
    finished = {row["step_id"] for row in progress if row["status"] == "DONE"}
    uncertain = any(
        row.get("result", {}).get("partial") or row.get("result", {}).get("waiting")
        for row in progress
    )
    unfinished = any(
        step.get("step_id", f"step-{index}") not in finished
        for index, step in enumerate(task.input.get("steps") or [], start=1)
    )
    return {
        "id": str(task.pk),
        "parent_id": str(task.parent_id) if task.parent_id else None,
        "workflow": task.workflow_key,
        "version": task.workflow_version,
        "status": task.status,
        "stage": task.stage,
        "detail": task.error or (task.outcome or {}).get("message", ""),
        "summary": (task.input or {}).get("summary")
        or task.capability_key.replace("_", " "),
        "updated_at": task.updated_at.isoformat(),
        "steps": [
            {
                "step_id": row["step_id"],
                "tool": row["tool"],
                "status": row["status"],
                "verified": bool(row.get("verification", {}).get("verified")),
                "detail": row.get("result", {}).get("error")
                or row.get("result", {}).get("detail")
                or "",
            }
            for row in progress
        ],
        "receipts": [
            {
                "id": str(row.pk),
                "verified": row.verification.get("verified", False),
                "links": row.links,
                "entity_refs": row.entity_refs,
            }
            for row in receipts
        ],
        "can_resume": task.status
        in {"FAILED", "PARTIAL", "SUSPENDED", "EXPIRED", "NEEDS_INPUT"}
        and unfinished
        and not uncertain
        and not task.attempts.exclude(capability_key="deliver_tenant_invitation")
        .exclude(status="CANCELLED")
        .exists(),
    }


@transaction.atomic
def resume_task(landlord, task_id, conversation_id):  # noqa: C901
    """New approval for unfinished effects; an old approval is never reused."""
    from .models import RamaTask
    from .plan_runner import _resolve_argument_refs
    from .plan_runner import save_plan

    task = RamaTask.objects.select_for_update().get(pk=task_id, landlord=landlord)
    if not task_payload(task)["can_resume"]:
        _invalid("This task cannot be resumed in its current state.")
    if task.attempts.exclude(status__in=RamaTask.TERMINAL_STATUSES).exists():
        _invalid(
            "This workflow already has an unfinished retry. Open that task.",
        )
    progress = {row["step_id"]: row for row in task.progress}
    completed = {
        key: row["result"] for key, row in progress.items() if row["status"] == "DONE"
    }
    steps = []
    for index, original in enumerate(task.input.get("steps") or [], start=1):
        step = dict(original)
        key = step.get("step_id") or f"step-{index}"
        if key in completed:
            continue
        prior = progress.get(key) or {}
        if prior.get("result", {}).get("partial") or prior.get("result", {}).get(
            "waiting",
        ):
            _invalid(
                (
                    "This action may already have effects. Review its records before "
                    "preparing a correction; it will not be replayed."
                ),
            )

        def bind(value):
            if isinstance(value, dict):
                if value.get("$step") in completed:
                    return _resolve_argument_refs(value, completed)
                return {k: bind(v) for k, v in value.items()}
            if isinstance(value, list):
                return [bind(v) for v in value]
            return value

        step["arguments"] = bind(step.get("arguments") or {})
        step["depends_on"] = [
            dep for dep in step.get("depends_on", []) if dep not in completed
        ]
        step.pop("facts_hash", None)
        steps.append(step)
    if not steps:
        _invalid("All actions already completed. Nothing will be repeated.")
    proposal = plan_daily_workflow(
        landlord,
        json.dumps(steps),
        f"Resume: {task.input.get('summary', task.capability_key)}",
    )
    if "plan" not in proposal:
        _invalid(
            proposal.get("error")
            or proposal.get("question_for_user")
            or "Additional information is required before retrying.",
        )
    plan = save_plan(landlord, conversation_id, proposal["plan"])
    plan.task.parent = task
    plan.task.save(update_fields=["parent", "updated_at"])
    return plan


@transaction.atomic
def prepare_attention(landlord):  # noqa: C901
    """In-app only, deterministic and deduplicated; no business effects."""
    from rentium.leases.models import Lease
    from rentium.ledger.models import BankStatement

    from .models import RamaInsight
    from .models import RamaTask

    if not enabled(landlord):
        return 0
    # Serialize sweeps for this owner; retrying cannot create duplicate cards.
    type(landlord).objects.select_for_update().get(pk=landlord.pk)
    findings = {}
    for statement in BankStatement.objects.filter(
        batch__landlord=landlord,
    ).select_related("batch"):
        remaining = statement.transactions.filter(result={}).count()
        if remaining:
            findings[f"statement:{statement.batch_id}"] = {
                "summary": (
                    f"Review {remaining} unresolved bank rows for {statement.account}."
                ),
                "batch_id": str(statement.batch_id),
                "path": "/dashboard/financial",
            }
    for lease in Lease.objects.filter(
        landlord=landlord,
        status__in=["DRAFT", "PENDING"],
    ):
        findings[f"onboarding:{lease.pk}"] = {
            "summary": (
                f"Finish onboarding for lease {lease.lease_number}; "
                "check missing terms and signatures."
            ),
            "lease_id": str(lease.pk),
            "path": "/dashboard/leases",
        }
    for task in RamaTask.objects.filter(
        landlord=landlord,
        workflow_key__gt="",
        parent__isnull=True,
        status__in=["PARTIAL", "FAILED", "WAITING", "SUSPENDED"],
    ):
        if (
            task.attempts.exclude(capability_key="deliver_tenant_invitation")
            .exclude(status="CANCELLED")
            .exists()
        ):
            continue
        findings[f"task:{task.pk}"] = {
            "summary": task_payload(task)["summary"],
            "task_id": str(task.pk),
        }
    existing = {
        row.facts.get("key"): row
        for row in RamaInsight.objects.filter(
            landlord=landlord,
            kind="rama.workflow.attention",
        )
    }
    for key, facts in findings.items():
        row = existing.get(key)
        if row:
            if row.status not in {
                RamaInsight.Status.DISMISSED,
                RamaInsight.Status.ACKED,
            }:
                row.status = RamaInsight.Status.OPEN
            row.facts = {"key": key, **facts, "in_app_only": True}
            row.analysis = facts["summary"]
            row.save(update_fields=["facts", "analysis", "status", "updated_at"])
        else:
            RamaInsight.objects.create(
                landlord=landlord,
                kind="rama.workflow.attention",
                facts={"key": key, **facts, "in_app_only": True},
                analysis=facts["summary"],
            )
    for key, row in existing.items():
        if key not in findings and row.status != RamaInsight.Status.DISMISSED:
            row.status = RamaInsight.Status.ACTIONED
            row.save(update_fields=["status", "updated_at"])
    return len(findings)
