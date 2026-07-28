"""
The autonomy gate: may this turn's previewed writes run without asking?

Why this is a separate module
-----------------------------
It is deliberately NOT in three other places it could have gone:

- not in ``registry.execute`` — that is dumb dispatch and must stay that way;
  it knows nothing about landlord policy, conversations, or audit;
- not in the tool loop in ``service.py`` — the confirm-blanking line there is
  the single most safety-critical line in RAMA and should stay boring;
- not in ``plan_runner`` — that is the executor. The question "should this
  even become a plan the landlord has to approve?" is asked before it.

So the gate is consulted from exactly one place: where a turn's previews are
about to be persisted for confirmation.

What it does NOT change
-----------------------
The model still previews everything, still never sets ``confirm``, and still
cannot approve its own write. When this gate approves a turn, the previews go
through the identical path a landlord's "yes" takes —
``save_batch`` → ``validate_plan`` → ``run_plan`` — so every tool's own
guardrails and blockers re-run against current state. The only thing that
changes is who supplied the "yes", and that answer is a Constitution rule the
landlord confirmed in its own dedicated own_confirm step.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from dataclasses import field
from datetime import timedelta

from django.utils import timezone

from .roles import READ_ONLY_ROLES
from .tool_meta import AUTO_CATEGORIES
from .tool_meta import Autonomy
from .tool_meta import meta_for

# How long an auto-executed action stays undoable. Evaluated at read time, so
# there is no expiry job to run or fall behind.
AUTO_UNDO_TTL = timedelta(hours=24)

# Ceilings applied when the landlord's rule doesn't name its own.
DEFAULT_MAX_PER_TURN = 3
DEFAULT_MAX_PER_DAY = 20
# Chat channels have no undo affordance beyond typing "undo", and a mis-parsed
# text message that writes with no visible receipt is the worst failure mode in
# the design. The landlord can widen this deliberately.
DEFAULT_CHANNELS = ("web",)


@dataclass(frozen=True)
class AutonomyPolicy:
    categories: frozenset[str] = frozenset()
    channels: frozenset[str] = frozenset(DEFAULT_CHANNELS)
    max_per_turn: int = DEFAULT_MAX_PER_TURN
    max_per_day: int = DEFAULT_MAX_PER_DAY
    rule_id: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.categories)


@dataclass(frozen=True)
class AutonomyDecision:
    approved: bool
    reason: str = ""
    policy: AutonomyPolicy | None = None
    specs: list[dict] = field(default_factory=list)


DENIED_NO_POLICY = AutonomyDecision(False, "No autonomy rule in the Constitution.")


# --------------------------------------------------------------- the policy
def policy_for(landlord) -> AutonomyPolicy:
    """The landlord's autonomy grant. No rule → an empty, disabled policy.

    Off by default is the whole point: a landlord who has never opted in can
    never be surprised, however the tools are classified.
    """
    from .constitution import active_rules
    from .models import RamaConstitutionRule

    rule = (
        active_rules(landlord, RamaConstitutionRule.RuleType.AUTONOMY)
        .order_by("-updated_at")
        .first()
    )
    if rule is None:
        return AutonomyPolicy()
    params = rule.params if isinstance(rule.params, dict) else {}

    # Unknown categories are dropped rather than honoured — a typo must
    # narrow autonomy, never widen it.
    categories = frozenset(
        str(c).strip().lower()
        for c in (params.get("categories") or [])
        if str(c).strip().lower() in AUTO_CATEGORIES
    )
    channels = frozenset(
        str(c).strip().lower() for c in (params.get("channels") or DEFAULT_CHANNELS)
    ) or frozenset(DEFAULT_CHANNELS)

    def _cap(key, default):
        try:
            return max(0, int(params.get(key, default)))
        except (TypeError, ValueError):
            return default

    return AutonomyPolicy(
        categories=categories,
        channels=channels,
        max_per_turn=_cap("max_per_turn", DEFAULT_MAX_PER_TURN),
        max_per_day=_cap("max_per_day", DEFAULT_MAX_PER_DAY),
        rule_id=rule.pk,
    )


def actions_today(landlord) -> int:
    """Auto-executed actions in the trailing 24 hours (not a calendar day, so
    the budget can't be reset by a timezone boundary)."""
    from .models import RamaAutoAction

    return RamaAutoAction.objects.filter(
        landlord=landlord, created_at__gte=timezone.now() - timedelta(days=1),
    ).count()


# ----------------------------------------------------------------- the gate
def spec_eligible(landlord, spec: dict, policy: AutonomyPolicy) -> tuple[bool, str]:
    """Whether one previewed write may run unattended."""
    if spec.get("kind") != "single":
        # Playbook plans are bulk operations over many entities. Never auto.
        return False, "multi-step plans always need confirmation"

    tool = spec.get("tool") or ""
    meta = meta_for(tool)
    if meta.autonomy != Autonomy.OPT_IN:
        return False, f"{tool} always needs confirmation"
    if meta.auto_category not in policy.categories:
        return False, f"{meta.auto_category or 'uncategorised'} is not pre-authorised"
    if meta.undo is None:
        # test_autonomy guarantees this can't happen; belt and braces, because
        # the cost of being wrong is an unundoable surprise write.
        return False, f"{tool} has no undo"

    arguments = spec.get("arguments") or {}
    # An unresolved target means the tool was about to act on a guess.
    for ambiguous in ("options", "candidates", "needs_input"):
        if arguments.get(ambiguous):
            return False, f"{tool} target is ambiguous"

    if meta.auto_guard is not None:
        safe = {k: v for k, v in arguments.items() if k != "confirm"}
        refusal = meta.auto_guard(landlord, **safe)
        if refusal:
            return False, refusal
    return True, ""


def evaluate_turn(
    landlord,
    pending_specs: list[dict],
    *,
    role: str,
    channel: str,
    had_pending_plan: bool,
) -> AutonomyDecision:
    """Decide whether this whole turn's previews may execute unattended.

    All-or-nothing by design. Splitting a turn into "these ran, confirm those"
    would break the promise save_batch makes verbatim — that one "yes" runs
    every change the landlord was shown — and produce a reply that is half
    receipt and half preview. A mixed turn simply falls back to today's
    behaviour.
    """
    if not pending_specs:
        return AutonomyDecision(False, "nothing to run")
    if role in READ_ONLY_ROLES:
        # Analyst roles report; they never act. Their tool sets are read-only
        # anyway — this is the explicit statement that nothing writes while the
        # landlord is asleep. Gating on role rather than channel matters:
        # _delegate runs corporal sub-turns with channel="system" and those are
        # interactive.
        return AutonomyDecision(False, f"the {role} agent never writes")
    if had_pending_plan:
        # Never mix an unattended write into an unresolved confirmation.
        return AutonomyDecision(False, "a confirmation is already outstanding")

    policy = policy_for(landlord)
    if not policy.enabled:
        return DENIED_NO_POLICY
    if channel not in policy.channels:
        return AutonomyDecision(False, f"autonomy is not enabled for {channel}", policy)
    if len(pending_specs) > policy.max_per_turn:
        return AutonomyDecision(
            False, f"more than {policy.max_per_turn} changes in one turn", policy,
        )

    for spec in pending_specs:
        ok, why = spec_eligible(landlord, spec, policy)
        if not ok:
            return AutonomyDecision(False, why, policy)

    used = actions_today(landlord)
    if used + len(pending_specs) > policy.max_per_day:
        return AutonomyDecision(
            False, f"daily limit of {policy.max_per_day} reached", policy,
        )
    return AutonomyDecision(True, "", policy, list(pending_specs))


# ------------------------------------------------------------------- undo
def undo_pair(tool: str, arguments: dict, result: dict) -> tuple[str, dict] | None:
    """The inverse call for something already executed, or None if it can no
    longer be reversed. Never raises — a broken inverse must degrade to "not
    undoable", not break the turn that just succeeded."""
    meta = meta_for(tool)
    if meta.undo is None:
        return None
    try:
        pair = meta.undo(arguments or {}, result or {})
    except Exception:  # noqa: BLE001 - a bad inverse must not break the turn
        return None
    if not pair:
        return None
    name, args = pair
    return name, {k: v for k, v in (args or {}).items() if k != "confirm"}


def record_auto_actions(
    landlord,
    conversation_id,
    progress: dict,
    policy: AutonomyPolicy | None,
) -> list[dict]:
    """Write one receipt per successfully auto-executed step.

    Only `executed` steps are recorded: a failed step changed nothing, so
    offering to undo it would be a lie.
    """
    from .models import RamaAutoAction

    receipts: list[dict] = []
    for outcome in (progress or {}).get("executed") or []:
        tool = outcome.get("tool") or ""
        arguments = outcome.get("arguments") or {}
        result = outcome.get("result") or {}
        pair = undo_pair(tool, arguments, result)
        row = RamaAutoAction.objects.create(
            landlord=landlord,
            conversation_id=conversation_id,
            tool=tool,
            arguments=json.loads(json.dumps(arguments, default=str)),
            target_label=str(outcome.get("target") or "")[:200],
            result=json.loads(json.dumps(result, default=str)),
            policy_rule_id=policy.rule_id if policy else None,
            undo_tool=pair[0] if pair else "",
            undo_arguments=pair[1] if pair else {},
        )
        receipts.append(
            {
                "id": str(row.pk),
                "tool": row.tool,
                "target": row.target_label,
                "undoable": bool(row.undo_tool),
            },
        )
    return receipts


def undoable_actions(landlord, conversation_id=None):
    """Auto-actions still within the undo window, newest first."""
    from .models import RamaAutoAction

    qs = RamaAutoAction.objects.filter(
        landlord=landlord,
        status=RamaAutoAction.Status.DONE,
        created_at__gte=timezone.now() - AUTO_UNDO_TTL,
    ).exclude(undo_tool="")
    if conversation_id is not None:
        qs = qs.filter(conversation_id=conversation_id)
    return qs


def undo_action(action, landlord, audit=None) -> dict:
    """Reverse one auto-executed action.

    Runs the stored inverse through save_single + run_plan rather than calling
    registry.execute directly, so run_plan() remains the ONLY site in the
    codebase that injects confirm=yes.
    """
    from .models import RamaAutoAction
    from .plan_runner import run_plan
    from .plan_runner import save_single

    if action.status != RamaAutoAction.Status.DONE:
        return {"error": f"That action is already {action.get_status_display().lower()}."}
    if not action.undo_tool:
        return {"error": "That action can't be undone."}
    if timezone.now() - action.created_at > AUTO_UNDO_TTL:
        action.status = RamaAutoAction.Status.EXPIRED
        action.save(update_fields=["status"])
        return {"error": "That action is too old to undo automatically."}

    # A throwaway conversation id: save_single() clears whatever plan the
    # conversation currently holds, and an undo must never destroy a
    # confirmation the landlord still has outstanding. run_plan deletes the
    # row when it finishes, so nothing is left behind.
    plan = save_single(landlord, uuid.uuid4(), action.undo_tool, action.undo_arguments)
    progress = run_plan(plan, landlord, audit=audit)
    if progress.get("failed") or not progress.get("executed"):
        action.status = RamaAutoAction.Status.UNDO_FAILED
        action.save(update_fields=["status"])
        detail = ""
        for failure in progress.get("failed") or []:
            detail = str((failure.get("result") or {}).get("error") or "")
            break
        return {"error": f"Couldn't undo that. {detail}".strip()}

    action.status = RamaAutoAction.Status.UNDONE
    action.undone_at = timezone.now()
    action.save(update_fields=["status", "undone_at"])
    return {
        "undone": True,
        "tool": action.tool,
        "target": action.target_label,
        "progress": progress,
    }
