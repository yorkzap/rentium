"""
Playbooks — deterministic multi-step recipes over the guarded tool surface.

A playbook turns "landlord intent + a set of targets" into an ordered list of
steps, where every step is a call to an EXISTING registry tool (each with its
own guardrails, re-validated at execution time). The model never authors
steps; it only picks a playbook and filters. Adding a new chain — even one
that isn't a single existing Rentium feature — is one compose function here.

The pipeline (all Python, no LLM):
  1. enumerate targets  (finder internals / resolve_property)
  2. partition          (tool_meta blockers → actionable vs blocked+reason)
  3. compose steps      (playbook; blocked-aware)
  4. return a structured plan; the runner (plan_runner) executes it after
     the landlord confirms — steps flagged requires_own_confirm (from
     TOOL_META, e.g. terminate_lease) always pause for their own "yes".

The plan JSON contract is deliberately provider-neutral: a future smarter
"decision layer" model can emit step lists into the same validator/runner
without touching execution or guardrails.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain_crud import lease_terminate_blockers, property_delete_blockers
from .tool_meta import meta_for

# Lease statuses that can never be removed from a property: the lease row is
# audit history, it PROTECTs the property, and it can be neither terminated
# again nor deleted. A listing with one of these can never be deleted.
_FINAL_LEASE_STATUSES = ("TERMINATED", "EXPIRED", "RENEWED")


@dataclass(frozen=True)
class Step:
    tool: str
    arguments: dict
    target_label: str
    item_key: str
    note: str = ""

    @property
    def requires_own_confirm(self) -> bool:
        return meta_for(self.tool).own_confirm

    def to_dict(self, n: int) -> dict:
        return {
            "n": n,
            "tool": self.tool,
            "arguments": self.arguments,
            "target": self.target_label,
            "item_key": self.item_key,
            "requires_own_confirm": self.requires_own_confirm,
            **({"note": self.note} if self.note else {}),
        }


@dataclass
class Composition:
    steps: list[Step] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)


def _blocked(target: str, reason: str, detail: str, options: list[str], **extra) -> dict:
    return {
        "target": target,
        "reason": reason,
        "detail": detail,
        "options": options,
        **extra,
    }


# ---------------------------------------------------------------- targets
def _enumerate_listing_targets(
    landlord,
    *,
    include: str = "",
    pick: str = "",
    has_images: str = "",
    vacant_today: str = "",
    has_lease: str = "",
    listing_status: str = "",
    group: str = "",
    name_contains: str = "",
    exclude: str = "",
) -> tuple[list, list[dict], list[dict], str]:
    """(properties, unresolved, ambiguous, match_rule). include beats filters.

    `unresolved` = tokens that matched NOTHING (a real error). `ambiguous` =
    tokens that matched MORE THAN ONE listing — these are NOT blocked, the
    operation is still possible once the landlord picks one, so they are kept
    separate (each carries its `candidates` so the caller can offer a choice or
    a pick=oldest|newest follow-up). `pick` is applied to every include token.
    """
    from rentium.properties.models import Property

    from .resolve import resolve_property

    tokens = [t.strip() for t in (include or "").split(",") if t.strip()]
    if tokens:
        props, unresolved, ambiguous = [], [], []
        for tok in tokens:
            prop, err = resolve_property(landlord, tok, pick=pick)
            if not err:
                props.append(prop)
            elif isinstance(err, dict) and err.get("candidates"):
                ambiguous.append({"query": tok, "candidates": err["candidates"]})
            else:
                msg = err.get("error", "") if isinstance(err, dict) else str(err)
                unresolved.append(
                    _blocked(tok, "unresolved", msg, ["give the exact name or id"])
                )
        rule = f"Explicit list: {len(props)} of {len(tokens)} resolved."
        return props, unresolved, ambiguous, rule

    from .domain_reads import find_listings

    found = find_listings(
        landlord,
        has_images=has_images,
        vacant_today=vacant_today,
        has_lease=has_lease,
        listing_status=listing_status,
        group=group,
        name_contains=name_contains,
        exclude=exclude,
    )
    ids = [row["id"] for row in found["listings"]]
    by_id = {str(p.pk): p for p in Property.objects.filter(landlord=landlord, pk__in=ids)}
    props = [by_id[i] for i in ids if i in by_id]
    return props, [], [], found["match_rule"]


def _enumerate_unit_targets(landlord, *, include: str = "", holding: str = ""):
    """(units, unresolved, match_rule) for unit-scoped playbooks.

    Units resolve by their own name ("Main Floor"), optionally narrowed by
    holding name OR street address, because a landlord almost always has
    several "Main Floor"s / "Garden Suite"s across a portfolio. An ambiguous
    name is reported as unresolved WITH its candidates rather than silently
    taking the first — picking the wrong floor to restructure is not
    recoverable by re-running.
    """
    from django.db.models import Q

    from rentium.properties.models import PropertyUnit

    from .unit_structure import _resolve_unit

    qs = PropertyUnit.objects.filter(landlord=landlord).select_related("holding")
    holding_q = (holding or "").strip()
    if holding_q:
        # Holding display names ("McKenzie House") often differ from the street
        # the landlord types ("950 McKenzie Ave"). Match both.
        qs = qs.filter(
            Q(holding__name__icontains=holding_q)
            | Q(holding__address__icontains=holding_q),
        )

    tokens = [t.strip() for t in (include or "").split(",") if t.strip()]
    if not tokens:
        units = list(qs.order_by("holding__name", "name"))
        rule = f"All units{f' in {holding}' if holding_q else ''}: {len(units)}."
        return units, [], rule

    units, unresolved = [], []
    for tok in tokens:
        # Prefer the shared scorer so "Garden Suite 950 McKenzie" and bare
        # "Garden Suite" + holding= both resolve the same way tools do.
        unit, err = _resolve_unit(landlord, tok, holding=holding_q)
        if unit is not None:
            units.append(unit)
            continue
        if isinstance(err, dict) and err.get("candidates"):
            unresolved.append(
                _blocked(
                    tok,
                    "ambiguous",
                    err.get("error") or f"{tok!r} matches several units.",
                    ["narrow with holding=<address or house name>",
                     "pass the unit id"],
                    candidates=err.get("candidates"),
                )
            )
        else:
            msg = err.get("error", "") if isinstance(err, dict) else str(err or "")
            unresolved.append(
                _blocked(
                    tok,
                    "unresolved",
                    msg or f"No unit matching {tok!r}.",
                    ["give the exact unit name, or narrow with holding="],
                )
            )
    seen, deduped = set(), []
    for u in units:
        if u.pk not in seen:
            seen.add(u.pk)
            deduped.append(u)
    rule = f"Named units: {len(deduped)} of {len(tokens)} resolved."
    return deduped, unresolved, rule


# -------------------------------------------------------------- playbooks
def _compose_delete_listings(landlord, props, params) -> Composition:
    comp = Composition()
    for prop in props:
        blockers = property_delete_blockers(prop)
        if blockers:
            b = blockers[0]
            options = ["skip it", "retire it (NOT_AVAILABLE + hidden) instead"]
            if b["reason"] == "leases_protect":
                options.insert(
                    0,
                    "terminate its leases first (say: terminate their leases "
                    "then delete)",
                )
            comp.blocked.append(
                _blocked(
                    prop.name,
                    b["reason"],
                    b["detail"],
                    options,
                    leases=b.get("leases", []),
                )
            )
            continue
        comp.steps.append(
            Step(
                tool="delete_property",
                arguments={"property_query": str(prop.pk)},
                target_label=prop.name,
                item_key=str(prop.pk),
            )
        )
    return comp


def _retire_step(prop, why: str) -> Step:
    """Retire a listing that can never be deleted: NOT_AVAILABLE + hidden."""
    return Step(
        tool="update_property",
        arguments={
            "property_query": str(prop.pk),
            "status": "NOT_AVAILABLE",
            "is_publicly_visible": "no",
        },
        target_label=f"{prop.name} — retire (cannot be deleted: {why})",
        item_key=str(prop.pk),
        note=(
            "lease and work-order records are permanent audit history that "
            "PROTECT the listing, so it is retired (NOT_AVAILABLE + hidden) "
            "instead of deleted"
        ),
    )


def _compose_terminate_and_delete(landlord, props, params) -> Composition:
    """End each listing's leases; delete the listing only where truly possible.

    Reality of the data model: any NON-draft lease (active or finished) is an
    immutable audit record that PROTECTs its listing forever — terminating a
    lease produces exactly such a record. So true deletion only exists for
    listings whose leases are all DRAFTs (or that have none) and that have no
    work orders. Everything else keeps its listing AS-IS after the lease ends
    (the landlord can re-lease it later); retiring (NOT_AVAILABLE + hidden) is
    offered as an option, never done automatically.
    """
    from rentium.leases.models import Lease

    comp = Composition()
    for prop in props:
        leases = list(Lease.objects.filter(property=prop))
        drafts = [ls for ls in leases if ls.status == Lease.LeaseStatus.DRAFT]
        finals = [ls for ls in leases if ls.status in _FINAL_LEASE_STATUSES]
        live = [
            ls
            for ls in leases
            if ls not in drafts and ls not in finals and not lease_terminate_blockers(ls)
        ]

        for lease in live:
            comp.steps.append(
                Step(
                    tool="terminate_lease",
                    arguments={"lease_number": lease.lease_number},
                    target_label=f"{prop.name} — lease {lease.lease_number}",
                    item_key=str(prop.pk),
                    note="voids open charges, closes occupancy",
                )
            )
        for lease in drafts:
            comp.steps.append(
                Step(
                    tool="delete_draft_lease",
                    arguments={"lease_number": lease.lease_number},
                    target_label=f"{prop.name} — draft {lease.lease_number}",
                    item_key=str(prop.pk),
                    note="draft paperwork only",
                )
            )

        has_wos = prop.work_orders.exists()
        if live or finals or has_wos:
            if live:
                why = "its terminated lease will stay on record"
            elif finals:
                why = f"{len(finals)} finished lease record(s) reference it"
            else:
                why = "work orders reference it"
            comp.blocked.append(
                _blocked(
                    prop.name,
                    "becomes_protected",
                    (
                        f"{prop.name} cannot be deleted — {why} (permanent "
                        "audit history, DB PROTECT). The listing stays as-is "
                        "so it can be re-leased later."
                    ),
                    [
                        "leave it as-is (default)",
                        f"retire it (NOT_AVAILABLE + hidden) — say: retire {prop.name}",
                    ],
                )
            )
        else:
            comp.steps.append(
                Step(
                    tool="delete_property",
                    arguments={"property_query": str(prop.pk)},
                    target_label=prop.name,
                    item_key=str(prop.pk),
                )
            )
    return comp


def _compose_update_status(landlord, props, params) -> Composition:
    from rentium.properties.models import Property

    comp = Composition()
    new_status = (params.get("new_status") or "").strip().upper()
    valid = {s for s, _ in Property.PropertyStatus.choices}
    if new_status not in valid:
        comp.blocked.append(
            _blocked(
                "*",
                "bad_status",
                f"new_status must be one of {sorted(valid)} (got {new_status!r}).",
                ["pass a valid new_status"],
            )
        )
        return comp
    for prop in props:
        if prop.status == new_status:
            comp.blocked.append(
                _blocked(
                    prop.name,
                    "already",
                    f"{prop.name} is already {new_status}.",
                    ["skip it"],
                )
            )
            continue
        comp.steps.append(
            Step(
                tool="update_property",
                arguments={"property_query": str(prop.pk), "status": new_status},
                target_label=f"{prop.name}: {prop.status} → {new_status}",
                item_key=str(prop.pk),
            )
        )
    return comp


def _compose_retire_listings(landlord, props, params) -> Composition:
    """Take listings off the market without destroying them.

    _compose_terminate_and_delete has always TOLD the landlord this is the way
    out for a listing that lease history protects ("retire it — say: retire
    X"), but no playbook existed to do it, so the advice dead-ended and the
    model had to improvise a bulk update. This is that action.
    """
    comp = Composition()
    for prop in props:
        if prop.status == "NOT_AVAILABLE" and not prop.is_publicly_visible:
            comp.blocked.append(
                _blocked(
                    prop.name,
                    "already",
                    f"{prop.name} is already retired.",
                    ["skip it"],
                )
            )
            continue
        comp.steps.append(
            Step(
                tool="update_property",
                arguments={
                    "property_query": str(prop.pk),
                    "status": "NOT_AVAILABLE",
                    "is_publicly_visible": "no",
                },
                target_label=f"{prop.name} — retire",
                item_key=str(prop.pk),
                note="kept with all its history; hidden and marked unavailable",
            )
        )
    return comp


def _compose_set_visibility(landlord, props, params) -> Composition:
    """Publish or hide listings on the public site."""
    comp = Composition()
    raw = str(params.get("visible") or "").strip().casefold()
    if raw not in ("yes", "no", "true", "false", "1", "0"):
        comp.blocked.append(
            _blocked(
                "*",
                "bad_visible",
                "visible must be yes or no.",
                ["pass visible=yes or visible=no"],
            )
        )
        return comp
    wanted = raw in ("yes", "true", "1")

    for prop in props:
        if prop.is_publicly_visible == wanted:
            comp.blocked.append(
                _blocked(
                    prop.name,
                    "already",
                    f"{prop.name} is already {'visible' if wanted else 'hidden'}.",
                    ["skip it"],
                )
            )
            continue
        # Publishing something that can't actually appear is a silent no-op —
        # say so instead of queueing a step that changes nothing visible.
        blockers = prop.publish_blockers() if wanted else []
        if blockers:
            comp.blocked.append(
                _blocked(
                    prop.name,
                    "not_publishable",
                    f"{prop.name} can't go public yet: {'; '.join(blockers)}.",
                    ["fix the blockers first", "skip it"],
                )
            )
            continue
        comp.steps.append(
            Step(
                tool="update_property",
                arguments={
                    "property_query": str(prop.pk),
                    "is_publicly_visible": "yes" if wanted else "no",
                },
                target_label=(
                    f"{prop.name} — {'publish' if wanted else 'hide'}"
                ),
                item_key=str(prop.pk),
            )
        )
    return comp


def _compose_switch_rental_mode(landlord, units, params) -> Composition:
    """Change how whole units are rented, one step per unit.

    Unit-scoped rather than listing-scoped: "rent the Wascana floors room by
    room" is one intent over three physical spaces, and each may be blocked for
    its own reason. Partitioning here means the landlord sees exactly which
    ones can move and why the others can't, instead of a half-applied change.
    """
    from rentium.properties.models import PropertyUnit
    from rentium.properties.services import describe_rental_mode_switch

    comp = Composition()
    raw = str(params.get("new_mode") or "").strip().upper().replace(" ", "_")
    if raw in ("ROOMS", "ROOM_BY_ROOM", "BY_ROOM"):
        mode = PropertyUnit.RentalMode.BY_ROOM
    elif raw in ("WHOLE", "UNIT", "ENTIRE", "WHOLE_UNIT"):
        mode = PropertyUnit.RentalMode.WHOLE_UNIT
    else:
        comp.blocked.append(
            _blocked(
                "*",
                "bad_mode",
                "new_mode must be WHOLE_UNIT or BY_ROOM.",
                ["pass new_mode=WHOLE_UNIT or new_mode=BY_ROOM"],
            )
        )
        return comp

    for unit in units:
        preview = describe_rental_mode_switch(unit, mode)
        label = f"{unit.name} ({unit.holding.name})"
        if "error" in preview:
            comp.blocked.append(
                _blocked(label, "already", preview["error"], ["skip it"])
            )
            continue
        if preview["blocked_by"]:
            names = ", ".join(
                f"{b['lease_number']} ({b['status']})" for b in preview["blocked_by"]
            )
            comp.blocked.append(
                _blocked(
                    label,
                    "live_leases",
                    f"{label} has live leases: {names}. How a unit is rented "
                    "cannot change underneath a signed or pending agreement.",
                    ["end those leases first", "skip it"],
                    leases=preview["blocked_by"],
                )
            )
            continue
        # Always pin the resolved unit UUID. Re-resolving by bare name at
        # execution time is what produced "Several units match 'Garden Suite'"
        # after the landlord already confirmed a plan that named the address.
        comp.steps.append(
            Step(
                tool="set_unit_rental_mode",
                arguments={
                    "unit_name": str(unit.pk),
                    "rental_mode": mode,
                    "holding": unit.holding.address or unit.holding.name,
                },
                target_label=f"{label} -> {mode}",
                item_key=str(unit.pk),
                note=(
                    f"parks {len(preview['will_park'])} listing(s); "
                    + (
                        f"brings back {len(preview['will_reactivate'])}"
                        if preview["will_reactivate"]
                        else "a new listing will be needed"
                    )
                ),
            )
        )
    return comp


PLAYBOOKS = {
    "delete_listings": {
        "compose": _compose_delete_listings,
        "describe": "Delete the matching listings",
    },
    "retire_listings": {
        "compose": _compose_retire_listings,
        "describe": (
            "Take the matching listings off the market (NOT_AVAILABLE + hidden) "
            "while keeping them and all their history"
        ),
    },
    "set_visibility": {
        "compose": _compose_set_visibility,
        "describe": "Publish or hide the matching listings (needs visible)",
    },
    "switch_rental_mode": {
        "compose": _compose_switch_rental_mode,
        "describe": "Change how whole units are rented (needs new_mode)",
        "targets": "units",
    },
    "terminate_and_delete": {
        "compose": _compose_terminate_and_delete,
        "describe": (
            "End each listing's leases; delete the listing only where no "
            "lease/work-order history protects it (protected listings stay "
            "as-is; retiring is offered, never automatic)"
        ),
    },
    "update_status": {
        "compose": _compose_update_status,
        "describe": "Set a new listing status on the matching listings",
    },
}


# --------------------------------------------------- disambiguation payload
def _describe_candidate(c: dict) -> str:
    age = (c.get("created_at") or "")[:10]
    grp = f", group {c['group']}" if c.get("group") else ""
    leases = c.get("lease_count", 0)
    lease_txt = f", {leases} lease(s)" if leases else ", no leases"
    return f"{c['name']} [id {c['id']}, created {age}{grp}{lease_txt}]"


def _disambiguation_payload(operation: str, ambiguous: list[dict], match_rule: str) -> dict:
    """A multi-match is NOT a blocker — it's a question. Return a distinct
    signal (never mixed into `blocked`) that names the duplicates and tells the
    model the deterministic way forward: re-call with pick=oldest|newest or an id.
    """
    lines = []
    for a in ambiguous:
        cands = "; ".join(_describe_candidate(c) for c in a["candidates"])
        lines.append(f"'{a['query']}' matches {len(a['candidates'])} listings — {cands}")
    detail = " | ".join(lines)
    question = (
        "More than one listing matches. Which one? Say the id, 'the old one' "
        f"(oldest), 'the new one' (newest), or 'both' to include all. {detail}"
    )
    return {
        "operation": operation,
        "needs_disambiguation": True,
        "ambiguous": ambiguous,
        "question_for_user": question,
        "match_rule": match_rule,
        "relay_instruction": (
            "These are DUPLICATE-NAMED listings, not blocked or undeletable items "
            "— the operation is still possible once the landlord picks one. Do NOT "
            "run any tool and do NOT offer to 'skip' them. Ask question_for_user "
            "verbatim, then STOP and wait. When the landlord answers, call this "
            "SAME tool again with the same scope plus pick=oldest|newest (or "
            "pick=all for 'both', or put the id in include)."
        ),
    }


_DESTRUCTIVE_OPS = ("delete_listings", "terminate_and_delete")


def _narrow_same_name_collisions(op, props, pick, match_rule):
    """Footgun guard: a destructive plan must never silently target 2+ listings
    that share an identical name (the "delete the McKenzie Room F" trap — the
    filter path legitimately matches BOTH twins). Returns (props, payload):
    when payload is set the caller must STOP and ask; otherwise props is the
    (possibly narrowed) set to plan over.

    - pick oldest/first  → keep the older of each same-named group
    - pick newest/last   → keep the newer of each same-named group
    - pick all/both      → keep everything (explicit escape hatch)
    - no usable pick      → disambiguation question (never a wrong bulk delete)
    Singletons are always kept, so ordinary bulk ops are unaffected unless a
    genuine name collision exists within the target set.
    """
    from .resolve import _candidate_row, _normalize_pick

    if op not in _DESTRUCTIVE_OPS or len(props) < 2:
        return props, None

    by_name: dict[str, list] = {}
    for p in props:
        by_name.setdefault(p.name.strip().lower(), []).append(p)
    collisions = {n: g for n, g in by_name.items() if len(g) > 1}
    if not collisions:
        return props, None

    norm = _normalize_pick(pick)
    if norm in ("oldest", "first", "newest", "last"):
        newest = norm in ("newest", "last")

        def chosen(group):
            return sorted(group, key=lambda x: (x.created_at, x.pk))[-1 if newest else 0]

        keep_ids = {chosen(g).pk for g in by_name.values()}
        return [p for p in props if p.pk in keep_ids], None
    if norm == "all":
        return props, None

    ambiguous = []
    for _name, g in collisions.items():
        g_sorted = sorted(g, key=lambda x: (x.created_at, x.pk))
        ambiguous.append(
            {"query": g_sorted[0].name, "candidates": [_candidate_row(p) for p in g_sorted]}
        )
    return props, _disambiguation_payload(op, ambiguous, f"{match_rule} — identical names")


# ----------------------------------------------------------- plan payload
def _plan_payload(operation: str, summary: str, comp: Composition) -> dict:
    steps = [s.to_dict(i + 1) for i, s in enumerate(comp.steps)]
    # "Already in that mode" is informational, not a decision. Don't make the
    # landlord re-confirm skipping basements that were never asked for.
    informational = {"already"}
    actionable_blocked = [
        b for b in comp.blocked if b.get("reason") not in informational
    ]
    payload: dict = {
        "operation": operation,
        "summary": summary,
        "steps": steps,
        "blocked": comp.blocked,
    }
    out: dict = {"plan": payload}
    if comp.steps:
        out["needs_confirm"] = True
    if actionable_blocked:
        names = ", ".join(b["target"] for b in actionable_blocked[:8])
        payload["question_for_user"] = (
            f"These are blocked: {names}. Should I skip them, or do you want "
            "to handle them differently (see each item's options)?"
        )
    elif comp.blocked and not comp.steps:
        # Everything was already done — report that, no yes needed.
        payload["question_for_user"] = (
            "Nothing to change — every matched unit is already in the "
            "requested state (see blocked items)."
        )
    own = [s for s in steps if s["requires_own_confirm"]]
    relay = [
        "Show the landlord the FULL plan: every numbered step and every "
        "blocked item with its reason. Do not run any other tool. ",
    ]
    if comp.steps:
        relay.append(
            "Then ask them to confirm; one 'yes' runs the plan. "
        )
    if own:
        relay.append(
            f"{len(own)} step(s) are lease terminations or similar — the "
            "system will pause at each for its own confirmation. "
        )
    if actionable_blocked or (comp.blocked and not comp.steps):
        relay.append("Ask question_for_user verbatim. ")
    relay.append("Then STOP and wait.")
    out["relay_instruction"] = "".join(relay)
    return out


# ------------------------------------------------------------ model tools
def plan_operation(
    landlord,
    *,
    operation: str,
    include: str = "",
    pick: str = "",
    exclude: str = "",
    has_images: str = "",
    vacant_today: str = "",
    has_lease: str = "",
    listing_status: str = "",
    group: str = "",
    name_contains: str = "",
    new_status: str = "",
    new_mode: str = "",
    visible: str = "",
    holding: str = "",
    confirm: str = "",
) -> dict:
    """Build a multi-step PLAN over a set of listings or units. ALWAYS use this
    (never a hand-rolled sequence of tools) when the landlord asks for a bulk
    or multi-step operation.

    operation, over LISTINGS: delete_listings | terminate_and_delete
    (terminate/remove leases first, then delete) | retire_listings (take off
    the market but keep everything) | update_status (needs new_status) |
    set_visibility (needs visible=yes|no).
    operation, over UNITS: switch_rental_mode (needs new_mode=WHOLE_UNIT or
    BY_ROOM; scope with include='Main Floor, Basement' and holding='950
    McKenzie Ave').

    Scope listings with include='name, name' OR filters (has_images yes/no,
    vacant_today, has_lease, listing_status, group, name_contains) plus
    exclude='name, name'. If an include name matches two listings (duplicates)
    the result asks which one — re-call with pick=oldest|newest (or put the id
    in include) to target 'the old one' / 'the new one'. Returns the full plan
    with any blocked items; the system handles confirmation and execution —
    show the plan, then stop."""
    op = (operation or "").strip().lower()
    playbook = PLAYBOOKS.get(op)
    if playbook is None:
        return {
            "error": (
                f"Unknown operation {operation!r}. "
                f"One of: {', '.join(sorted(PLAYBOOKS))}."
            )
        }

    # Unit-scoped playbooks enumerate physical spaces, not listings: "rent the
    # Wascana floors room by room" is one intent over three units.
    if playbook.get("targets") == "units":
        units, unresolved, match_rule = _enumerate_unit_targets(
            landlord, include=include, holding=holding
        )
        if not units and not unresolved:
            return {"result": "No units matched.", "match_rule": match_rule}
        comp = playbook["compose"](
            landlord, units, {"new_mode": new_mode, "visible": visible}
        )
        comp.blocked = unresolved + comp.blocked
        n_items = len({s.item_key for s in comp.steps})
        summary = (
            f"{playbook['describe']}: {n_items} unit(s), {len(comp.steps)} step(s)"
            + (f"; {len(comp.blocked)} blocked" if comp.blocked else "")
            + f". {match_rule}"
        )
        return _plan_payload(op, summary, comp)

    props, unresolved, ambiguous, match_rule = _enumerate_listing_targets(
        landlord,
        include=include,
        pick=pick,
        has_images=has_images,
        vacant_today=vacant_today,
        has_lease=has_lease,
        listing_status=listing_status,
        group=group,
        name_contains=name_contains,
        exclude=exclude,
    )
    # A duplicate-name match is a QUESTION, not a plan and not a blocker. Ask it
    # before building/running anything, so we never delete the wrong twin.
    if ambiguous:
        return _disambiguation_payload(op, ambiguous, match_rule)
    if not props and not unresolved:
        return {
            "result": "No listings matched.",
            "match_rule": match_rule,
        }

    # Footgun guard: even when the set came from a filter, never let a
    # destructive plan silently target two identically-named listings.
    props, collision_payload = _narrow_same_name_collisions(op, props, pick, match_rule)
    if collision_payload is not None:
        return collision_payload

    comp = playbook["compose"](
        landlord, props, {"new_status": new_status, "visible": visible}
    )
    comp.blocked = unresolved + comp.blocked

    n_items = len({s.item_key for s in comp.steps})
    summary = (
        f"{playbook['describe']}: {n_items} listing(s), "
        f"{len(comp.steps)} step(s)"
        + (f"; {len(comp.blocked)} blocked" if comp.blocked else "")
        + f". {match_rule}"
    )
    return _plan_payload(op, summary, comp)


def plan_move_tenant(
    landlord,
    *,
    tenant: str,
    from_property: str,
    to_property: str,
    start_date: str = "",
    total_rent: str = "",
    pick: str = "",
    confirm: str = "",
) -> dict:
    """Build a PLAN that moves a tenant between two of this landlord's rooms:
    terminate the current lease (pauses for its own confirmation), then set up
    the new tenancy on the target room (lease + signing invite + move-in
    inspection). tenant: name or email. start_date YYYY-MM-DD (default today);
    total_rent defaults to the old lease's rent. Show the plan, then stop."""
    from rentium.leases.models import Lease

    from .resolve import resolve_property

    # `pick` reaches resolve_property. It was previously absent from the
    # signature, so the registry dropped it: the model was told "multiple
    # listings match — pass pick", passed pick=oldest, and got the identical
    # error back with no way forward.
    src, err = resolve_property(landlord, from_property, pick=pick)
    if err:
        return {"error": f"from_property: {err}"}
    dst, err = resolve_property(landlord, to_property, pick=pick)
    if err:
        return {"error": f"to_property: {err}"}
    if src.pk == dst.pk:
        return {"error": "from_property and to_property are the same listing."}

    lease = (
        Lease.objects.filter(landlord=landlord, property=src)
        .exclude(status__in=_FINAL_LEASE_STATUSES)
        .order_by("-created_at")
        .first()
    )
    if lease is None:
        return {"error": f"No open lease on {src.name} to move anyone from."}

    comp = Composition()
    term_blockers = lease_terminate_blockers(lease)
    if lease.status == Lease.LeaseStatus.DRAFT:
        comp.steps.append(
            Step(
                tool="delete_draft_lease",
                arguments={"lease_number": lease.lease_number},
                target_label=f"{src.name} — draft {lease.lease_number}",
                item_key=f"move:{src.pk}",
                note="draft paperwork only",
            )
        )
    elif term_blockers:
        comp.blocked.append(
            _blocked(src.name, term_blockers[0]["reason"], term_blockers[0]["detail"], ["skip"])
        )
    else:
        comp.steps.append(
            Step(
                tool="terminate_lease",
                arguments={"lease_number": lease.lease_number},
                target_label=f"{src.name} — lease {lease.lease_number}",
                item_key=f"move:{src.pk}",
                note="voids open charges, closes occupancy",
            )
        )

    rent = (total_rent or "").strip() or str(lease.total_rent)
    # setup_room_tenancy only creates the lease when start_date is present —
    # a move always needs the new lease, so default to today.
    from datetime import date as _date

    start = (start_date or "").strip() or _date.today().isoformat()
    tenant_str = (tenant or "").strip()
    is_email = "@" in tenant_str
    comp.steps.append(
        Step(
            tool="setup_room_tenancy",
            arguments={
                "room_name": dst.name,
                "address": dst.address or "",
                "city": dst.city or "",
                "group_name": dst.group.name if dst.group_id else "",
                "use_existing_if_name_matches": "1",
                "start_date": start,
                "total_rent": rent,
                "tenant_name": "" if is_email else tenant_str,
                "tenant_email": tenant_str if is_email else "",
            },
            target_label=f"{dst.name} — new tenancy for {tenant_str}",
            item_key=f"move:{dst.pk}",
            note="new lease + signing invite + move-in inspection",
        )
    )

    summary = (
        f"Move {tenant_str} from {src.name} to {dst.name}: end the current "
        f"lease, then set up the new tenancy at ${rent}."
    )
    return _plan_payload("move_tenant", summary, comp)
