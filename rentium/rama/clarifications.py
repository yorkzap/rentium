"""Durable, provider-independent continuation of tool clarifications.

A tool result may need one fact that only the landlord can supply.  The tool
owns that contract; chat prose does not.  These helpers persist the exact tool
and its already-resolved arguments, bind the question to the visible prompt,
and deterministically turn the next short reply into the missing field.
"""

from __future__ import annotations

import re
from datetime import timedelta

from django.utils import timezone

from .command_engine import create_task
from .models import RamaTask
from .outcomes import CommandOutcome
from .outcomes import OutcomeKind

CLARIFICATION_TTL = timedelta(days=7)
_TOKEN = re.compile(r"[a-z0-9]+")


def _field_for(result: dict) -> str:
    contract = result.get("clarification") or {}
    if contract.get("field"):
        return str(contract["field"])
    if result.get("needs"):
        return str(result["needs"])
    if result.get("missing_field"):
        return str(result["missing_field"])
    missing = result.get("missing") or []
    if isinstance(missing, (list, tuple)) and len(missing) == 1:
        return str(missing[0])
    return ""


def persist_tool_clarification(  # noqa: PLR0913 - explicit persistence context
    *,
    landlord,
    conversation_id,
    episode,
    source_message,
    tool: str,
    arguments: dict,
    result: dict,
) -> RamaTask | None:
    """Persist a resumable tool question, or None for an unstructured one."""
    # Existing composite workflows may own multi-stage, state-backed
    # clarification logic richer than a single missing field.  They opt into
    # this generic path by returning the explicit contract; merely having a
    # human-facing question is not enough to steal control from them.
    if not isinstance(result.get("clarification"), dict):
        return None
    question = str(result.get("question_for_user") or "").strip()
    field = _field_for(result)
    if not question or not field:
        return None

    context = {
        "clarification": {
            **dict(result.get("clarification") or {}),
            "field": field,
            "question": question,
        },
    }
    task = create_task(
        landlord=landlord,
        conversation_id=conversation_id,
        capability_key=tool,
        inputs=dict(arguments or {}),
        context=context,
        episode=episode,
        source_message=source_message,
        expires_at=timezone.now() + CLARIFICATION_TTL,
    )
    task.subject = tool
    task.save(update_fields=["subject", "updated_at"])
    task.transition_to(
        RamaTask.Status.NEEDS_INPUT,
        outcome=CommandOutcome(
            OutcomeKind.NEEDS_INPUT,
            question,
            data={"field": field},
            missing=(field,),
        ).as_dict(),
    )
    return task


def bind_prompt(tasks: list[RamaTask], message) -> None:
    for task in tasks:
        task.episode = message.episode
        task.active_prompt = message
        task.expires_at = timezone.now() + CLARIFICATION_TTL
        task.save(
            update_fields=["episode", "active_prompt", "expires_at", "updated_at"],
        )


def prompted_tasks(*, landlord, conversation_id, prompt_id: str) -> list[RamaTask]:
    if not prompt_id:
        return []
    return list(
        RamaTask.objects.filter(
            landlord=landlord,
            conversation_id=conversation_id,
            status=RamaTask.Status.NEEDS_INPUT,
            active_prompt_id=prompt_id,
            expires_at__gt=timezone.now(),
        ).order_by("created_at"),
    )


def _normal(value: str) -> str:
    return " ".join(_TOKEN.findall(str(value or "").casefold()))


def answer_for(task: RamaTask, answer: str) -> tuple[str, str] | None:
    """Return (field, value) only when the reply resolves one unique value."""
    contract = dict((task.context or {}).get("clarification") or {})
    field = str(contract.get("field") or "").strip()
    said = _normal(answer)
    if not field or not said:
        return None

    choices = contract.get("choices") or {}
    if isinstance(choices, dict) and choices:
        matches = []
        for value, aliases in choices.items():
            candidates = [value, *(aliases or [])]
            if any(_normal(alias) in said for alias in candidates if _normal(alias)):
                matches.append(str(value))
        return (field, matches[0]) if len(set(matches)) == 1 else None

    options = contract.get("options") or []
    if isinstance(options, list) and options:
        said_tokens = set(_TOKEN.findall(said))
        matches = []
        for option in options:
            corpus = " ".join(
                [
                    str(option.get("value") or ""),
                    str(option.get("label") or ""),
                    *[str(a) for a in (option.get("aliases") or [])],
                ],
            )
            corpus_normal = _normal(corpus)
            corpus_tokens = set(_TOKEN.findall(corpus_normal))
            if said in corpus_normal or (said_tokens and said_tokens <= corpus_tokens):
                matches.append(str(option.get("value") or ""))
        matches = [value for value in dict.fromkeys(matches) if value]
        return (field, matches[0]) if len(matches) == 1 else None

    # A field with no enumerated choices is deliberately free text.  Validation
    # remains with the capability when it is re-run.
    return field, str(answer or "").strip()


def finish(task: RamaTask, *, message: str, preview: bool = False) -> None:
    # The task state machine deliberately forbids jumping from a question to a
    # claimed success.  Resolving the answer is real command-engine progress:
    # validated input -> resumed execution -> verified hand-off.  A separate
    # plan task owns any confirmation that follows.
    task.transition_to(RamaTask.Status.READY)
    task.transition_to(RamaTask.Status.EXECUTING)
    task.transition_to(
        RamaTask.Status.VERIFIED,
        outcome=CommandOutcome(
            OutcomeKind.ANSWER,
            message,
            data={"continued_to_preview": preview},
        ).as_dict(),
    )


def cancel(tasks: list[RamaTask]) -> None:
    for task in tasks:
        task.transition_to(RamaTask.Status.CANCELLED)
