"""
The landlord's Constitution — written policy the General reads verbatim.

Prose sections (markdown, versioned append-only) carry nuance; structured
rules carry the machine-enforceable subset that $0 sentinels compare against.
Amendments — whether typed by the landlord in the UI or proposed by the
General in chat — always create a NEW version and deactivate the old one;
nothing is ever edited in place.
"""

from __future__ import annotations

import json

from django.db import transaction

from .models import RamaConstitutionRule, RamaConstitutionSection

SECTION_KEYS = ("balances", "vendors", "tenant-policies", "workflows")

_VALID_RULE_TYPES = {c for c, _ in RamaConstitutionRule.RuleType.choices}


import re

# --------------------------------------------------- unlawful-policy guard
# The Constitution is free text that RAMA reads back as the landlord's policy,
# so whatever is written here it will later act on and advise from. That makes
# one topic dangerous to get wrong.
#
# Asked "whose deposit does this come out of?", RAMA proposed writing:
#     "Tenant-caused damage ... may be deducted from their security deposit
#      after move-out."
# Under the BC RTA that is simply not true. A landlord may keep deposit money
# only with the tenant's WRITTEN agreement or by applying to the RTB within 15
# days — and getting it wrong loses the claim AND makes double the deposit
# payable. Enshrining it would have had RAMA confidently repeat it forever.
#
# The guard does not ban the subject. It requires that any rule about taking
# money from a deposit also state the condition that makes it lawful.
_DEPOSIT_WORDS = re.compile(
    r"\b(security |pet |damage )?deposit(s)?\b", re.IGNORECASE
)
_TAKING_WORDS = re.compile(
    r"\b(deduct|deducted|deduction|withhold|withheld|retain|retained|keep|"
    r"kept|apply|applied|offset|take|taken)\b",
    re.IGNORECASE,
)
_SAFEGUARD_WORDS = re.compile(
    r"(written agreement|agrees? in writing|in writing|rtb|residential "
    r"tenancy branch|dispute resolution|arbitration|15 day|fifteen day)",
    re.IGNORECASE,
)


def unlawful_deposit_language(text: str) -> str | None:
    """Reason to refuse this text, or None.

    Only fires when a passage talks about TAKING money from a deposit without
    naming the condition that makes it lawful.
    """
    body = text or ""
    if not body.strip():
        return None
    for chunk in re.split(r"[\n.;]+", body):
        if not _DEPOSIT_WORDS.search(chunk):
            continue
        if not _TAKING_WORDS.search(chunk):
            continue
        if _SAFEGUARD_WORDS.search(chunk):
            continue
        return (
            "This would record that deposit money can be kept or deducted "
            "without saying what makes that lawful. Under the BC RTA a "
            "landlord may keep any part of a deposit ONLY with the tenant's "
            "written agreement, or by applying to the RTB within 15 days of "
            "the later of the tenancy ending and receiving their forwarding "
            "address. Missing that loses the claim AND makes double the "
            "deposit payable — so a policy phrased this way would be acted on "
            "and would cost the landlord money.\n\n"
            f"Offending passage: {chunk.strip()[:200]!r}"
        )
    return None


def active_sections(landlord):
    return RamaConstitutionSection.objects.filter(landlord=landlord, is_active=True)


def active_rules(landlord, rule_type: str | None = None):
    qs = RamaConstitutionRule.objects.filter(landlord=landlord, active=True)
    if rule_type:
        qs = qs.filter(rule_type=rule_type)
    return qs


def render_for_prompt(landlord) -> str:
    """The Constitution as injected into the General's system prompt."""
    sections = list(active_sections(landlord).order_by("key"))
    rules = list(active_rules(landlord))
    if not sections and not rules:
        return (
            "## THE CONSTITUTION\n(empty — the landlord has not written any "
            "policies yet. Offer to draft sections with them: balances, "
            "vendors, tenant-policies, workflows.)"
        )
    parts = ["## THE CONSTITUTION (authoritative — overrides your own judgment)"]
    for s in sections:
        parts.append(f"### {s.title} [{s.key} v{s.version}]\n{s.body_md}".rstrip())
    if rules:
        parts.append("### Structured rules (enforced automatically by sentinels)")
        for r in rules:
            parts.append(f"- {r.rule_type}: {json.dumps(r.params, default=str)}")
    return "\n\n".join(parts)


def section_payload(landlord) -> dict:
    """JSON-safe dump for tools and the frontend editor."""
    return {
        "sections": [
            {
                "key": s.key,
                "title": s.title,
                "version": s.version,
                "body_md": s.body_md,
                "origin": s.origin,
                "updated": str(s.created_at.date()),
            }
            for s in active_sections(landlord).order_by("key")
        ],
        "rules": [
            {
                "id": r.pk,
                "rule_type": r.rule_type,
                "params": r.params,
                "section": r.section.key if r.section_id else None,
            }
            for r in active_rules(landlord)
        ],
    }


def parse_rule_changes(rule_changes: str) -> tuple[list[dict], str | None]:
    """Validate the JSON rule_changes payload. Returns (changes, error)."""
    raw = (rule_changes or "").strip()
    if not raw:
        return [], None
    try:
        changes = json.loads(raw)
    except ValueError:
        return [], "rule_changes must be valid JSON (a list of change objects)."
    if isinstance(changes, dict):
        changes = [changes]
    if not isinstance(changes, list):
        return [], "rule_changes must be a JSON list."
    for i, ch in enumerate(changes, start=1):
        if not isinstance(ch, dict):
            return [], f"rule_changes[{i}] must be an object."
        action = ch.get("action")
        if action not in ("add", "remove", "update"):
            return [], f"rule_changes[{i}].action must be add|remove|update."
        if action == "add" and ch.get("rule_type") not in _VALID_RULE_TYPES:
            return [], (
                f"rule_changes[{i}].rule_type must be one of "
                f"{sorted(_VALID_RULE_TYPES)}."
            )
        if action in ("remove", "update") and not ch.get("rule_id"):
            return [], f"rule_changes[{i}] needs rule_id."
    return changes, None


@transaction.atomic
def amend(
    landlord,
    *,
    key: str,
    title: str = "",
    body_md: str = "",
    rule_changes: list[dict] | None = None,
    origin: str = RamaConstitutionSection.Origin.LANDLORD,
) -> dict:
    """Create the next version of a section (+apply rule changes). Append-only."""
    key = (key or "").strip().lower()
    current = (
        RamaConstitutionSection.objects.filter(
            landlord=landlord, key=key, is_active=True
        )
        .order_by("-version")
        .first()
    )
    version = (current.version + 1) if current else 1
    section = RamaConstitutionSection.objects.create(
        landlord=landlord,
        key=key,
        title=(title or (current.title if current else key.replace("-", " ").title()))[:200],
        body_md=body_md if body_md else (current.body_md if current else ""),
        version=version,
        origin=origin,
        supersedes=current,
    )
    if current:
        current.is_active = False
        current.save(update_fields=["is_active"])
        # Rules keep pointing at their (now superseded) section for history;
        # active rules attach to the new version.
        RamaConstitutionRule.objects.filter(
            landlord=landlord, section=current, active=True
        ).update(section=section)

    applied = []
    for ch in rule_changes or []:
        action = ch["action"]
        if action == "add":
            rule = RamaConstitutionRule.objects.create(
                landlord=landlord,
                rule_type=ch["rule_type"],
                params=ch.get("params") or {},
                section=section,
            )
            applied.append({"added": rule.pk, "rule_type": rule.rule_type})
        else:
            rule = RamaConstitutionRule.objects.filter(
                landlord=landlord, pk=ch.get("rule_id")
            ).first()
            if rule is None:
                applied.append({"error": f"no rule {ch.get('rule_id')}"})
                continue
            if action == "remove":
                rule.active = False
                rule.save(update_fields=["active", "updated_at"])
                applied.append({"removed": rule.pk})
            else:  # update
                rule.params = ch.get("params") or rule.params
                if ch.get("rule_type") in _VALID_RULE_TYPES:
                    rule.rule_type = ch["rule_type"]
                rule.save(update_fields=["params", "rule_type", "updated_at"])
                applied.append({"updated": rule.pk})

    return {
        "amended": True,
        "section": {"key": section.key, "version": section.version, "title": section.title},
        "rule_changes": applied,
    }
