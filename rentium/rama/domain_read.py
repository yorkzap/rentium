"""
Generic read primitive over the Domain Capability Manifest (rama/manifest.py).

`read(landlord, entity, filters, fields, limit, aggregate, group_by, order_by)`
answers a composed question against any manifest entity — no bespoke tool per
question. Safety is structural:

  - The queryset is ALWAYS scoped to the acting landlord first
    (`{scope_path: landlord}`), then narrowed by the entity's standing filters
    (`base_queryset`, e.g. "not voided"); user filters can only narrow further,
    never widen, so cross-tenant reads are impossible.
  - Only fields declared in the manifest can be filtered, returned, aggregated
    or grouped (default-deny) — an undeclared/sensitive field is unreachable.
  - The filter language is a tiny whitelist (field OP value) mapped to safe ORM
    lookups; there is no raw ORM/SQL surface exposed to the model.

WHY AGGREGATION LIVES HERE rather than in another list_* tool: 63 of RAMA's 177
tools were read-only, and most were this function plus a count. Each one then
competed for a place in the ~12 tool schemas a turn can carry, and the right one
routinely lost — "how many rents did we receive for aug or are due?" ranked
`charge_schedule` 20th, so the model never saw it and could not answer at all.
A question the generic primitive can answer needs no retrieval, because `read`
is always in context. Making this function more expressive is how RAMA gets
broader, rather than by adding a 178th tool and hoping it ranks.

Totals are computed over the WHOLE filtered queryset, never over `rows[:limit]`.
An aggregate over the first page, presented as a total, is the exact class of
confidently-wrong answer this module exists to prevent.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
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


# Filter names weak models reach for that aren't fields on the entity, mapped
# to the ORM lookup they actually mean. Whitelisted one by one — this is not a
# general "pass any ORM path" escape hatch.
#
# Every entry below is a real call from the audit log that failed with
# "Can't filter on X". Rejecting them taught the model nothing: `name_contains`
# is the obvious name for a contains filter, and asking for a lease by its
# property's name is a reasonable thing to want.
_RELATION_FILTERS = {
    ("lease", "property_name"): "property__name__icontains",
    ("lease", "property_query"): "property__name__icontains",
    ("lease", "tenant_name"): "lease_tenants__tenant__user__name__icontains",
    ("work_order", "property_name"): "property__name__icontains",
    ("inventory_item", "property_name"): "property__name__icontains",
}


def _alias_lookup(entity: str, fname: str, fmap: dict[str, FieldSpec]):
    """Resolve a non-field filter name to an ORM lookup, or None.

    Handles the declared relation filters above plus the generic
    `<field>_contains` form, which is what a model naturally writes when it
    wants a substring match and hasn't noticed the `~` operator.
    """
    direct = _RELATION_FILTERS.get((entity, fname))
    if direct:
        return direct
    if fname.endswith("_contains"):
        base = fname[: -len("_contains")]
        fs = fmap.get(base)
        if fs is not None and fs.filterable and fs.type in ("string", "enum"):
            return f"{base}__icontains"
    return None


# "field is empty" / "field is set" carry no operator token, so they have to be
# recognised before the _OPS scan or the clause looks malformed. Both spellings
# matter: this codebase declares text as `blank=True, default=""` far more often
# than nullable, so an isnull test on a CharField matches nothing and reads back
# as a confident "none".
_EMPTINESS = re.compile(
    r"^(?P<field>[\w.]+)\s+is\s+(?P<negate>not\s+)?(?:empty|blank|unset|null|set)$",
    re.IGNORECASE,
)


def _emptiness_clause(clause: str, fmap: dict[str, FieldSpec]):
    """Returns (handled, include, exclude, error)."""
    match = _EMPTINESS.match(clause.strip())
    if match is None:
        return False, None, None, None
    fname = match.group("field").strip()
    fs = fmap.get(fname)
    if fs is None or not fs.filterable:
        allowed = ", ".join(k for k, f in fmap.items() if f.nullable)
        return True, None, None, (
            f"Can't test {fname!r} for emptiness. Fields that can be empty: "
            f"{allowed or 'none on this entity'}."
        )
    if not fs.nullable and fs.type not in ("string", "enum"):
        return True, None, None, (
            f"{fname} is always set on this entity — filtering for empty would "
            f"return nothing. Drop the clause."
        )
    wants_set = clause.strip().lower().endswith("set") and not match.group("negate")
    path = fs.lookup_path
    blank_forms = {f"{path}__isnull": True}
    if fs.type in ("string", "enum"):
        # Cover both conventions in one clause; see the comment above.
        blank_forms = {f"{path}__in": [None, ""]}
    if wants_set:
        return True, {}, blank_forms, None
    return True, blank_forms, {}, None


def _parse_filters(filters: str, fmap: dict[str, FieldSpec], entity: str = ""):
    """Returns (include_kwargs, exclude_kwargs, error)."""
    include: dict = {}
    exclude: dict = {}
    if not filters.strip():
        return include, exclude, None
    for clause in filters.split(","):
        clause = clause.strip()
        if not clause:
            continue
        handled, inc, exc, err = _emptiness_clause(clause, fmap)
        if handled:
            if err:
                return None, None, err
            include.update(inc)
            exclude.update(exc)
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
            aliased = _alias_lookup(entity, fname, fmap)
            if aliased is not None:
                # Aliases are always substring/text lookups, so the raw value
                # goes through as-is.
                include[aliased] = raw.strip()
                continue
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


# --------------------------------------------------------------------------- #
# Aggregation, grouping, ordering, and date sugar
# --------------------------------------------------------------------------- #

_AGG_FUNCS = ("sum", "avg", "min", "max", "count")
#: Above this a "grouped table" is really just the rows again, and the model
#: presents it as a summary. Refuse rather than truncate: a truncated group
#: table looks exactly like a complete one.
_MAX_GROUPS = 50
_TRUNC = {"month": "TruncMonth", "year": "TruncYear"}


def _agg_key(func: str, fname: str) -> str:
    return "count" if func == "count" else f"{func}_{fname.replace('__', '_')}"


def _parse_aggregate(aggregate: str, spec: EntitySpec):
    """Returns (list of (key, django_aggregate), referenced_names, error)."""
    from django.db.models import Avg, Count, Max, Min, Sum

    builders = {"sum": Sum, "avg": Avg, "min": Min, "max": Max}
    specs: list[tuple[str, object]] = []
    referenced: set[str] = set()
    for raw in (aggregate or "").split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token == "count":
            specs.append(("count", Count("pk")))
            continue
        if ":" not in token:
            return None, None, (
                f"Bad aggregate {raw.strip()!r} — use 'count' or 'FUNC:field' "
                f"with FUNC in {', '.join(_AGG_FUNCS)}."
            )
        func, fname = (part.strip() for part in token.split(":", 1))
        if func not in builders:
            return None, None, (
                f"Unknown aggregate function {func!r}. "
                f"Use one of {', '.join(_AGG_FUNCS)}."
            )
        fs = spec.field_map().get(fname)
        if fs is None or not fs.aggregatable:
            allowed = ", ".join(spec.aggregatable_map()) or "none on this entity"
            return None, None, (
                f"Can't aggregate on {fname!r}. Aggregatable fields: {allowed}."
            )
        referenced.add(fs.name)
        specs.append((_agg_key(func, fs.name), builders[func](fs.lookup_path)))
    return specs, referenced, None


def _parse_group_by(group_by: str, spec: EntitySpec):
    """Returns (list of (alias, expression_or_path), referenced_names, error)."""
    from django.db.models.functions import TruncMonth, TruncYear

    truncs = {"month": TruncMonth, "year": TruncYear}
    keys: list[tuple[str, object]] = []
    referenced: set[str] = set()
    for raw in (group_by or "").split(","):
        token = raw.strip()
        if not token:
            continue
        if len(keys) >= 2:
            return None, None, (
                "Group by at most two keys — more than that is the rows again, "
                "not a summary."
            )
        if ":" in token:
            kind, fname = (part.strip() for part in token.split(":", 1))
            kind = kind.lower()
            if kind not in truncs:
                return None, None, (
                    f"Unknown grouping {kind!r}. Use "
                    f"{' or '.join(_TRUNC)}:<date field>."
                )
            fs = spec.field_map().get(fname)
            if fs is None or fs.type != "date":
                dates = ", ".join(
                    k for k, f in spec.field_map().items() if f.type == "date"
                )
                return None, None, (
                    f"Can't group by {kind} of {fname!r}. Date fields: "
                    f"{dates or 'none on this entity'}."
                )
            referenced.add(fs.name)
            keys.append((f"{kind}_{fs.name}", truncs[kind](fs.lookup_path)))
            continue
        fs = spec.field_map().get(token)
        if fs is None or not fs.groupable:
            allowed = ", ".join(spec.groupable_map()) or "none on this entity"
            return None, None, (
                f"Can't group by {token!r}. Groupable fields: {allowed}. "
                f"(Also available: month:<date field>, year:<date field>.)"
            )
        referenced.add(fs.name)
        keys.append((fs.name, fs.lookup_path))
    return keys, referenced, None


def _parse_order_by(order_by: str, spec: EntitySpec, agg_keys: set[str]):
    """Returns (orm_ordering, referenced_names, error)."""
    token = (order_by or "").strip()
    if not token:
        return None, set(), None
    descending = token.startswith("-")
    name = token.lstrip("-").strip()
    if name in agg_keys:  # order a grouped table by its own totals
        return ("-" if descending else "") + name, set(), None
    fs = spec.field_map().get(name)
    if fs is None or not fs.filterable:
        allowed = ", ".join(k for k, f in spec.field_map().items() if f.filterable)
        return None, None, f"Can't order by {name!r}. Orderable fields: {allowed}."
    return ("-" if descending else "") + fs.lookup_path, {fs.name}, None


def _parse_period(month: str, year: str, between: str, spec: EntitySpec):
    """Turn a time word into ordinary bounds on the entity's date field.

    Resolved server-side on purpose: asked for "this month", models reach for a
    year they were trained on. union.month_money carries an `as_of_year_hint`
    string for exactly that reason.
    """
    from .union import _month_bounds

    month = (month or "").strip()
    year = (year or "").strip()
    between = (between or "").strip()
    if not (month or year or between):
        return {}, "", None
    if not spec.date_field:
        return None, "", (
            f"{spec.key} has no date field, so month/year/between don't apply. "
            f"Filter on a date field directly."
        )
    path = spec.field_map()[spec.date_field].lookup_path
    today = date.today()

    if between:
        parts = [p.strip() for p in between.split("..")]
        if len(parts) != 2 or not all(parts):
            return None, "", "Use between=YYYY-MM-DD..YYYY-MM-DD."
        return (
            {f"{path}__gte": parts[0], f"{path}__lte": parts[1]},
            f"{spec.date_field} {parts[0]}..{parts[1]}",
            None,
        )

    if year:
        if not re.fullmatch(r"\d{4}", year):
            return None, "", f"year must be YYYY, got {year!r}."
        return (
            {f"{path}__gte": date(int(year), 1, 1),
             f"{path}__lt": date(int(year) + 1, 1, 1)},
            f"{spec.date_field} in {year}",
            None,
        )

    anchor = None
    if month.lower() in ("this", "current", "this month"):
        anchor = today
    elif month.lower() in ("last", "previous", "last month"):
        anchor = today.replace(day=1) - timedelta(days=1)
    else:
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return None, "", (
                f"month must be YYYY-MM (or 'this'/'last'), got {month!r}."
            )
        y, m = month.split("-")
        try:
            anchor = date(int(y), int(m), 1)
        except ValueError:
            return None, "", f"month must be YYYY-MM, got {month!r}."
    start, end = _month_bounds(anchor)
    return (
        {f"{path}__gte": start, f"{path}__lt": end},
        f"{spec.date_field} in {start:%Y-%m}",
        None,
    )


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


def _referenced_names(text: str, spec: EntitySpec) -> set[str]:
    """Declared field names that appear anywhere in an argument string."""
    tokens = set(re.findall(r"[\w.]+", text or ""))
    return {name for name in spec.field_map() if name in tokens}


def read(landlord, *, entity: str = "", filters: str = "", fields: str = "",
         limit: str = "20", aggregate: str = "", group_by: str = "",
         order_by: str = "", month: str = "", year: str = "",
         between: str = "") -> dict:
    spec = MANIFEST.get((entity or "").strip().lower())
    if spec is None:
        return {
            "error": f"Unknown entity {entity!r}.",
            "entities": list(MANIFEST.keys()),
        }
    from django.apps import apps

    Model = apps.get_model(*spec.model.split("."))
    fmap = spec.field_map()

    include, exclude, ferr = _parse_filters(filters or "", fmap, spec.key)
    if ferr:
        return {"error": ferr}

    period, period_note, perr = _parse_period(month, year, between, spec)
    if perr:
        return {"error": perr}
    include.update(period)

    agg_specs, agg_refs, aerr = _parse_aggregate(aggregate, spec)
    if aerr:
        return {"error": aerr}
    group_keys, group_refs, gerr = _parse_group_by(group_by, spec)
    if gerr:
        return {"error": gerr}
    ordering, order_refs, oerr = _parse_order_by(
        order_by, spec, {key for key, _ in agg_specs},
    )
    if oerr:
        return {"error": oerr}

    # Scope FIRST and unconditionally — never widened by user filters. The
    # entity's standing filters (e.g. "not voided") come next, for the same
    # reason and with the same guarantee: user filters can only narrow.
    qs = spec.base_queryset_for(Model.objects).filter(**{spec.scope_path: landlord})

    # Derived fields are queryset annotations, so they have to exist before
    # anything filters, groups or sums on them. Applied only when one is
    # actually named, so an ordinary read does not pay for the join.
    derived = {f.name for f in spec.fields if f.source}
    referenced = (
        _referenced_names(filters, spec)
        | _referenced_names(fields, spec)
        | agg_refs | group_refs | order_refs
    )
    if spec.annotate and (referenced & derived):
        qs = spec.annotate_for(qs)

    if include:
        qs = qs.filter(**include)
    if exclude:
        qs = qs.exclude(**exclude)
    qs = qs.order_by(spec.default_order)

    try:
        lim = max(1, min(int(limit or "20"), 100))
    except ValueError:
        lim = 20

    # `total` and every aggregate below are computed on `qs` — the whole
    # filtered set — and only `rows` is sliced. Keep it that way.
    total = qs.count()
    result = {
        "entity": spec.key,
        "total_matched": total,
    }
    applied = [part for part in [(filters or "").strip(), period_note] if part]
    if applied:
        # Echo what was actually applied. A number whose question the model has
        # to remember is a number it will attach to the wrong question.
        result["filters_applied"] = "; ".join(applied)
    if spec.scope_note:
        result["scope_note"] = spec.scope_note

    if agg_specs or group_keys:
        summary = _summarise(qs, spec, agg_specs, group_keys, ordering)
        if "error" in summary:
            return {**result, **summary}
        result.update(summary)
        # A grouped/aggregated answer is a summary; rows would just be the page
        # again under a total that covers everything. Ask for them separately.
        return result

    wanted = _select_fields(fields or "", spec)
    if ordering:
        qs = qs.order_by(ordering)
    rows = [{fs.name: _render(o, fs) for fs in wanted} for o in qs[:lim]]
    result.update(
        {
            "returned": len(rows),
            "fields": [fs.name for fs in wanted],
            "rows": rows,
            "truncated": total > len(rows),
        },
    )
    return result


def _agg_value(value, spec: EntitySpec, key: str):
    """Money and counts render the way the rest of `read` renders them.

    Decimals become strings — a float total of somebody's rent is a rounding
    error waiting to be quoted back at them.
    """
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _summarise(qs, spec: EntitySpec, agg_specs, group_keys, ordering) -> dict:
    """Totals, and optionally a breakdown by up to two keys."""
    from django.db.models import Count

    # A group_by with no aggregate still means "how many in each" — that is
    # the only thing it could mean.
    aggregates = dict(agg_specs) or {"count": Count("pk")}

    totals = qs.aggregate(**aggregates)
    out = {
        "totals": {
            key: _agg_value(value, spec, key) for key, value in totals.items()
        },
    }
    if not group_keys:
        return out

    aliases = [alias for alias, _ in group_keys]
    annotations = {
        alias: expression
        for alias, expression in group_keys
        if not isinstance(expression, str)
    }
    values = [
        alias if alias in annotations else expression
        for alias, expression in group_keys
    ]
    grouped = qs
    if annotations:
        grouped = grouped.annotate(**annotations)
    grouped = grouped.values(*values).annotate(**aggregates)
    grouped = grouped.order_by(ordering) if ordering else grouped.order_by(*values)

    # Refuse rather than truncate: a cut-off group table is indistinguishable
    # from a complete one, and reads as the whole picture.
    count = grouped.count()
    if count > _MAX_GROUPS:
        return {
            "error": (
                f"That grouping produces {count} groups, which is a listing "
                f"rather than a summary. Narrow the filters, or group by "
                f"something with fewer values."
            ),
            "distinct_groups": count,
        }

    rows = []
    for row in grouped:
        rendered = {}
        for alias, expression in group_keys:
            key = alias if alias in annotations else expression
            rendered[alias] = _agg_value(row.get(key), spec, alias)
        for key in aggregates:
            rendered[key] = _agg_value(row.get(key), spec, key)
        rows.append(rendered)
    out["group_by"] = aliases
    out["groups"] = rows
    return out


def link(landlord, *, entity: str = "", query: str = "") -> dict:
    """Resolve ONE instance of a catalogued entity within the landlord's scope and
    return a clickable in-app deep link (+ what can be downloaded there). Generic
    Phase-2 replacement for per-entity open_* tools — driven by the manifest's
    LinkSpec. Same scope guarantee as read: only the landlord's own rows resolve."""
    from django.db.models import Q
    from .links import dashboard_collection, url_for_path

    collection = dashboard_collection(entity)
    if collection is not None:
        label, path = collection
        return {
            "entity": (entity or "").strip().casefold(),
            "label": label,
            "link": url_for_path(path),
            "collection": True,
            "note": f"Open the {label} dashboard page.",
        }

    spec = MANIFEST.get((entity or "").strip().lower())
    if spec is None:
        linkable = [k for k, s in MANIFEST.items() if s.links]
        from .links import DASHBOARD_COLLECTIONS

        return {
            "error": f"Unknown entity {entity!r}.",
            "linkable": linkable + list(DASHBOARD_COLLECTIONS),
        }
    if spec.links is None:
        linkable = [k for k, s in MANIFEST.items() if s.links]
        return {"error": f"{spec.key} has no page to link to.", "linkable": linkable}

    from django.apps import apps

    Model = apps.get_model(*spec.model.split("."))
    ls = spec.links
    lookup = spec.resolve_lookup()
    label_field = spec.resolve_label()
    qs = Model.objects.filter(**{spec.scope_path: landlord})
    q = (query or "").strip()
    if q:
        cond = Q()
        for f in lookup:
            cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond)
    qs = qs.order_by(spec.default_order)

    matches = list(qs[:6])
    if not matches:
        return {"error": f"No {spec.key} matching {query!r} in your portfolio."}
    if len(matches) > 1:
        return {
            "disambiguate": [
                {"label": str(getattr(m, label_field, m.pk)), "hint": str(m.pk)}
                for m in matches
            ],
            "note": f"Several {spec.key}s match {query!r} — which one?",
        }

    m = matches[0]
    out = {
        "entity": spec.key,
        "label": str(getattr(m, label_field, m.pk)),
        "link": url_for_path(ls.page.format(id=m.pk)),
    }
    if ls.downloads:
        out["available_there"] = list(ls.downloads)
        out["note"] = (
            f"Open the link to view it; {', '.join(ls.downloads)} "
            "can be downloaded on that page."
        )
    return out
