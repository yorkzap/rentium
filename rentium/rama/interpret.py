"""
Asking the model what the landlord MEANT — safely.

WHY THIS EXISTS
---------------
RAMA understood "came out of the bank" and "cleared the bank" but not "it's
left the bank", so the answer to its own question read as no answer, the turn
fell through to prose, and a guard retracted the lot. The instinct was to add
another alternation to the regex. The landlord's objection to that is correct
and is the reason for this module:

    "we cannot come up with each wording or pattern for this. RAMA should be
     smart enough to know what it means. That's the whole point of an LLM."

Enumerating phrasings is a losing game. There is no finite list of ways to say
that money has left an account, and every miss looks to the landlord like RAMA
being stupid about a sentence a child would understand.

WHAT MAKES THIS SAFE
--------------------
The danger with "let the model decide" is that a model asked an open question
invents a plausible answer, and an invented answer about money is worse than
no answer. Every property below exists to make that impossible rather than
unlikely:

  1. CLOSED SET. The model chooses from options the caller listed. Anything
     else — a sentence, a number, a near-miss, an empty string — is discarded
     and the call returns None. The model cannot widen its own output space.
  2. NO FACTS. It classifies; it never supplies. Amounts, names, dates and
     ids are read from the database by the caller, never from this reply. The
     worst a wrong answer can do is route to the wrong branch, and the branch
     it routes to still previews and still asks.
  3. NO TOOLS. The call is made with an empty tool list, so an interpretation
     can never become a write. It has no reach.
  4. DEGRADES, NEVER BREAKS. No key, no provider, a timeout, an outage, a
     refusal: all return None, and every caller has a deterministic fallback.
     The system gets less smart, not less correct.
  5. ABSTAINS. "unclear" is always an available answer and is not a failure —
     asking the landlord one plain question beats guessing between two.
  6. AUDITED. Question, message and answer land in RamaAudit, so a decision
     the landlord disputes can be read back.

What has NOT changed: the model still cannot execute anything, confirm
anything, or put a figure on a record. Interpretation decides which
deterministic path runs. The path itself is Python, and the landlord's yes is
still required at the end of it.

USING IT
--------
    from .interpret import classify

    answer = classify(
        landlord,
        question="Is the landlord saying the money has already left their bank?",
        message=message,
        options=("paid", "unpaid", "unclear"),
    )

Keep `options` small, mutually exclusive, and named for what they mean rather
than for what the caller will do with them. Always include an abstain option.
Always keep the deterministic fallback for when this returns None.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# NOT SOLVED HERE: the provider contract (providers/base.py) takes no timeout,
# so a hanging upstream hangs this call as it would any other RAMA turn. One
# classification is a handful of tokens, so it is a small addition to a turn
# that already makes provider calls — but it IS an addition, and the fix
# belongs in the provider interface where it would cover every call site.

_SYSTEM = (
    "You interpret one short message from a landlord using a Canadian "
    "property-management app, and you do nothing else.\n\n"
    "Answer with EXACTLY ONE of the option words given to you. No punctuation, "
    "no explanation, no sentence — the single word and nothing more.\n\n"
    "You are reading intent only. Never infer or supply amounts, dates, names, "
    "or any other fact: something else has already read those from the "
    "database. If the message does not clearly mean any of the options, answer "
    "with the abstain option. Abstaining is correct and useful — a wrong guess "
    "about money costs the landlord real money, and abstaining only costs them "
    "one plain question."
)


def _clean(raw: str) -> str:
    text = (raw or "").strip().casefold()
    return "".join(ch for ch in text if ch.isalnum() or ch in "_- ")


def classify(
    landlord,
    *,
    question: str,
    message: str,
    options: tuple[str, ...],
    context: str = "",
    conversation_id=None,
) -> str | None:
    """One of `options`, or None when the model could not be asked or strayed.

    None means "no opinion" in every failure mode there is — unconfigured,
    unreachable, slow, or off-contract — so a caller can always treat it as
    "fall back to the deterministic reading" without distinguishing why.
    """
    text = (message or "").strip()
    if not text or not options:
        return None
    if not getattr(settings, "RAMA_SEMANTIC_INTERPRETATION", True):
        return None

    from .providers import get_provider
    from .providers.base import ProviderError
    from .runtime import get_landlord_config

    try:
        config = get_landlord_config(landlord)
    except Exception:  # noqa: BLE001 — interpretation must never break a turn
        logger.exception("interpret: could not resolve landlord config")
        return None
    if not config.enabled or not config.api_key:
        return None

    allowed = tuple(_clean(o) for o in options)
    prompt = (
        f"{question}\n\n"
        + (f"Context: {context}\n\n" if context else "")
        + f"The landlord said: {text[:600]!r}\n\n"
        + "Options: "
        + ", ".join(allowed)
        + "\n\nAnswer with one option word only."
    )

    try:
        provider = get_provider(config.provider)
        turn = provider.complete(
            model=config.model,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            # No tools. An interpretation can never become a write.
            tools=[],
            api_key=config.api_key,
        )
    except (ProviderError, Exception):  # noqa: BLE001 — see docstring
        logger.exception("interpret: provider call failed")
        return None

    answer = _clean(getattr(turn, "text", ""))
    # A model that ignored "one word only" and wrote a sentence is not trusted
    # to have meant the option buried in it — "probably paid, but check with
    # them" is not a decision. Only an exact match counts.
    chosen = answer if answer in allowed else None

    _audit(landlord, conversation_id, question, text, answer, chosen)
    if chosen is None:
        return None
    # Hand back the caller's own spelling, not the normalised one.
    return options[allowed.index(chosen)]


def _audit(landlord, conversation_id, question, message, raw, chosen) -> None:
    """A decision the landlord disputes has to be readable afterwards.

    RamaAudit rows are keyed to a conversation, so a caller that has none has
    nowhere to file this. That is a caller bug rather than a reason to lose the
    turn: log it loudly here and keep going, so an unthreaded call site shows
    up in the logs instead of quietly costing the audit trail.
    """
    if conversation_id is None:
        logger.warning(
            "interpret: no conversation_id — classification not audited (%r → %r)",
            question,
            chosen,
        )
        return
    try:
        from .models import RamaAudit

        RamaAudit.objects.create(
            landlord=landlord,
            conversation_id=conversation_id,
            kind=RamaAudit.Kind.TOOL_CALL,
            content={
                "tool": "interpret.classify",
                "arguments": {"question": question, "message": message[:300]},
                "result": {"raw": raw[:60], "chosen": chosen},
            },
        )
    except Exception:  # noqa: BLE001 — never let logging break a turn
        logger.exception("interpret: audit failed")
