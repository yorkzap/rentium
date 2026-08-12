"""Bind a landlord's requested outcome to the plan RAMA may execute.

Tool schemas answer "is this call valid?".  They do not answer the more
important question "is this the change the landlord requested?".  This module
adds that second check.  The contract is deliberately small and JSON-safe so
it can be persisted inside ``RamaTask.input`` and checked again when the
landlord later says Yes.

High-confidence capability routing is generic.  Domain outcome constraints
are additive: a capability can teach this layer how to preserve phrases such
as "make the total $400" without putting business arithmetic in the model.
"""

from __future__ import annotations

import re
from decimal import Decimal
from decimal import InvalidOperation

from .capabilities import supported_tool_for_request

_LEASE_NUMBER = re.compile(r"\bRMT[A-Z0-9-]+\b", re.IGNORECASE)
_MONEY = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)")
_CONTROL_ONLY = re.compile(
    r"^\s*(?:yes|y|yeah|yep|confirm|confirmed|proceed|go ahead|do it|"
    r"prepare(?: the)? preview|preview it|now|is it done(?: yet)?|hello|hi)"
    r"\s*[?.!]*\s*$",
    re.IGNORECASE,
)
_CONTEXTUAL_FOLLOW_UP = re.compile(
    r"^\s*(?:household|shared|both|individual|her|[a-z]+ only|first one|"
    r"second one|the first one|the second one|option (?:one|two)|"
    r"the one\b.*|RMT[A-Z0-9-]+)\s*[?.!]*\s*$",
    re.IGNORECASE,
)


def _money(value: str) -> str | None:
    try:
        return str(Decimal(value.replace(",", "")).quantize(Decimal("0.01")))
    except (InvalidOperation, AttributeError):
        return None


def _rent_target(text: str) -> dict:
    """Extract a desired final state, never a model-computed delta."""
    low = " ".join((text or "").casefold().replace(chr(0x2019), "'").split())
    if "rent" not in low or not (
        re.search(r"\b(?:first|1st)\s+month", low)
        or re.search(r"\bprorat(?:ed|ion)\s+rent\b", low)
    ):
        return {}
    # "discount by/off $400" is a delta, not a requested final total.
    if re.search(r"\b(?:by|off)\s*\$\s*[0-9]", low):
        return {}
    target_wording = bool(
        re.search(r"\b(?:to|at|only)\s*\$\s*[0-9]", low)
        or re.search(r"\btotal(?:\s+(?:to|of|is|be))?\s*\$\s*[0-9]", low)
        or re.search(
            r"\b(?:set|make|change|adjust)\b[^.!?]{0,100}\$\s*[0-9]",
            low,
        ),
    )
    if not target_wording:
        return {}
    matches = _MONEY.findall(text or "")
    target = _money(matches[-1]) if matches else None
    return {"target_lease_total": target} if target is not None else {}


# Domain capabilities contribute outcome extractors to one enforcement path.
# Adding another requested-final-state workflow extends this registry; it does
# not require another branch in the turn engine or confirmation runner.
OUTCOME_EXTRACTORS = {
    "apply_rent_adjustment": _rent_target,
}


def contract_from_messages(messages: list[str]) -> dict:
    """Compile the newest high-confidence request from landlord messages.

    ``messages`` must be newest first.  Short continuation turns ("preview",
    "now", "hello") inherit the nearest real request; an explicit new request
    wins immediately.  No fuzzy catalogue score becomes authority here.
    """
    for index, raw in enumerate(messages):
        text = str(raw or "").strip()
        if not text or _CONTROL_ONLY.fullmatch(text):
            continue
        capability = supported_tool_for_request(text)
        if not capability:
            if index == 0 and not _CONTEXTUAL_FOLLOW_UP.fullmatch(text):
                return {}
            continue
        contract = {
            "version": 1,
            "required_capability": capability,
            "source_text": text[:1000],
            "constraints": {},
        }
        lease = _LEASE_NUMBER.search(text)
        if lease:
            contract["constraints"]["lease_number"] = lease.group(0).upper()
        extractor = OUTCOME_EXTRACTORS.get(capability)
        if extractor is not None:
            contract["constraints"].update(extractor(text))
        # An unconstrained request may legitimately compile to several lower-
        # level tools (bulk creation, rename+group, composite playbooks). Exact
        # capability binding activates only when this module has concrete
        # outcome/identity facts that must survive compilation.
        contract["strict_capability"] = bool(contract["constraints"])
        return contract
    return {}


def validate_step(
    contract: dict | None, tool: str, arguments: dict | None,
) -> list[str]:
    """Return semantic mismatches between one proposed effect and its intent."""
    contract = contract or {}
    effects = contract.get("effects") or []
    if effects:
        args = arguments or {}
        matching = [
            effect
            for effect in effects
            if str(effect.get("capability") or "") == tool
            and _effect_identity_matches(effect.get("constraints") or {}, args)
        ]
        if not matching:
            return [
                "This step is not one of the exact effects in the landlord's "
                "compiled request. Do not add, omit, or retarget batch items.",
            ]
        return _validate_constraints(
            tool,
            args,
            matching[0].get("constraints") or {},
        )

    required = str(contract.get("required_capability") or "").strip()
    if not required:
        return []
    args = arguments or {}
    errors: list[str] = []
    if contract.get("strict_capability") and tool != required:
        errors.append(
            f"The landlord requested {required}, but this step uses {tool}. "
            "Prepare the requested capability instead; do not substitute a "
            "legal-document field or a different ledger operation.",
        )
        return errors

    return _validate_constraints(tool, args, contract.get("constraints") or {})


def _effect_identity_matches(constraints: dict, arguments: dict) -> bool:
    """Match the identity fields that distinguish effects in one batch."""
    for key in ("lease_number", "effective_date"):
        expected = str(constraints.get(key) or "").strip().casefold()
        supplied = str(arguments.get(key) or "").strip().casefold()
        if expected and supplied != expected:
            return False
    return True


def _validate_constraints(tool: str, args: dict, constraints: dict) -> list[str]:
    errors: list[str] = []
    exact_lease = str(constraints.get("lease_number") or "").strip()
    supplied_lease = str(args.get("lease_number") or "").strip()
    if exact_lease and supplied_lease.casefold() != exact_lease.casefold():
        errors.append(
            f"The request names lease {exact_lease}; bind the preview to that "
            "exact lease.",
        )
    target_total = str(constraints.get("target_lease_total") or "").strip()
    if tool == "apply_rent_adjustment" and target_total:
        supplied = _money(str(args.get("target_lease_total") or ""))
        if supplied != target_total:
            errors.append(
                "The landlord requested a final first-month household total of "
                f"${target_total}. Pass target_lease_total={target_total} and let "
                "the backend calculate the discount from live allocations; do "
                "not supply a guessed flat amount.",
            )
        if str(args.get("amount") or "").strip():
            errors.append(
                "A target-total request must not carry a model-computed amount.",
            )
    effective_date = str(constraints.get("effective_date") or "").strip()
    supplied_effective_date = str(args.get("effective_date") or "").strip()
    if effective_date and supplied_effective_date != effective_date:
        errors.append(
            f"The requested billing period begins {effective_date}; bind the "
            "preview to that exact effective_date.",
        )
    return errors


def contract_for_effects(source_text: str, effects: list[dict]) -> dict:
    """Build a persisted one-to-one contract for a deterministic batch.

    Each effect is a validated backend capability plus its frozen identity and
    requested final state.  The confirmation runner checks both that every plan
    step belongs to this set and that every requested effect appears exactly
    once.
    """
    return {
        "version": 2,
        "required_capability": "batch",
        "source_text": str(source_text or "")[:1000],
        "strict_capability": True,
        "effects": [
            {
                "capability": str(effect.get("capability") or ""),
                "constraints": dict(effect.get("constraints") or {}),
            }
            for effect in effects
        ],
    }


def validate_effect_set(contract: dict | None, steps: list[dict]) -> list[str]:
    """Ensure a compiled batch neither drops nor duplicates requested effects."""
    effects = (contract or {}).get("effects") or []
    if not effects:
        return []
    errors: list[str] = []
    for index, effect in enumerate(effects, start=1):
        capability = str(effect.get("capability") or "")
        constraints = effect.get("constraints") or {}
        matches = [
            step
            for step in steps
            if str(step.get("tool") or "") == capability
            and _effect_identity_matches(
                constraints,
                step.get("arguments") or {},
            )
            and not _validate_constraints(
                capability,
                step.get("arguments") or {},
                constraints,
            )
        ]
        if len(matches) != 1:
            lease = constraints.get("lease_number") or f"effect {index}"
            errors.append(
                f"Compiled request effect {lease} must appear exactly once; "
                f"found {len(matches)} matching steps.",
            )
    return errors


def attach_contract(plan_payload: dict, contract: dict | None) -> dict:
    """Copy a contract into a payload without mutating caller-owned data."""
    payload = dict(plan_payload or {})
    if contract:
        payload["intent_contract"] = contract
    return payload
