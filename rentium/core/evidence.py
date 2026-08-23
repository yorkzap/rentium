"""Typed evidence returned by trusted domain projections.

Operational answers must distinguish a complete query from a partial view.  A
plain ``[]`` cannot do that: it can mean either "there are none" or "this
adapter cannot see them".  ``FactPacket`` carries that distinction all the way
from the domain service to RAMA's renderer and API response.

This module deliberately has no Django or RAMA dependency.  Lease documents,
REST serializers, management commands, and assistants can therefore consume
the same facts without importing a chat runtime.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from typing import Any

_COMPLETE_WITH_GAPS = "COMPLETE evidence cannot contain unavailable facts or conflicts"
_PARTIAL_WITH_NEGATIVES = (
    "Negative conclusions are allowed only when coverage is COMPLETE"
)


class Coverage(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    CONFLICTING = "CONFLICTING"


@dataclass(frozen=True)
class FactSource:
    model: str
    record_id: str
    field: str


@dataclass(frozen=True)
class Fact:
    key: str
    value: Any
    source: FactSource
    value_type: str = "string"


@dataclass(frozen=True)
class EvidenceGap:
    key: str
    reason: str
    source: FactSource | None = None


@dataclass(frozen=True)
class EvidenceConflict:
    key: str
    values: tuple[Any, ...]
    sources: tuple[FactSource, ...]


@dataclass(frozen=True)
class FactPacket:
    subject: dict[str, str]
    facet: str
    as_of: str
    coverage: Coverage
    facts: tuple[Fact, ...] = ()
    missing: tuple[EvidenceGap, ...] = ()
    unavailable: tuple[EvidenceGap, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    allowed_negative_conclusions: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.coverage == Coverage.COMPLETE and (self.unavailable or self.conflicts):
            raise ValueError(_COMPLETE_WITH_GAPS)
        if self.coverage != Coverage.COMPLETE and self.allowed_negative_conclusions:
            raise ValueError(_PARTIAL_WITH_NEGATIVES)

    def permits_negative(self, conclusion: str) -> bool:
        """Return whether this exact absence claim is supported by complete facts."""
        return (
            self.coverage == Coverage.COMPLETE
            and conclusion in self.allowed_negative_conclusions
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["coverage"] = self.coverage.value
        return payload
