"""
Reading an uploaded form well enough to ASK the right question about it.

When a landlord drops an unfamiliar PDF into the app — or sends one to RAMA on
Telegram — the system knows nothing about it. It could be a pet addendum that
has to be signed before the lease takes effect, an RTB-8 that ends a tenancy, or
a strata bylaw sheet that is signed whenever. Those three have completely
different consequences, and picking wrong is worse than not picking at all: a
form silently classified as WITH_LEASE would block a lease from activating, and
one silently classified as MOVE_OUT would offer to end a tenancy.

So this module never decides. It reads the OCR text and the PDF's own form-field
names, scores the evidence, and hands back a SUGGESTION plus the exact phrases
that produced it. `form_services` writes that to `suggested_stage` /
`suggestion_signals` and leaves `stage` at UNCLASSIFIED. A human — in the
dashboard, or by answering RAMA's question_for_user — is what promotes a
suggestion into a fact. Same rule the document inbox already follows: OCR
proposes an amount, a person confirms it before money moves.

Deterministic on purpose. No model call, no network, no temperature: the same
PDF must classify the same way in a test, in a Celery worker, and six months
from now. Weak-model-first, the same reason the RAMA tool surface is a hand
written dict rather than something the model generates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from .lease_forms import FormStage

#: Below this, we have seen nothing worth interrupting a landlord about.
MIN_SUGGESTION_SCORE = 3


@dataclass(frozen=True)
class Signal:
    """One piece of evidence: a pattern, what it implies, and how strongly."""

    pattern: str
    stage: str
    weight: int
    purpose: str = ""
    label: str = ""

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, re.IGNORECASE)


# Ordered strongest-first for readability only; scoring is order-independent.
#
# Weights, roughly:
#   10  the form names itself (a printed form number is not a coincidence)
#    6  an unambiguous title phrase
#    3  supporting vocabulary that only makes sense for one stage
#    2  a hint that narrows things but proves nothing on its own
SIGNALS: tuple[Signal, ...] = (
    # --- BC Residential Tenancy Branch forms, by their printed number -------
    Signal(
        r"#?\bRTB[-\s]?8\b",
        FormStage.MOVE_OUT,
        10,
        "Mutual Agreement to End a Tenancy (BC RTB-8) — both parties agree in "
        "writing to end the tenancy on a stated date.",
        "RTB-8",
    ),
    Signal(
        r"#?\bRTB[-\s]?1\b",
        FormStage.WITH_LEASE,
        10,
        "BC Residential Tenancy Agreement (RTB-1) — the tenancy agreement itself.",
        "RTB-1",
    ),
    # --- Title phrases -----------------------------------------------------
    Signal(
        r"mutual\s+agreement\s+to\s+end\s+(a\s+)?tenancy",
        FormStage.MOVE_OUT,
        6,
        "Ends the tenancy by mutual agreement on a stated date.",
        "mutual agreement to end a tenancy",
    ),
    Signal(
        r"notice\s+to\s+end\s+(a\s+)?tenancy",
        FormStage.MOVE_OUT,
        6,
        "Notice ending the tenancy.",
        "notice to end tenancy",
    ),
    Signal(
        r"\bmove[-\s]?out\b|\bvacate\b|\bvacating\b",
        FormStage.MOVE_OUT,
        3,
        "",
        "move-out / vacate",
    ),
    Signal(
        r"forwarding\s+address|return\s+of\s+(the\s+)?(security\s+)?deposit",
        FormStage.MOVE_OUT,
        3,
        "",
        "deposit return",
    ),
    Signal(
        r"tenancy\s+agreement|lease\s+agreement|residential\s+tenancy",
        FormStage.WITH_LEASE,
        3,
        "",
        "tenancy agreement",
    ),
    Signal(
        r"\bpet\s+(agreement|addendum|policy)\b|\bpet\s+damage\s+deposit\b",
        FormStage.WITH_LEASE,
        6,
        "Pet terms agreed as part of the tenancy.",
        "pet agreement",
    ),
    Signal(
        r"\bparking\s+(agreement|addendum)\b",
        FormStage.WITH_LEASE,
        6,
        "Parking terms agreed as part of the tenancy.",
        "parking agreement",
    ),
    Signal(
        r"smoke[-\s]?free|no[-\s]?smoking\s+(agreement|addendum|policy)",
        FormStage.WITH_LEASE,
        6,
        "Smoking terms agreed as part of the tenancy.",
        "smoking policy",
    ),
    Signal(
        r"\bguarantor\b|\bco[-\s]?signer\b|\bindemnit(y|or)\b",
        FormStage.WITH_LEASE,
        6,
        "A third party guarantees the tenant's obligations.",
        "guarantor agreement",
    ),
    Signal(
        r"\bhouse\s+rules\b|\bstrata\s+(bylaws?|rules)\b|\bcondo\s+rules\b",
        FormStage.ADDENDUM,
        6,
        "Rules the tenant acknowledges, signed at any point in the tenancy.",
        "house / strata rules",
    ),
    Signal(
        r"\baddendum\b|\bamendment\s+to\b|\bamending\s+agreement\b",
        FormStage.ADDENDUM,
        3,
        "Changes or adds to an existing agreement.",
        "addendum",
    ),
    Signal(
        r"\brent\s+increase\b",
        FormStage.ADDENDUM,
        6,
        "Notice of a rent increase during the tenancy.",
        "rent increase",
    ),
    Signal(
        r"condition\s+inspection\s+report",
        FormStage.ADDENDUM,
        6,
        "Condition inspection record. Rentium already has a built-in inspection "
        "workflow — this may not need to be a signed attachment.",
        "condition inspection",
    ),
)

_COMPILED = tuple((signal, signal.compiled()) for signal in SIGNALS)


@dataclass
class Suggestion:
    stage: str = ""
    purpose: str = ""
    confidence: str = "none"  # none | low | medium | high
    score: int = 0
    signals: list[dict] = dataclass_field(default_factory=list)
    scores: dict = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "purpose": self.purpose,
            "confidence": self.confidence,
            "score": self.score,
            "signals": self.signals,
            "scores": self.scores,
        }

    @property
    def is_actionable(self) -> bool:
        """Worth putting in front of a landlord as a proposal."""
        return bool(self.stage) and self.confidence in {"medium", "high"}


def _confidence(top: int, runner_up: int) -> str:
    # A clear winner is what makes a suggestion worth stating. Two stages
    # scoring close together means the document says both things, and the
    # honest answer there is "I'm not sure", not a coin flip dressed as an
    # answer.
    if top >= 10 and top >= runner_up * 2:
        return "high"
    if top >= 6 and top > runner_up:
        return "medium"
    if top >= MIN_SUGGESTION_SCORE:
        return "low"
    return "none"


def suggest_form_purpose(
    ocr_text: str = "",
    acroform_field_names: list[str] | None = None,
    filename: str = "",
) -> Suggestion:
    """Score what a blank form appears to be for. Never decides — only suggests.

    `acroform_field_names` and `filename` are folded into the same haystack as
    the OCR text because they are often the clearest evidence available: a form
    whose fields are named "forwarding address" is about a move-out whatever its
    body text says, and a file called `rtb8.pdf` from a scanner with no text
    layer would otherwise score zero.
    """
    parts = [ocr_text or "", filename or ""]
    parts.extend(acroform_field_names or [])
    haystack = "\n".join(str(part) for part in parts if part)
    if not haystack.strip():
        return Suggestion()

    scores: dict[str, int] = {}
    found: list[dict] = []
    purposes: dict[str, tuple[int, str]] = {}

    for signal, pattern in _COMPILED:
        match = pattern.search(haystack)
        if not match:
            continue
        # str(), not the enum member: these land in a JSONField and are read
        # back by RAMA and the UI, which should see "MOVE_OUT", not a repr.
        stage_key = str(signal.stage)
        scores[stage_key] = scores.get(stage_key, 0) + signal.weight
        found.append(
            {
                "label": signal.label or signal.pattern,
                "stage": stage_key,
                "weight": signal.weight,
                "matched": match.group(0)[:80],
            }
        )
        if signal.purpose:
            best = purposes.get(stage_key)
            if best is None or signal.weight > best[0]:
                purposes[stage_key] = (signal.weight, signal.purpose)

    if not scores:
        return Suggestion()

    ranked = sorted(scores.items(), key=lambda row: row[1], reverse=True)
    stage, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    confidence = _confidence(top, runner_up)

    if confidence == "none":
        return Suggestion(score=top, scores=scores, signals=found)

    return Suggestion(
        stage=stage,
        purpose=purposes.get(stage, (0, ""))[1],
        confidence=confidence,
        score=top,
        signals=found,
        scores=scores,
    )


def chat_question(suggestion: Suggestion, form_name: str) -> str:
    """The verbatim question RAMA asks a landlord about an unclassified form.

    Phrased as a choice between real options rather than "is this an RTB-8?",
    because a yes/no invites a weak model to answer on the landlord's behalf.
    """
    label = form_name or "that form"
    if not suggestion.is_actionable:
        return (
            f"I've stored {label}, but I can't tell what it's for. Is it signed "
            f"with the lease (so the lease isn't active until it's signed), "
            f"signed any time during the tenancy, or signed to end the tenancy?"
        )
    stage_words = {
        FormStage.WITH_LEASE: "signed with the lease",
        FormStage.ADDENDUM: "signed any time during the tenancy",
        FormStage.MOVE_OUT: "signed to end the tenancy",
    }
    reading = suggestion.purpose or stage_words.get(suggestion.stage, "")
    return (
        f"{label} looks like it's {stage_words.get(suggestion.stage, '')} — "
        f"{reading} Should I file it that way, or is it one of the other two "
        f"(signed with the lease / any time during the tenancy / to end the "
        f"tenancy)?"
    )
