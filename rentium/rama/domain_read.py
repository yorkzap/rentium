"""
Generic read primitive over the Domain Capability Manifest (rama/manifest.py).

`read(landlord, entity, filters, fields, limit)` answers a composed question
against any manifest entity — no bespoke tool per question. Safety is structural:

  - The queryset is ALWAYS scoped to the acting landlord first
    (`{scope_path: landlord}`); user filters can only narrow it, never widen it,
    so cross-tenant reads are impossible.
  - Only fields declared in the manifest can be filtered or returned
    (default-deny) — an undeclared/sensitive field is unreachable.
  - The filter language is a tiny whitelist (field OP value) mapped to safe ORM
    lookups; there is no raw ORM/SQL surface exposed to the model.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .manifest import MANIFEST, EntitySpec, FieldSpec

# Longest operators first so ">=" isn't mis-split as ">".
_OPS = [
    (">=", "__gte"), ("<=", "__lte"), ("!=", "!="), ("~", "__icontains"),
    (">", "__gt"), ("<", "__lt"), ("=", "="),
]
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _coerce(fs: FieldSpec, raw: str):
    v = raw.strip()
    if fs.type in ("number", "money"):
        try:
            return Decimal(v)
        except (InvalidOperation, ValueError):
            raise ValueError(f"{fs.name} needs a number, got {raw!r}.")
    if fs.type == "bool":
        low = v.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"{fs.name} needs true/false, got {raw!r}.")
    return v  # string / enum / date (Django parses ISO dates for gte/lte/exact)


def _parse_filters(filters: str, fmap: dict[str, FieldSpec]):
    """Returns (include_kwargs, exclude_kwargs, error)."""
    include: dict = {}
    exclude: dict = {}
    if not filters.strip():
        return include, exclude, None
    for clause in filters.split(","):
        clause = clause.strip()
        if not clause:
            continue
        op_tok = suffix = None
        for tok, suf in _OPS:
            if tok in clause:
                op_tok, suffix = tok, suf
                break
        if op_tok is None:
            return None, None, f"Bad filter {clause!r} — use field=value, >, <, >=, <=, ~ (contains), or != ."
        fname, raw = clause.split(op_tok, 1)
        fname = fname.strip()
        fs = fmap.get(fname)
        if fs is None or not fs.filterable:
            allowed = ", ".join(k for k, f in fmap.items() if f.filterable)
            return None, None, f"Can't filter on {fname!r}. Filterable fields: {allowed}."
        try:
            value = _coerce(fs, raw)
        except ValueError as exc:
            return None, None, str(exc)
        if suffix == "=":  # equality: iexact for text/enum, exact otherwise
            key = f"{fname}__iexact" if fs.type in ("string", "enum") else fname
            include[key] = value
        elif suffix == "!=":
            exclude[fname] = value
        else:
            include[f"{fname}{suffix}"] = value
    return include, exclude, None


def _select_fields(fields: str, spec: EntitySpec) -> list[FieldSpec]:
    fmap = spec.field_map()
    if fields.strip():
        picked = [fmap[n.strip()] for n in fields.split(",") if n.strip() in fmap]
        if picked:
            return picked
    return list(spec.fields)


def _render(obj, fs: FieldSpec):
    if fs.display and hasattr(obj, fs.display):
        return getattr(obj, fs.display)()
    v = getattr(obj, fs.name, None)
    if v is None:
        return None
    if fs.type in ("money", "number"):
        return str(v)
    if fs.type == "date" and hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def read(landlord, *, entity: str = "", filters: str = "", fields: str = "",
         limit: str = "20") -> dict:
    spec = MANIFEST.get((entity or "").strip().lower())
    if spec is None:
        return {
            "error": f"Unknown entity {entity!r}.",
            "entities": list(MANIFEST.keys()),
        }
    from django.apps import apps

    Model = apps.get_model(*spec.model.split("."))
    fmap = spec.field_map()

    include, exclude, ferr = _parse_filters(filters or "", fmap)
    if ferr:
        return {"error": ferr}

    # Scope FIRST and unconditionally — never widened by user filters.
    qs = Model.objects.filter(**{spec.scope_path: landlord})
    if include:
        qs = qs.filter(**include)
    if exclude:
        qs = qs.exclude(**exclude)
    qs = qs.order_by(spec.default_order)

    try:
        lim = max(1, min(int(limit or "20"), 100))
    except ValueError:
        lim = 20

    wanted = _select_fields(fields or "", spec)
    total = qs.count()
    rows = [{fs.name: _render(o, fs) for fs in wanted} for o in qs[:lim]]
    return {
        "entity": spec.key,
        "returned": len(rows),
        "total_matched": total,
        "fields": [fs.name for fs in wanted],
        "rows": rows,
    }


def link(landlord, *, entity: str = "", query: str = "") -> dict:
    """Resolve ONE instance of a catalogued entity within the landlord's scope and
    return a clickable in-app deep link (+ what can be downloaded there). Generic
    Phase-2 replacement for per-entity open_* tools — driven by the manifest's
    LinkSpec. Same scope guarantee as read: only the landlord's own rows resolve."""
    from django.conf import settings
    from django.db.models import Q

    spec = MANIFEST.get((entity or "").strip().lower())
    if spec is None:
        linkable = [k for k, s in MANIFEST.items() if s.links]
        return {"error": f"Unknown entity {entity!r}.", "linkable": linkable}
    if spec.links is None:
        linkable = [k for k, s in MANIFEST.items() if s.links]
        return {"error": f"{spec.key} has no page to link to.", "linkable": linkable}

    from django.apps import apps

    Model = apps.get_model(*spec.model.split("."))
    ls = spec.links
    qs = Model.objects.filter(**{spec.scope_path: landlord})
    q = (query or "").strip()
    if q:
        cond = Q()
        for f in ls.lookup:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    qs = qs.order_by(spec.default_order)

    matches = list(qs[:6])
    if not matches:
        return {"error": f"No {spec.key} matching {query!r} in your portfolio."}
    if len(matches) > 1:
        return {
            "disambiguate": [
                {"label": str(getattr(m, ls.label_field, m.pk)), "hint": str(m.pk)}
                for m in matches
            ],
            "note": f"Several {spec.key}s match {query!r} — which one?",
        }

    m = matches[0]
    base = settings.FRONTEND_URL.rstrip("/")
    out = {
        "entity": spec.key,
        "label": str(getattr(m, ls.label_field, m.pk)),
        "link": base + ls.page.format(id=m.pk),
    }
    if ls.downloads:
        out["available_there"] = list(ls.downloads)
        out["note"] = (
            f"Open the link to view it; {', '.join(ls.downloads)} "
            "can be downloaded on that page."
        )
    return out
