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

from .domain_read import _coerce  # shared type coercion
from .manifest import MANIFEST, EntitySpec


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

    if spec.links is None:
        return None, {"error": f"{spec.key} can't be targeted by name yet."}
    Model = apps.get_model(*spec.model.split("."))
    qs = Model.objects.filter(**{spec.scope_path: landlord})
    q = (query or "").strip()
    if q:
        cond = Q()
        for f in spec.links.lookup:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    matches = list(qs.order_by(spec.default_order)[:6])
    if not matches:
        return None, {"error": f"No {spec.key} matching {query!r} in your portfolio."}
    if len(matches) > 1:
        return None, {
            "error": f"Several {spec.key}s match {query!r} — be more specific.",
            "options": [str(getattr(m, spec.links.label_field, m.pk)) for m in matches],
        }
    return matches[0], None


def update(landlord, *, entity: str = "", query: str = "", changes: str = "",
           confirm: str = "") -> dict:
    spec = MANIFEST.get((entity or "").strip().lower())
    if spec is None:
        editable = [k for k, s in MANIFEST.items() if s.editable_map()]
        return {"error": f"Unknown entity {entity!r}.", "editable_entities": editable}
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
    for clause in (changes or "").split(","):
        clause = clause.strip()
        if not clause:
            continue
        if "=" not in clause:
            return {"error": f"Bad change {clause!r} — use field=value."}
        fname, raw = clause.split("=", 1)
        fname = fname.strip()
        fs = editable.get(fname)
        if fs is None:
            return {
                "error": f"Can't edit {fname!r} on {spec.key}. "
                f"Editable: {', '.join(editable)}."
            }
        try:
            parsed[fname] = _coerce(fs, raw)
        except ValueError as exc:
            return {"error": str(exc)}
    if not parsed:
        return {"error": "No changes given — pass changes='field=value, …'."}

    label = str(getattr(inst, spec.links.label_field, inst.pk))
    if not _confirmed(confirm):
        return _preview(
            "update",
            {"entity": spec.key, "target": label,
             "changes": {k: str(v) for k, v in parsed.items()}},
            "Applies these edits. confirm=yes.",
        )

    from django.core.exceptions import ValidationError

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
