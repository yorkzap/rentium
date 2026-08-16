"""Telling "there are none" apart from "I can't see any of those".

August 2026, verbatim:

    landlord: Did I provide anyone discount this month?
    RAMA:     Short answer: No — I don't see any discounts recorded for this
              month (Aug 2026).
              What I checked:
              • Ledger (due_date in Aug 2026) shows only Rent charges…
              • A query for negative amounts in Aug 2026 returned 0 rows.

There was a $1,600 discount. RAMA could not see it, because a discount in
Rentium is a RentAdjustment and that model was not in the manifest at all. So it
reasoned from generic accounting — a discount is a negative amount — searched
the ledger, found nothing, and reported the absence of evidence as evidence of
absence.

This is the worst failure mode the agent has. A missing FIELD produces an error
that names the fields that do exist, and the model corrects itself. A missing
ENTITY produces silence, and silence reads exactly like zero. Adding the missing
model fixes that one question; this module fixes the class, and keeps fixing it
for whatever isn't in the manifest next.

The index is derived from the manifest and the models behind it — entity labels,
field labels, and enum choice names — so a concept becomes recognisable the day
somebody declares it, with no word list to maintain here.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .manifest import MANIFEST

# Words that appear in so many labels they identify nothing. Indexing them would
# point every sentence at every entity and the guard would never fire usefully.
_TOO_GENERIC = frozenset(
    """
    a an and or of on in at to for the this that with without by from is are
    date dates day days month months year years time times
    name names label labels type types kind kinds status statuses state states
    amount amounts total totals number numbers count counts sum value values
    id ids record records entry entries row rows item items note notes
    description details detail info information data field fields
    property properties unit units per new old other others
    """.split(),
)


# The vocabulary that MAKES a sentence read as a denial. It must never also be
# read as the thing being denied, or the guard cites itself: "Layout recorded"
# is a real field label on property_unit, so "no discounts recorded" flagged
# property_unit — a denial about the word "recorded". The trigger words and the
# payload words have to be disjoint sets.
_DENIAL_VOCAB = frozenset(
    """
    no not never none nothing zero any some
    recorded record recording logged log found find file filed
    see seen saw show shows shown
    match matches matching returned returns return
    result results row rows query queries queried
    check checked checking look looked looking
    short answer against
    """.split(),
)

_WORD = re.compile(r"[a-z]+")

# A sentence claiming nothing of a kind exists. Deliberately narrow: it must be
# an assertion about the books, not a refusal ("no, I can't do that") or a
# hedge. Over-firing here costs a wasted round on every turn, so the bar is a
# denial the landlord would reasonably read as "the answer is zero".
_DENIALS = (
    re.compile(r"\bno\b[^.!?\n]{0,80}\b(?:recorded|logged|on file|found|"
               r"in the (?:books|ledger|system|records))", re.I),
    re.compile(r"\bi (?:don'?t|do not|didn'?t|can'?t|cannot) see\b", re.I),
    re.compile(r"\bthere (?:are|is|were|was) (?:no|not any|none)\b", re.I),
    re.compile(r"\b(?:nothing|none)\b[^.!?\n]{0,40}\b(?:recorded|logged|"
               r"found|on file|matches?|matching)", re.I),
    re.compile(r"^\s*(?:short answer:\s*)?no\b[\s,.—-]", re.I | re.M),
    re.compile(r"\b(?:0|zero) (?:rows|results|matches|records)\b", re.I),
)


def _stem(word: str) -> str:
    """Crudest possible singular. The enum choice is DISCOUNT; the landlord and
    the model both write "discounts". Applied to BOTH the index and the lookup,
    so the two always agree — a real stemmer would be a dependency and a new
    way for them to disagree."""
    if len(word) > 4 and word.endswith(("ses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [
        stemmed
        for w in _WORD.findall(text.casefold())
        if (stemmed := _stem(w)) not in _TOO_GENERIC
        and stemmed not in _DENIAL_VOCAB
        and w not in _TOO_GENERIC
        and w not in _DENIAL_VOCAB
    ]


def _enum_words(spec) -> list[str]:
    """Choice display names, e.g. AdjustmentType.DISCOUNT → 'Negotiated
    Discount' → {negotiated, discount}. This is what makes the word the
    landlord actually used ("discount") resolve to an entity nobody labelled
    "discount" anywhere."""
    from django.apps import apps  # noqa: PLC0415

    try:
        model = apps.get_model(spec.model)
    except LookupError:  # pragma: no cover - a manifest typo is caught elsewhere
        return []
    found: list[str] = []
    for field in spec.fields:
        if field.type != "enum" or field.source:
            continue
        try:
            choices = model._meta.get_field(field.name).choices or ()
        except Exception:  # noqa: BLE001 - annotations and traversals have none
            continue
        for value, display in choices:
            found.extend(_words(str(value).replace("_", " ")))
            found.extend(_words(str(display)))
    return found


@lru_cache(maxsize=1)
def concept_index() -> dict[str, frozenset[str]]:
    """word → every entity key that could hold it.

    Cached: the manifest is static at runtime. Tests that mutate it call
    concept_index.cache_clear() and primary_index.cache_clear().
    """
    index: dict[str, set[str]] = {}
    for key, spec in MANIFEST.items():
        words = set(_words(key.replace("_", " ")))
        words |= set(_words(spec.label))
        for field in spec.fields:
            words |= set(_words(field.label))
        words |= set(_enum_words(spec))
        for word in words:
            index.setdefault(word, set()).add(key)
    return {word: frozenset(keys) for word, keys in index.items()}


@lru_cache(maxsize=1)
def primary_index() -> dict[str, frozenset[str]]:
    """word → entities the word is the NAME of, not merely a detail on.

    Only the entity's key and label count here. The distinction is what makes
    the guard usable:

      "discount" — rent_adjustment is LABELLED "Rent adjustment (discount /
        proration / increase)". The concept is what the table is for. Not
        reading it is a real hole.
      "deposit"  — no entity is called a deposit; it appears as field labels on
        lease and lease_tenant and as a ledger entry_type. Every one of those
        is a legitimate partial answer, so a denial after reading any of them
        is a judgement call, not an obvious miss.

    Firing on the second kind is how a correct answer about August deposits got
    replaced with a list of furniture: "Room C" and "Garden Suite" appeared in
    the reply, matched incidental label words, and sent the model off to read
    inventory. Only a primary owner is worth interrupting an answer for.
    """
    index: dict[str, set[str]] = {}
    for key, spec in MANIFEST.items():
        for word in set(_words(key.replace("_", " "))) | set(_words(spec.label)):
            index.setdefault(word, set()).add(key)
    return {word: frozenset(keys) for word, keys in index.items()}


def denial_sentences(text: str) -> list[str]:
    """The sentences in a reply that assert nothing of some kind exists."""
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        stripped = sentence.strip()
        if stripped and any(p.search(stripped) for p in _DENIALS):
            out.append(stripped)
    return out


def unchecked_denials(
    text: str,
    entities_read: set[str],
    *,
    landlord_message: str = "",
    proper_nouns: frozenset[str] = frozenset(),
) -> dict[str, frozenset[str]]:
    """Concepts denied in `text` whose own table this turn never read.

    Returns {word: the primary entities holding it that were NOT read}. Empty
    unless the reply denies the very thing that was asked about and never
    looked in the table named after it.

    Three conditions, each one earned by a false positive:

    1. The word is a PRIMARY owner's name (see primary_index) — otherwise an
       incidental "Room C" in a deposit answer sends the model to read
       inventory.
    2. The word appears in the LANDLORD'S QUESTION. The guard exists to stop
       RAMA denying what was asked; a noun that only shows up in the answer,
       as an identifier or an aside, is not the claim being made.
    3. Some primary owner went unread. Not "all" — "discount" lives in
       ledger_entry too (entry_type CREDIT, "Credit / Discount"), and RAMA read
       the ledger, found it genuinely empty, and stopped. That check was
       correct and the conclusion was still wrong, because the other place held
       $1,600. One source can confirm a thing exists; it can never rule it out.
    4. The word is not the NAME of something in the landlord's portfolio.
       "How many bedrooms are there in the garden suite?" matched "suite"
       against property_unit ("Unit within a holding (floor / suite)") and sent
       RAMA away from property_area — which held both bedrooms — to a table
       that could not answer. A name that happens to contain a domain word is
       still a name. `proper_nouns` comes from the landlord's own units,
       holdings, properties and people.
    """
    index = primary_index()
    read = set(entities_read or ())
    asked = set(_words(landlord_message or ""))
    if not asked:
        return {}
    names = {_stem(word) for word in (proper_nouns or ())}
    flagged: dict[str, frozenset[str]] = {}
    for sentence in denial_sentences(text):
        for word in _words(sentence):
            if word not in asked or word in names:
                continue
            owners = index.get(word)
            if not owners or len(owners) > 3:
                continue
            unread = owners - read
            if unread:
                flagged[word] = frozenset(unread)
    return flagged


def look_here_first(flagged: dict[str, frozenset[str]]) -> str:
    """The instruction handed back to the model when a denial is unsupported."""
    lines = []
    for word, owners in sorted(flagged.items()):
        where = ", ".join(sorted(owners))
        lines.append(f'- "{word}" is recorded in: {where}')
    return (
        "STOP — you are about to tell the landlord that something does not "
        "exist, and you never looked where it would be.\n"
        + "\n".join(lines)
        + "\n\nRead the entity named above with the `read` tool, then answer "
        "THE LANDLORD'S ORIGINAL QUESTION — this is one missing check inside "
        "that answer, not a new topic. Keep every figure you already gathered; "
        "do not start reporting on the entity you just read as if it were what "
        "was asked.\n"
        "If it really is empty, say so and name the record type you checked. "
        "Never report the absence of a record you did not query — in this "
        "system a discount, a proration and a rent increase are all "
        "rent_adjustment rows, not ledger lines, and answering from the wrong "
        "table produces a confident wrong answer."
    )
