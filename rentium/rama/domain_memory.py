"""
Tool implementations for durable landlord memory.

Thin on purpose: the interesting decisions (what may be stored, how a
correction supersedes, what reaches the prompt) all live in memory.py, so the
chat path and the API path cannot drift apart on policy.
"""

from __future__ import annotations

from .domain_crud import _confirmed
from .domain_crud import _preview
from .memory import forget as _forget
from .memory import normalise_key
from .memory import rejects
from .memory import write as _write
from .models import RamaMemory


def remember(
    landlord,
    *,
    subject: str,
    fact: str,
    applies_to: str = "",
    confirm: str = "",
) -> dict:
    body = (fact or "").strip()
    subject = (subject or "").strip()
    if not subject:
        return {"error": "subject is required — what is this preference about?"}
    if not body:
        return {"error": "fact is required — what should I remember?"}

    refusal = rejects(body)
    if refusal:
        # Not an error the model should retry around or rephrase past: the
        # guard exists precisely because a determined retry is the failure
        # mode. Report it to the landlord verbatim.
        return {"refused": True, "error": refusal}

    key = normalise_key(subject)
    if not key:
        return {"error": f"{subject!r} isn't a usable subject — try one or two words."}

    existing = RamaMemory.objects.filter(
        landlord=landlord, key=key, status=RamaMemory.Status.ACTIVE,
    ).first()
    scope = (
        RamaMemory.Scope.ENTITY
        if (applies_to or "").strip()
        else RamaMemory.Scope.PORTFOLIO
    )
    preview = {
        "subject": key,
        "fact": body,
        "applies_to": (applies_to or "").strip(),
        "replaces": existing.body if existing else "",
    }
    if not _confirmed(confirm):
        return _preview(
            "remember",
            preview,
            "Replaces what I remembered about this."
            if existing
            else "Remembers this for future conversations.",
        )

    row = _write(
        landlord,
        key=key,
        body=body,
        scope=scope,
        entity_key=(applies_to or "").strip(),
    )
    return {
        "remembered": True,
        "subject": row.key,
        "fact": row.body,
        "replaced": preview["replaces"],
    }


def forget(landlord, *, subject: str, confirm: str = "") -> dict:
    subject = (subject or "").strip()
    if not subject:
        return {"error": "subject is required — what should I forget?"}

    key = normalise_key(subject)
    existing = RamaMemory.objects.filter(
        landlord=landlord, key=key, status=RamaMemory.Status.ACTIVE,
    ).first()
    if existing is None:
        existing = RamaMemory.objects.filter(
            landlord=landlord, status=RamaMemory.Status.ACTIVE, body__icontains=subject,
        ).first()
    if existing is None:
        return {"error": f"I'm not holding anything about {subject!r}."}

    preview = {"subject": existing.key, "fact": existing.body}
    if not _confirmed(confirm):
        return _preview("forget", preview, "Drops this preference.")

    row = _forget(landlord, subject)
    return {"forgotten": True, "subject": row.key, "fact": row.body}
