"""
Generic write primitive over the Domain Capability Manifest — Phase 3.

`update(entity, query, changes, confirm)` sets manifest-declared editable fields
on ONE instance the landlord owns. Safety is structural and layered:

  1. Scope — the instance is resolved from `Model.objects.filter({scope_path:
     landlord})`, so you can only ever edit your own rows.
  2. Default-deny fields — only fields with editable=True in the manifest can be
     set; everything else (ledger amounts, tokens, FSM status) is unreachable.
  3. State guard — the entity's edit_guard blocks edits by state (e.g. a locked
     lease), mirroring the LeaseNotLocked permission.
  4. Preview + confirm — every write previews first and only applies on
     confirm=yes, through the same deterministic confirm machine as bespoke
     tools; the tool call is audited.
  5. Validation — Django field validators run before save; a bad value is
     reported, not persisted.
"""

from __future__ import annotations

import re

from .domain_read import _coerce  # shared type coercion
from .manifest import MANIFEST, EntitySpec


def _split_change_clauses(changes: str, fields) -> list[str]:
    """Split "a=1, b=2" into clauses WITHOUT breaking values that contain commas.

    Splitting on every comma first was the single biggest source of RAMA write
    failures: 32 of 53 logged `update` errors were one call,

        changes='description=1 bedroom, living room, kitchen, private patio'

    which is a correct call. The old parser cut it at each comma, parsed
    'description=1 bedroom', then rejected 'living room' as a malformed clause.
    Free text almost always contains commas, so `description`, `name` and
    `address` were close to unusable.

    So a comma only starts a new clause when what follows is an actual known
    field name followed by '='. Anything else is part of the current value.
    Longest names first, so `postal_code` wins over any prefix of it.
    """
    text = (changes or "").strip()
    if not text:
        return []
    names = sorted((f for f in fields), key=len, reverse=True)
    if not names:
        return [c.strip() for c in text.split(",") if c.strip()]
    boundary = "|".join(re.escape(n) for n in names)
    parts = re.split(rf",\s*(?=(?:{boundary})\s*=)", text)
    return [p.strip() for p in parts if p.strip()]


def _clause_error(clause: str, fields) -> dict:
    """A rejection the model can act on: what it sent, what is editable, and a
    worked example. A bare "use field=value" told it nothing it didn't know."""
    listed = ", ".join(sorted(fields)) if fields else "(none)"
    example = next(iter(sorted(fields)), "name")
    return {
        "error": (
            f"Couldn't read {clause!r} as a change. Use field=value, and "
            f"separate several changes with commas — a value may itself "
            f"contain commas."
        ),
        "editable_fields": listed,
        "example": f"changes='{example}=new value'",
    }


def _parse_change_clauses(changes: str, fields=()) -> tuple[dict[str, str], dict | None]:
    parsed = {}
    for clause in _split_change_clauses(changes, fields):
        if "=" not in clause:
            return {}, _clause_error(clause, fields)
        fname, raw = clause.split("=", 1)
        parsed[fname.strip()] = raw.strip()
    return parsed, None


def _route_structured_property_update(
    landlord, *, query: str, changes: str, confirm: str
) -> dict | None:
    """Keep property type/layout edits on the domain-aware update path.

    Weak models sometimes call the generic editor with UI vocabulary such as
    ``listing_type=Full Unit``. Accept those aliases, but execute through
    ``update_property`` so category cleanup, lease guards and full validation
    cannot be bypassed.
    """
    aliases = {
        "listing_type": "property_category",
        "property_type": "property_category",
        "category": "property_category",
    }
    structured = {
        "property_category",
        "unit_type",
        "room_type",
        "bedrooms",
        "bathrooms",
        "max_occupancy",
        "square_footage",
    }
    known = (
        structured
        | set(aliases)
        | {
            "name", "status", "description", "address", "city", "province",
            "postal_code", "asking_rent", "is_publicly_visible", "pick",
        }
    )
    raw_changes, err = _parse_change_clauses(changes, known)
    if err:
        return err
    normalized = {aliases.get(k, k): v for k, v in raw_changes.items()}
    if not structured.intersection(normalized):
        return None

    supported = structured | {
        "name",
        "status",
        "description",
        "address",
        "city",
        "province",
        "asking_rent",
        "is_publicly_visible",
        "pick",
    }
    unsupported = sorted(set(normalized) - supported)
    if unsupported:
        return {
            "error": "This structured property correction also included unsupported "
            f"fields: {', '.join(unsupported)}."
        }

    from .domain_crud import update_property

    return update_property(
        landlord,
        property_query=query,
        confirm=confirm,
        **normalized,
    )


def _preview(action, preview, how):
    from .domain_crud import _preview as _p
    return _p(action, preview, how)


def _confirmed(confirm):
    from .domain_crud import _confirmed as _c
    return _c(confirm)


def _resolve_one(landlord, spec: EntitySpec, query: str):
    """Resolve exactly one instance the landlord owns, by the manifest lookup
    fields. Returns (instance, error_dict|None)."""
    from django.apps import apps
    from django.db.models import Q

    lookup = spec.resolve_lookup()
    if not lookup:
        return None, {"error": f"{spec.key} can't be targeted by name yet."}
    label = spec.resolve_label()
    Model = apps.get_model(*spec.model.split("."))
    qs = Model.objects.filter(spec.scope_q(landlord))
    q = (query or "").strip()
    if q:
        cond = Q()
        for f in lookup:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    matches = list(qs.order_by(spec.default_order)[:6])
    if not matches:
        return None, {"error": f"No {spec.key} matching {query!r} in your portfolio."}
    if len(matches) > 1:
        return None, {
            "error": f"Several {spec.key}s match {query!r} — be more specific.",
            "options": [str(getattr(m, label, m.pk)) for m in matches],
        }
    return matches[0], None


def _resolve_choice(inst, fname: str, raw: str):
    """Map a user value ('Fair', 'fair', 'FAIR') to a valid enum choice code, so
    an enum edit passes model validation. Returns (code, error)."""
    field_obj = inst._meta.get_field(fname)
    choices = list(field_obj.choices or [])
    val = raw.strip()
    for code, label in choices:
        if val.lower() in (str(code).lower(), str(label).lower()):
            return code, None
    valid = ", ".join(str(label) for _, label in choices)
    return None, f"{fname} must be one of: {valid}."


def update(landlord, *, entity: str = "", query: str = "", changes: str = "",
           confirm: str = "") -> dict:
    spec = MANIFEST.get((entity or "").strip().lower())
    if spec is None:
        editable = [k for k, s in MANIFEST.items() if s.editable_map()]
        return {"error": f"Unknown entity {entity!r}.", "editable_entities": editable}
    if spec.key == "property":
        routed = _route_structured_property_update(
            landlord, query=query, changes=changes, confirm=confirm
        )
        if routed is not None:
            return routed
    editable = spec.editable_map()
    if not editable:
        return {"error": f"{spec.key} has no fields RAMA can edit."}

    inst, err = _resolve_one(landlord, spec, query)
    if err:
        return err

    if spec.edit_guard is not None:
        ok, reason = spec.edit_guard(inst)
        if not ok:
            return {"error": reason}

    parsed: dict = {}
    for clause in _split_change_clauses(changes, editable):
        if "=" not in clause:
            return _clause_error(clause, editable)
        fname, raw = clause.split("=", 1)
        fname = fname.strip()
        fs = editable.get(fname)
        if fs is None:
            return {
                "error": f"Can't edit {fname!r} on {spec.key}. "
                f"Editable: {', '.join(editable)}."
            }
        # Any field constrained by model-level choices (province='BC'→'bc',
        # status, condition, …) is resolved to its code, tolerating case / display
        # labels — not just fields the manifest tagged 'enum'.
        try:
            has_choices = bool(inst._meta.get_field(fname).choices)
        except Exception:
            has_choices = False
        if fs.type == "enum" or has_choices:
            code, err = _resolve_choice(inst, fname, raw)
            if err:
                return {"error": err}
            parsed[fname] = code
            continue
        try:
            value = _coerce(fs, raw)
        except ValueError as exc:
            return {"error": str(exc)}
        # Normalise a postal code the way the model's save() would, so full_clean
        # validates the canonical form ('v8x 3g5' → 'V8X 3G5').
        if fname == "postal_code":
            from rentium.properties.models import normalise_postal_code

            value = normalise_postal_code(value)
        parsed[fname] = value
    if not parsed:
        return {"error": "No changes given — pass changes='field=value, …'."}

    label = str(getattr(inst, spec.resolve_label(), inst.pk))
    if not _confirmed(confirm):
        return _preview(
            "update",
            {"entity": spec.key, "target": label,
             "changes": {k: str(v) for k, v in parsed.items()}},
            "Applies these edits. confirm=yes.",
        )

    from django.core.exceptions import ValidationError

    # A lease edit goes through the lease service, not a generic setattr: it is
    # the one place that records an amendment against tenants who already
    # signed, and a second write path would silently skip that.
    if spec.key == "lease":
        from rentium.leases.services import update_lease_record

        try:
            result = update_lease_record(
                landlord=landlord, lease=inst, values=parsed
            )
        except ValidationError as exc:
            return {
                "error": "; ".join(
                    m
                    for msgs in getattr(exc, "message_dict", {"": exc.messages}).values()
                    for m in msgs
                )
            }
        out = {
            "updated": True,
            "entity": spec.key,
            "target": label,
            "changes": {k: str(v) for k, v in parsed.items()},
        }
        if result["amended_signers"]:
            out["amended_signers"] = result["amended_signers"]
            out["note"] = (
                f'{", ".join(result["amended_signers"])} had already signed — '
                f"this amends the agreement they signed. Not notified."
            )
        return out

    for k, v in parsed.items():
        setattr(inst, k, v)
    try:
        # Validate only the fields we touched (field validators + type checks),
        # not the whole model — avoids tripping unrelated required-field rules.
        inst.full_clean(
            exclude=[f.name for f in inst._meta.fields if f.name not in parsed]
        )
    except ValidationError as exc:
        return {"error": "; ".join(m for msgs in exc.message_dict.values() for m in msgs)}

    update_fields = list(parsed)
    if any(f.name == "updated_at" for f in inst._meta.fields):
        update_fields.append("updated_at")
    inst.save(update_fields=update_fields)
    return {
        "updated": True,
        "entity": spec.key,
        "target": label,
        "changes": {k: str(v) for k, v in parsed.items()},
    }
