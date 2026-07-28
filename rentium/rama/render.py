"""
Numbers that carry where they came from.

A landlord acting on the Treasurer's advice needs to know which figures are
measured and which are assumed — "you have $180k of equity" means something
very different if the valuation is a 2019 appraisal versus this month's
assessment. So the Treasurer never passes a bare number around: every figure
travels with its provenance, and the human-readable string is rendered from
that pair rather than typed by a model.

This is also the anti-hallucination mechanism. Prose the model writes refers to
figures only by token (`{{f7}}`); `substitute()` replaces each token with the
value AND its parenthetical. A model physically cannot drop the caveat or
invent a number here, because it never types one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


class SourceType:
    """Where a number came from, in descending order of how much it can be
    trusted without qualification."""

    LEDGER = "LEDGER"  # posted, immutable, reconciled
    DOCUMENT = "DOCUMENT"  # read off a document the landlord supplied
    LANDLORD = "LANDLORD"  # the landlord told us
    WEB = "WEB"  # researched, with a URL and a fetch date
    TAX_TABLE = "TAX_TABLE"  # a dated rate table a human loaded
    ESTIMATE = "ESTIMATE"  # computed under stated assumptions
    PROVISIONAL = "PROVISIONAL"  # staged, not yet committed to the ledger


ALL_SOURCE_TYPES = frozenset(
    {
        SourceType.LEDGER,
        SourceType.DOCUMENT,
        SourceType.LANDLORD,
        SourceType.WEB,
        SourceType.TAX_TABLE,
        SourceType.ESTIMATE,
        SourceType.PROVISIONAL,
    }
)

# How each source reads to a human. Kept here so the wording is identical
# everywhere and cannot drift between a chat reply and a dashboard card.
_SOURCE_WORDING = {
    SourceType.LEDGER: "from your books",
    SourceType.DOCUMENT: "from a document you provided",
    SourceType.LANDLORD: "you told me",
    SourceType.WEB: "researched",
    SourceType.TAX_TABLE: "published rate",
    SourceType.ESTIMATE: "estimate",
    SourceType.PROVISIONAL: "not yet posted",
}


@dataclass(frozen=True)
class Provenance:
    source_type: str
    ref: str = ""  # ledger id, fact id, URL, table id
    as_of: date | None = None
    fetched_at: datetime | None = None
    note: str = ""

    def describe(self) -> str:
        """The parenthetical a landlord sees beside the number."""
        bits = [_SOURCE_WORDING.get(self.source_type, self.source_type.lower())]
        if self.note:
            bits.append(self.note)
        if self.as_of:
            bits.append(f"as of {self.as_of.isoformat()}")
        elif self.fetched_at:
            bits.append(f"fetched {self.fetched_at.date().isoformat()}")
        return ", ".join(bits)


@dataclass(frozen=True)
class Figure:
    """One number, its units, and its pedigree.

    `value is None` is a first-class state meaning "we do not know this" — it
    is deliberately NOT zero. Treating an unknown mortgage balance as $0 would
    silently report the landlord as owning the house outright.
    """

    value: Decimal | None
    unit: str = "CAD"
    period: str = "ONE_TIME"  # ONE_TIME | MONTHLY | ANNUAL
    provenance: Provenance | None = None
    label: str = ""
    # Tokens of the figures this was computed from, so a derived number can be
    # traced back to its inputs rather than appearing from nowhere.
    derived_from: tuple[str, ...] = field(default_factory=tuple)

    @property
    def known(self) -> bool:
        return self.value is not None

    def render(self) -> str:
        if self.value is None:
            # Say WHY it is unknown. "I can't tell you your equity because
            # there's no valuation on file" is actionable; "unknown" is not,
            # and this is the most common thing the Treasurer has to report.
            reason = (self.provenance.note if self.provenance else "") or (
                f"{self.label or 'this figure'} not on file"
            )
            return f"unknown ({reason})"
        if self.unit == "CAD":
            body = f"${self.value:,.2f}"
        elif self.unit == "percent":
            body = f"{self.value}%"
        elif self.unit == "years":
            body = f"{self.value} years"
        else:
            body = f"{self.value} {self.unit}"
        if self.period == "MONTHLY":
            body += "/mo"
        elif self.period == "ANNUAL":
            body += "/yr"
        if self.provenance is None:
            return body
        return f"{body} ({self.provenance.describe()})"


def unknown(label: str, *, unit: str = "CAD", note: str = "") -> Figure:
    """A figure we do not have. Named so callers stop reaching for Decimal(0)."""
    return Figure(
        value=None,
        unit=unit,
        label=label,
        provenance=Provenance(source_type=SourceType.ESTIMATE, note=note)
        if note
        else None,
    )


# ---------------------------------------------------------------------------
# Token substitution.
#
# The model writes prose referring to figures ONLY as {{f7}}. Python swaps each
# token for the value and its provenance. This is what makes "every number is a
# computed number" a property of the pipeline rather than an instruction the
# model is asked to follow — a weak model cannot drop a caveat or invent a
# figure it never typed.
# ---------------------------------------------------------------------------
import re as _re

_TOKEN = _re.compile(r"\{\{\s*(f\d+)\s*\}\}")


def substitute(prose: str, table: dict[str, Figure]) -> tuple[str, list[str]]:
    """Replace {{fN}} with rendered figures.

    Returns (text, violations). A token with no matching figure is a contract
    violation, not something to paper over: it means the model invented a
    reference, and the caller should retry or fall back to the deterministic
    table rather than show the landlord a dangling placeholder.
    """
    violations: list[str] = []

    def _swap(match):
        token = match.group(1)
        figure = table.get(token)
        if figure is None:
            violations.append(f"unknown figure token {{{{{token}}}}}")
            return f"[{token}?]"
        return figure.render()

    return _TOKEN.sub(_swap, prose or ""), violations


def unresolved_tokens(prose: str, table: dict[str, Figure]) -> list[str]:
    return [t for t in _TOKEN.findall(prose or "") if t not in table]


def provenance_legend(table: dict[str, Figure]) -> str:
    """A short 'where these came from' block for a report footer."""
    seen: dict[str, str] = {}
    for figure in table.values():
        if figure.provenance is None:
            continue
        seen.setdefault(
            figure.provenance.source_type,
            _SOURCE_WORDING.get(figure.provenance.source_type, ""),
        )
    if not seen:
        return ""
    return "Sources: " + "; ".join(f"{k.lower()} — {v}" for k, v in sorted(seen.items()))
