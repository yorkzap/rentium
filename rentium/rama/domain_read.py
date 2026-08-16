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

from .manifest import (
    MANIFEST,
    EntitySpec,
    FieldSpec,
    relation_label_path,
    relation_paths,
    resolve_path,
    resolve_relation,
)

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


# Names that mean a relation path but aren't spelled like one. What used to be
# a hand-maintained table of five is now two shapes, because manifest.resolve_path
# handles `lease__start_date` generically — every entry that table held was a
# real call from the audit log that had failed, i.e. a landlord waiting for
# somebody to notice.
#
# `<relation>_name` / `<relation>_query` survive as sugar: "give me the leases
# for Maple Street" is a substring match on the related entity's human label,
# not on a field the model can name.
_RELATION_SUGAR = re.compile(r"^(?P<rel>\w+?)_(?:name|query)$")


def _alias_lookup(entity_spec, fname: str, fmap: dict[str, FieldSpec]):
    """Resolve a non-field filter name to an ORM lookup, or None.

    Two generic forms: `<field>_contains`, which is what a model writes when it
    wants a substring match and hasn't noticed the `~` operator; and
    `<relation>_name`, a substring match against the related entity's label.
    """
    if fname.endswith("_contains"):
        base = fname[: -len("_contains")]
        fs = fmap.get(base)
        if fs is not None and fs.filterable and fs.type in ("string", "enum"):
            return f"{base}__icontains"
        # `lease_number_contains` style across a relation.
        path, spec_, _err = resolve_path(entity_spec, base)
        if path and spec_ is not None and spec_.type in ("string", "enum"):
            return f"{path}__icontains"

    match = _RELATION_SUGAR.match(fname)
    if match:
        label_path = relation_label_path(entity_spec, match.group("rel"))
        if label_path:
            return f"{label_path}__icontains"
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


def _filterable_names(spec: EntitySpec) -> str:
    """What to offer when a filter name is rejected.

    Includes the relation prefixes, because the model cannot guess that
    `lease__start_date` is legal from a list that only shows local fields —
    that is precisely how it ended up reading the ledger once per lease.
    """
    local = [k for k, f in spec.field_map().items() if f.filterable]
    rels = relation_paths(spec)
    if rels:
        local.append(
            "and these relations — each usable as `rel__field=…` or named "
            "directly as `rel=<id or name>`: " + ", ".join(rels),
        )
    return ", ".join(local)


_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$",
                   re.I)


def _identity_paths(prefix: str, target: EntitySpec) -> list[str]:
    """Declared text columns on the far side that identify one row to a human.

    `resolve_lookup()` is already exactly that list — it is what `link` searches
    when the landlord names a thing — so a relation becomes filterable by name
    without anybody declaring anything twice.
    """
    fmap = target.field_map()
    return [
        f"{prefix}__{fname}"
        for fname in target.resolve_lookup()
        if (fs := fmap.get(fname)) is not None
        and fs.filterable
        and not fs.source
        and fs.type in ("string", "enum")
    ]


def _pk_shaped(target: EntitySpec, value: str) -> bool:
    """Is this value shaped like the far side's primary key?

    Checked against the actual pk field rather than assumed, so a numeric value
    against a UUID key falls through to a name match instead of reaching the
    database and coming back as a ValidationError about a malformed uuid — a
    true statement about the wrong reading of the question.
    """
    from django.apps import apps  # noqa: PLC0415

    pk = apps.get_model(*target.model.split("."))._meta.pk.get_internal_type()
    if pk == "UUIDField":
        return bool(_UUID.match(value))
    if pk in ("AutoField", "BigAutoField", "SmallAutoField", "IntegerField",
              "BigIntegerField"):
        return value.isdigit()
    return False


def _relation_clause(spec: EntitySpec, fname: str, suffix: str, raw: str):
    """`lease=RMT652523-C281` — filter on the RELATION, not a field of it.

    Returns (handled, Q or None, error).

    The model reaches for this constantly and it used to be refused outright:
    asked whether Aishwarya and Naveen got a discount, it wrote
    `filters='lease=<uuid>'` on rent_adjustment, was told no such field existed,
    and spent a round rewriting the same intent as a path to a named column. It
    recovered, but a round is a third of the turn's budget, and the request was
    never ambiguous — a relation has exactly one primary key and one human name.

    An identifier goes to the primary key; anything else is matched against the
    same columns `link` searches, so "the Maple Street lease" works as written.
    Scope is untouched: the queryset is already filtered to the landlord, so
    naming another landlord's id here matches nothing of theirs.
    """
    from django.db.models import Q

    resolved = resolve_relation(spec, fname)
    if resolved is None:
        return False, None, None
    prefix, target = resolved
    value = raw.strip()
    if not value:
        return True, None, f"{fname}= needs a value (an id, or part of a name)."
    if suffix not in ("=", "!=", "__icontains"):
        return True, None, (
            f"{fname} is a relation, so only = , != and ~ apply to it. For "
            f"ranges and comparisons filter on one of its fields: "
            f"{fname}__<field>."
        )

    if _pk_shaped(target, value):
        return True, Q(**{f"{prefix}__pk": value}), None

    paths = _identity_paths(prefix, target) or [
        p for p in [relation_label_path(spec, fname)] if p
    ]
    if not paths:
        return True, None, (
            f"{fname} can't be matched by name — filter on one of its fields "
            f"instead: {fname}__<field>."
        )
    cond = Q()
    for path in paths:
        cond |= Q(**{f"{path}__icontains": value})
    return True, cond, None


_PERIOD_IN_FILTER = re.compile(
    r"^\s*(month|year)(?::[a-z_]+)?\s*=\s*(.+?)\s*$", re.I,
)


def _lift_period_out_of_filters(filters: str, month: str, year: str, fmap: dict):
    """Accept `month=2026-08` written as a FILTER instead of an argument.

    `month:due_date` is real syntax — for group_by — and the model reasonably
    assumed it worked in filters too, spending two of its eight rounds on
    "Can't filter on 'month'" and "Can't filter on 'month:due_date'". The
    intent is unambiguous and the destination already exists, so honour it
    rather than teach the distinction. An explicit month= argument still wins.
    """
    kept = []
    for clause in (filters or "").split(","):
        match = _PERIOD_IN_FILTER.match(clause)
        if match is None:
            if clause.strip():
                kept.append(clause.strip())
            continue
        which, value = match.group(1).casefold(), match.group(2)
        if which in fmap:
            # An entity that really has a column called `month` means it; the
            # convenience must never shadow a declared field.
            kept.append(clause.strip())
            continue
        if which == "month" and not month.strip():
            month = value
        elif which == "year" and not year.strip():
            year = value
    return ", ".join(kept), month, year


def _parse_filters(filters: str, fmap: dict[str, FieldSpec], spec: EntitySpec = None):
    """Returns (include_kwargs, exclude_kwargs, q_objects, error).

    `q_objects` carries the clauses that are an OR internally — matching a
    relation by name searches several columns at once — and is ANDed with the
    rest, so it narrows like every other clause.
    """
    include: dict = {}
    exclude: dict = {}
    conditions: list = []
    if not filters.strip():
        return include, exclude, conditions, None
    for clause in filters.split(","):
        clause = clause.strip()
        if not clause:
            continue
        handled, inc, exc, err = _emptiness_clause(clause, fmap)
        if handled:
            if err:
                return None, None, None, err
            include.update(inc)
            exclude.update(exc)
            continue
        op_tok = suffix = None
        for tok, suf in _OPS:
            if tok in clause:
                op_tok, suffix = tok, suf
                break
        if op_tok is None:
            return None, None, None, (
                f"Bad filter {clause!r} — use field=value, >, <, >=, <=, "
                f"~ (contains), != , 'field is empty', or a range "
                f"field=2026-08-01..2026-08-31 ."
            )
        fname, raw = clause.split(op_tok, 1)
        fname = fname.strip()

        # Local field, then the relation itself (lease=…), then a relation path
        # (lease__start_date), then sugar. A declared field of the same name
        # always wins — the manifest is the authority on what a word means here.
        fs = fmap.get(fname)
        path = fs.lookup_path if (fs is not None and fs.filterable) else None
        if path is None:
            handled, cond, err = _relation_clause(spec, fname, suffix, raw)
            if handled:
                if err:
                    return None, None, None, err
                conditions.append(~cond if suffix == "!=" else cond)
                continue
            path, fs, err = resolve_path(spec, fname)
            if err:
                return None, None, None, err
        if path is None or fs is None or not fs.filterable:
            aliased = _alias_lookup(spec, fname, fmap)
            if aliased is not None:
                # Aliases are always substring/text lookups, so the raw value
                # goes through as-is.
                include[aliased] = raw.strip()
                continue
            return None, None, None, (
                f"Can't filter on {fname!r}. Filterable fields: "
                f"{_filterable_names(spec)}."
            )

        # A range on the field itself — `start_date=2026-08-01..2026-08-31`.
        # This is what the model reaches for, on the field it actually cares
        # about; `between=` only ever applied to the entity's default date.
        if suffix == "=" and ".." in raw:
            low, _, high = raw.partition("..")
            try:
                lo, hi = _coerce(fs, low), _coerce(fs, high)
            except ValueError as exc:
                return None, None, None, str(exc)
            include[f"{path}__gte"] = lo
            include[f"{path}__lte"] = hi
            continue

        try:
            value = _coerce(fs, raw)
        except ValueError as exc:
            return None, None, None, str(exc)
        if suffix == "=":  # equality: iexact for text/enum, exact otherwise
            key = f"{path}__iexact" if fs.type in ("string", "enum") else path
            include[key] = value
        elif suffix == "!=":
            exclude[path] = value
        else:
            include[f"{path}{suffix}"] = value
    return include, exclude, conditions, None


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
        path = fs.lookup_path if fs is not None else None
        if fs is None:
            path, fs, err = resolve_path(spec, fname)
            if err:
                return None, None, err
        if fs is None or not fs.aggregatable:
            allowed = ", ".join(spec.aggregatable_map()) or "none on this entity"
            return None, None, (
                f"Can't aggregate on {fname!r}. Aggregatable fields: {allowed}."
            )
        referenced.add(fs.name)
        specs.append((_agg_key(func, fname), builders[func](path)))
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
            path = fs.lookup_path if fs is not None else None
            if fs is None:
                path, fs, err = resolve_path(spec, fname)
                if err:
                    return None, None, err
            if fs is None or fs.type != "date":
                dates = ", ".join(
                    k for k, f in spec.field_map().items() if f.type == "date"
                )
                return None, None, (
                    f"Can't group by {kind} of {fname!r}. Date fields: "
                    f"{dates or 'none on this entity'}."
                )
            referenced.add(fs.name)
            keys.append((f"{kind}_{fname}", truncs[kind](path)))
            continue
        # `group_by="lease"` — group by the RELATION, keyed on the related
        # entity's human label rather than its UUID. Without this the only way
        # to ask "deposits per lease" was one query per lease, which is how a
        # turn burned its whole time budget and answered nothing.
        label_path = relation_label_path(spec, token)
        if label_path is not None:
            referenced.add(token)
            keys.append((token, label_path))
            continue

        fs = spec.field_map().get(token)
        path = fs.lookup_path if fs is not None else None
        if fs is None:
            path, fs, err = resolve_path(spec, token)
            if err:
                return None, None, err
        if fs is None or not fs.groupable:
            allowed = ", ".join(spec.groupable_map()) or "none on this entity"
            rels = ", ".join(relation_paths(spec))
            return None, None, (
                f"Can't group by {token!r}. Groupable fields: {allowed}. "
                f"Relations (grouped by their name): {rels or 'none'}. "
                f"(Also available: month:<date field>, year:<date field>.)"
            )
        referenced.add(fs.name)
        keys.append((token, path))
    return keys, referenced, None


def _parse_order_by(order_by: str, spec: EntitySpec, agg_keys: set[str]):
    """Returns (orm_ordering, referenced_names, error)."""
    token = (order_by or "").strip()
    if not token:
        return None, set(), None
    descending = token.startswith("-")
    name = token.lstrip("-").strip()
    if ":" in name:
        # The model orders by the aggregate the way it SPELLED the aggregate —
        # `order_by='-sum:amount'` against `aggregate='sum:amount'`. Refusing
        # that cost a round on a query that was otherwise exactly right.
        func, _, fname = name.partition(":")
        name = _agg_key(func.strip().lower(), fname.strip())
    if name in agg_keys:  # order a grouped table by its own totals
        return ("-" if descending else "") + name, set(), None
    fs = spec.field_map().get(name)
    path = fs.lookup_path if fs is not None else None
    if fs is None:
        path, fs, err = resolve_path(spec, name)
        if err:
            return None, None, err
    if fs is None or not fs.filterable:
        return None, None, (
            f"Can't order by {name!r}. Orderable fields: {_filterable_names(spec)}."
        )
    return ("-" if descending else "") + path, {fs.name}, None


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


#: Every row has one, on every entity, and the model asks for it constantly —
#: it is how it refers to a row in the next call. Not a manifest field because
#: no entity should have to declare it.
_ID_ALIASES = ("id", "pk")


class _Column:
    """A column to return: what was asked for, and how to get it off a row.

    `owner` is the attribute chain to the object that HOLDS the field, so a
    display method (`get_adjustment_type_display`) is called on the right object
    rather than on the row two relations away.
    """

    __slots__ = ("alias", "owner", "fs")

    def __init__(self, alias: str, owner: tuple, fs: FieldSpec | None):
        self.alias, self.owner, self.fs = alias, owner, fs


def _resolve_column(spec: EntitySpec, name: str):
    """(column, error) for one requested field name."""
    fs = spec.field_map().get(name)
    if fs is not None:
        return _Column(name, (), fs), None
    if name in _ID_ALIASES:
        return _Column(name, (), None), None

    # The relation itself — `lease_tenant__lease` means "which lease", and the
    # answer a human wants is its number, not its uuid.
    label_path = relation_label_path(spec, name)
    if label_path is not None:
        parts = label_path.split("__")
        target = resolve_relation(spec, name)[1]
        return _Column(name, tuple(parts[:-1]), target.field_map()[parts[-1]]), None

    path, fs, err = resolve_path(spec, name)
    if err:
        return None, err
    if path is None or fs is None:
        return None, f"not a field or a relation on {spec.key}"
    return _Column(name, tuple(path.split("__")[:-1]), fs), None


def _readable_names(spec: EntitySpec) -> str:
    rels = ", ".join(relation_paths(spec)) or "none"
    return (
        f"{', '.join(spec.field_map())}, id — and through relations "
        f"(as `rel__field`, or `rel` alone for its name): {rels}"
    )


def _select_fields(fields: str, spec: EntitySpec):
    """(columns, rejected, error). Loud about what it could not give you, and
    still gives you the rest.

    Both halves were learned the hard way. Silently dropping unknown names is
    what this used to do, and it cost a whole turn: asked which lease a discount
    belonged to, the model requested `fields='lease_tenant__lease, …'`, got rows
    without those keys and no word about why, and re-asked six times before the
    budget ran out and it told the landlord it could not tell.

    Failing the whole call for one bad name in a list of ten is no better — the
    model re-sends nearly the same list, because the reply says which name was
    wrong but the fix is one edit away from a list it has already typed out. So:
    return every column that resolves, and name the ones that didn't next to the
    rows, where they cannot be missed.
    """
    names = [n.strip() for n in (fields or "").split(",") if n.strip()]
    if not names:
        return [_Column(f.name, (), f) for f in spec.fields], {}, None
    columns, rejected = [], {}
    for name in names:
        column, err = _resolve_column(spec, name)
        if err:
            rejected[name] = err
            continue
        columns.append(column)
    if not columns:
        named = "; ".join(f"{name}: {why}" for name, why in rejected.items())
        return None, rejected, (
            f"None of those fields exist. {named}. Readable: "
            f"{_readable_names(spec)}."
        )
    return columns, rejected, None


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


def _render_column(obj, column: _Column):
    owner = obj
    for attr in column.owner:
        owner = getattr(owner, attr, None)
        if owner is None:
            return None
    if column.fs is None:  # id / pk
        return str(getattr(owner, "pk", "")) or None
    return _render(owner, column.fs)


def _referenced_names(text: str, spec: EntitySpec) -> set[str]:
    """Declared field names that appear anywhere in an argument string."""
    tokens = set(re.findall(r"[\w.]+", text or ""))
    return {name for name in spec.field_map() if name in tokens}


def read(landlord, **kwargs) -> dict:
    """Guarded entry point: a raw exception must never reach the model.

    Asked for a date range it spelled `start_date=2026-08-01..2026-08-31`, the
    model got back a Django ValidationError verbatim. That taught it nothing —
    every other error in this module names what IS allowed, which is how it
    self-corrects mid-turn (see the _alias_lookup notes). It retried, learned
    nothing again, and burned the turn's time budget.

    So the last line of defence is structural: whatever goes wrong below, the
    model receives a sentence describing the fix.
    """
    from django.core.exceptions import FieldError, ValidationError

    try:
        return _read(landlord, **kwargs)
    except ValidationError as exc:
        return {
            "error": (
                f"A filter value wasn't the right shape: "
                f"{'; '.join(exc.messages)} "
                f"Dates are YYYY-MM-DD (a range is start..end), money is a "
                f"plain number, booleans are true/false."
            ),
        }
    except (FieldError, ValueError, TypeError) as exc:
        return {
            "error": (
                f"That query didn't work: {exc}. Call data_catalogue for this "
                f"entity's fields, or read it directly instead of through a "
                f"relation."
            ),
        }


def _read(landlord, *, entity: str = "", filters: str = "", fields: str = "",
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

    filters, month, year = _lift_period_out_of_filters(
        filters or "", month, year, fmap,
    )

    include, exclude, conditions, ferr = _parse_filters(filters, fmap, spec)
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
        order_by, spec,
        # Aggregate keys and the annotated group keys (month:/year:) are real
        # names in the grouped queryset. A group key that is just a column path
        # is not an alias, so it stays out — ordering by it would silently mean
        # something else.
        {key for key, _ in agg_specs}
        | {alias for alias, expr in group_keys if not isinstance(expr, str)},
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
    for condition in conditions:
        qs = qs.filter(condition)
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
        if (fields or "").strip():
            # Say that `fields` was ignored. Dropping it in silence is the same
            # defect as dropping an unknown field name in silence, and it cost
            # the same turn: asked for adjustments grouped by lease WITH the
            # row detail, the model got groups, saw no rows, and re-sent the
            # identical call four more times.
            result["fields_ignored"] = (
                "A grouped/aggregated result is a summary, so `fields` does not "
                "apply — `groups` carries the group key and the aggregates, "
                "nothing else. For the individual rows, call read again with "
                "the same filters, your `fields`, and NO group_by/aggregate."
            )
        # A grouped/aggregated answer is a summary; rows would just be the page
        # again under a total that covers everything. Ask for them separately.
        return result

    wanted, rejected, serr = _select_fields(fields or "", spec)
    if serr:
        return {**result, "error": serr}
    if rejected:
        # One shared list, not one per rejected name — five bad names in a list
        # of twenty must not return the catalogue five times.
        result["fields_unavailable"] = rejected
        result["fields_available"] = _readable_names(spec)
    if ordering:
        qs = qs.order_by(ordering)
    # One join per traversed column, instead of one query per row.
    joins = {"__".join(c.owner) for c in wanted if c.owner}
    if joins:
        qs = qs.select_related(*joins)
    rows = [{c.alias: _render_column(o, c) for c in wanted} for o in qs[:lim]]
    result.update(
        {
            "returned": len(rows),
            "fields": [c.alias for c in wanted],
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
