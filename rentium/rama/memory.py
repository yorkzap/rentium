"""
Durable landlord memory: what RAMA is allowed to remember between conversations.

Until now nothing survived a conversation except the Constitution — twelve
text-only history turns and a capped fact digest, then nothing. That is the
gap this closes.

The design is defensive on purpose, because a memory store attached to an
agent has two failure modes that are much worse than having no memory at all:

1. **It becomes a hallucination source.** If RAMA stores "Room C rents for
   $900" and the rent later changes, it now has a confident, durable, wrong
   fact that competes with the live portfolio card. `rejects()` refuses to
   store portfolio state at all — that is what `live_context()` is for, and it
   is recomputed every turn.

2. **It becomes a liability.** A landlord saying "remember the tenant in 2B is
   on disability" would have the app create a durable record of special
   category personal data. `rejects()` refuses that too, for the same reason
   `constitution.unlawful_deposit_language` exists: whatever is written here,
   RAMA will act on and repeat.

Corrections are supersessions, never edits, so "why did RAMA think that?" is
always answerable from the chain.
"""

from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import RamaMemory

# Injection budget. Small on purpose: memory is a nudge, not a briefing.
MAX_INJECTED_ROWS = 12
MAX_INJECTED_CHARS = 800
# A row nobody has used in this long stops being injected (but is not deleted —
# the landlord may still see and re-pin it).
STALE_AFTER_DAYS = 180


# ------------------------------------------------------- portfolio-state guard
# These patterns describe things live_context() already knows and recomputes.
_MONEY = re.compile(r"[$€£]\s?\d|(?<!\w)\d[\d,]*\.\d{2}(?!\w)")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_COUNT_OF_THINGS = re.compile(
    r"\b\d+\s+(listings?|properties|units?|rooms?|leases?|tenants?|holdings?)\b",
    re.IGNORECASE,
)
_LIVE_FACTS = re.compile(
    r"\b(rent is|rent of|balance is|outstanding|owes|owing|arrears|"
    r"deposit is|deposit of|lease (starts|ends|expires)|"
    r"is (currently )?(vacant|occupied)|moved (in|out) on)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------- special-category guard
# Under PIPEDA / BC PIPA these are exactly the categories a landlord must not
# be keeping informal notes on, and a durable agent-readable record is worse
# than a note: RAMA would surface it unprompted, forever.
_SPECIAL_CATEGORY = re.compile(
    r"\b(disabilit(y|ies)|disabled|wheelchair|mental health|depress(ed|ion)|"
    r"anxiety|addict(ion|ed)|alcoholic|hiv|aids|cancer|pregnan(t|cy)|"
    r"medication|prescription|diagnos(is|ed)|therapy|"
    r"immigration status|undocumented|refugee|asylum|visa status|"
    r"race|racial|ethnicity|ethnic|religion|religious|muslim|christian|jewish|"
    r"hindu|sikh|buddhist|atheist|"
    r"gay|lesbian|bisexual|transgender|trans |queer|sexual orientation|"
    r"criminal record|convicted|felony|on parole|on probation|"
    r"single (mother|father|parent)|on welfare|on assistance|on disability)\b",
    re.IGNORECASE,
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)")
_POSTAL = re.compile(r"\b[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d\b")


def rejects(body: str) -> str | None:
    """Reason to refuse storing this, or None.

    Modelled on constitution.unlawful_deposit_language: it does not ban a
    subject, it bans an unsafe formulation, and it explains itself in terms the
    landlord can act on.
    """
    text = (body or "").strip()
    if not text:
        return "There's nothing to remember — tell me the preference."
    if len(text) > RamaMemory.MAX_BODY_CHARS:
        return (
            "That's too long to hold as one preference. Give me the single "
            f"standing rule in {RamaMemory.MAX_BODY_CHARS} characters or fewer."
        )
    if _SPECIAL_CATEGORY.search(text):
        return (
            "I can't store that. It describes someone's health, background, or "
            "personal circumstances, and keeping a standing record of it would "
            "put you offside privacy law (PIPEDA / BC PIPA) — I'd also repeat "
            "it back unprompted for as long as it existed. If it affects how "
            "you run the tenancy, record the accommodation you're providing, "
            "not the reason for it."
        )
    if (
        _MONEY.search(text)
        or _ISO_DATE.search(text)
        or _COUNT_OF_THINGS.search(text)
        or _LIVE_FACTS.search(text)
    ):
        return (
            "That's live data, not a preference — I read rents, balances, "
            "dates and occupancy fresh from your portfolio every time you ask, "
            "so storing a copy would go stale and start misleading you. Ask me "
            "for it any time instead. If there's a standing rule behind it "
            "(how you want something handled), tell me that and I'll keep it."
        )
    return None


def personal_data_present(landlord, body: str) -> bool:
    """Whether this memory contains someone's contact details.

    Not a refusal — "my plumber is Bob at 250-555-0100" is exactly the kind of
    thing worth remembering. It flags the row so an erasure request can find it
    (see the rama_forget_subject command); deleting a tenant's account cannot
    reach inside a text blob on its own.
    """
    text = body or ""
    if _EMAIL.search(text) or _PHONE.search(text) or _POSTAL.search(text):
        return True
    try:
        from rentium.leases.models import LeaseTenant

        names = (
            LeaseTenant.objects.filter(lease__landlord=landlord)
            .values_list("invited_name", flat=True)
            .distinct()
        )
    except Exception:  # noqa: BLE001 - flagging is best-effort, never fatal
        return False
    lowered = text.casefold()
    return any(n and str(n).strip().casefold() in lowered for n in names)


def normalise_key(subject: str) -> str:
    """The stable identity a memory supersedes on."""
    return (slugify(subject or "")[:80]).strip("-")


# ------------------------------------------------------------------- writes
@transaction.atomic
def write(
    landlord,
    *,
    key: str,
    body: str,
    scope: str = RamaMemory.Scope.PORTFOLIO,
    entity_key: str = "",
    source: str = RamaMemory.Source.LANDLORD_EXPLICIT,
    conversation_id=None,
) -> RamaMemory:
    """Record a memory, superseding any active row on the same key.

    Never edits in place: the previous row is marked SUPERSEDED and pointed at
    by the new one, so a wrong memory leaves a trail rather than vanishing.
    """
    slug = normalise_key(key)
    previous = RamaMemory.objects.select_for_update().filter(
        landlord=landlord, key=slug, status=RamaMemory.Status.ACTIVE,
    ).first()
    if previous is not None:
        previous.status = RamaMemory.Status.SUPERSEDED
        previous.save(update_fields=["status", "updated_at"])

    row = RamaMemory.objects.create(
        landlord=landlord,
        key=slug,
        body=(body or "").strip(),
        scope=scope,
        entity_key=(entity_key or "").strip()[:120],
        source=source,
        origin_conversation=conversation_id,
        supersedes=previous,
        contains_personal_data=personal_data_present(landlord, body),
    )
    _enforce_capacity(landlord)
    return row


def _enforce_capacity(landlord) -> None:
    """Keep the active set bounded, deterministically and without an LLM.

    Oldest unpinned never-used rows go first — the ones least likely to be
    load-bearing. They are superseded, not deleted, so nothing is lost.
    """
    active = RamaMemory.objects.filter(
        landlord=landlord, status=RamaMemory.Status.ACTIVE,
    )
    overflow = active.count() - RamaMemory.MAX_ACTIVE_PER_LANDLORD
    if overflow <= 0:
        return
    doomed = list(
        active.filter(pinned=False, use_count=0).order_by("updated_at")[:overflow],
    )
    RamaMemory.objects.filter(pk__in=[row.pk for row in doomed]).update(
        status=RamaMemory.Status.SUPERSEDED, updated_at=timezone.now(),
    )


def forget(landlord, subject: str) -> RamaMemory | None:
    """Retire a memory. Returns the row that was retired, or None."""
    slug = normalise_key(subject)
    row = RamaMemory.objects.filter(
        landlord=landlord, key=slug, status=RamaMemory.Status.ACTIVE,
    ).first()
    if row is None:
        # Fall back to a text match so "forget the Sunday thing" works.
        row = RamaMemory.objects.filter(
            landlord=landlord,
            status=RamaMemory.Status.ACTIVE,
            body__icontains=(subject or "").strip(),
        ).first()
    if row is None:
        return None
    row.status = RamaMemory.Status.FORGOTTEN
    row.save(update_fields=["status", "updated_at"])
    return row


# ------------------------------------------------------------------- reads
def active_memories(landlord):
    """Injectable rows: active, explicit, unexpired, not stale."""
    now = timezone.now()
    stale_before = now - timezone.timedelta(days=STALE_AFTER_DAYS)
    return (
        RamaMemory.objects.filter(
            landlord=landlord,
            status=RamaMemory.Status.ACTIVE,
            source=RamaMemory.Source.LANDLORD_EXPLICIT,
        )
        .exclude(expires_at__lt=now)
        .exclude(last_used_at__lt=stale_before, pinned=False)
    )


def render_for_prompt(landlord, message: str = "", focus: dict | None = None) -> str:
    """The LANDLORD MEMORY block, or "" when there is nothing to say.

    Retrieval is deterministic and bounded — no embeddings. For the tens of
    rows a landlord accumulates, a vector store would be a new dependency and a
    new failure mode buying nothing.
    """
    rows = list(active_memories(landlord))
    if not rows:
        return ""

    # Match ENTITY memories against what the landlord just said plus whatever
    # "it" currently resolves to (service._conversation_focus), so a follow-up
    # inherits the right property's preferences.
    focused = ((focus or {}).get("property") or {}).get("name") or ""
    haystack = f"{message or ''} {focused}".casefold()
    chosen: list[RamaMemory] = []
    for row in rows:
        if row.scope == RamaMemory.Scope.PORTFOLIO or (row.entity_key and row.entity_key.casefold() in haystack):
            chosen.append(row)

    chosen.sort(key=lambda r: (not r.pinned, -r.updated_at.timestamp()))

    lines: list[str] = []
    used = 0
    dropped = 0
    for row in chosen[:MAX_INJECTED_ROWS]:
        line = f"- {row.body.strip()}"
        # Never truncate a fact: half a fact is a false fact.
        if used + len(line) > MAX_INJECTED_CHARS:
            dropped += 1
            continue
        lines.append(line)
        used += len(line)
    dropped += max(0, len(chosen) - MAX_INJECTED_ROWS)

    if not lines:
        return ""
    _mark_used([row for row in chosen[: len(lines)]])

    note = ""
    if dropped:
        note = f"\n({dropped} more not shown — ask me what you've told me to remember.)"
    return (
        "## LANDLORD MEMORY (durable preferences this landlord told you in "
        "earlier conversations — NOT portfolio data. Subordinate to LIVE "
        "PORTFOLIO and THE CONSTITUTION: if they disagree, those are right. "
        "Never quote a number, date, rent, or balance from this section.)\n"
        + "\n".join(lines)
        + note
    )


def _mark_used(rows: list[RamaMemory]) -> None:
    if not rows:
        return
    from django.db.models import F

    RamaMemory.objects.filter(pk__in=[r.pk for r in rows]).update(
        use_count=F("use_count") + 1, last_used_at=timezone.now(),
    )


def payload(landlord, query: str = "") -> dict:
    """JSON-safe listing for the tool and the settings UI."""
    qs = RamaMemory.objects.filter(
        landlord=landlord, status=RamaMemory.Status.ACTIVE,
    )
    if (query or "").strip():
        qs = qs.filter(body__icontains=query.strip())
    return {
        "memories": [
            {
                "id": str(row.pk),
                "subject": row.key,
                "fact": row.body,
                "applies_to": row.entity_key,
                "source": row.source,
                "personal_data": row.contains_personal_data,
                "used": row.use_count,
                "recorded": str(row.created_at.date()),
            }
            for row in qs
        ],
    }
