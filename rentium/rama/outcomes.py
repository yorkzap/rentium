"""Typed terminal outcomes for RAMA commands.

Provider prose is presentation only.  The command engine and HTTP clients can
rely on these values to know whether a turn answered, needs input, previewed a
real operation, completed one, was already satisfied, or failed.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any


class OutcomeKind(str, Enum):
    ANSWER = "ANSWER"
    NEEDS_INPUT = "NEEDS_INPUT"
    PREVIEW = "PREVIEW"
    COMPLETED = "COMPLETED"
    NOOP = "NOOP"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CommandOutcome:
    kind: OutcomeKind
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    entity_refs: tuple[dict[str, str], ...] = ()
    links: tuple[dict[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value

    @classmethod
    def from_tool_result(cls, result: dict[str, Any]) -> CommandOutcome:
        result = result or {}
        if result.get("error"):
            return cls(OutcomeKind.FAILED, str(result["error"]), data=result)
        if result.get("needs_input"):
            missing = tuple(str(x) for x in (result.get("missing") or ()))
            message = str(
                result.get("question")
                or result.get("detail")
                or result.get("needs_input")
                or "More information is required.",
            )
            return cls(OutcomeKind.NEEDS_INPUT, message, data=result, missing=missing)
        if result.get("needs_confirm"):
            return cls(
                OutcomeKind.PREVIEW,
                str(result.get("summary") or "Ready for confirmation."),
                data=result,
            )
        if result.get("already_done") or result.get("noop"):
            return cls(
                OutcomeKind.NOOP,
                str(
                    result.get("already_done")
                    or result.get("detail")
                    or "That is already done.",
                ),
                data=result,
            )
        return cls(
            OutcomeKind.COMPLETED,
            str(result.get("summary") or result.get("detail") or "Completed."),
            data=result,
        )
