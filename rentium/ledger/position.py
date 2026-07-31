"""
One financial position, computed once, for every reader.

WHY THIS EXISTS
---------------
"Is the $100 Room C deposit payment in the ledger?" was answered No, then Yes,
in consecutive turns — and the No came back in a payload that ALSO said
`paid_to_date: "100.00"` next to `deposit_held: "0.00"`.

Nothing was stale. Every reader already used `with_settlement()` on live rows.
The contradiction was that "what is owed" and "what deposit is held" had been
hand-rolled six times over, once per caller, each with its own scope predicate:

  * services.deposits_held      — portfolio, no tenant filter        → 100.00 ✅
  * services.deposit_position   — filtered by lease                  → 100.00 ✅
  * services.tenant_statement   — filtered by `tenant=tenant`        →   0.00 ❌

The third is wrong because a joint-lease deposit charge carries `tenant=None`
(billing.py: `_one_time(None, DEPOSIT_CHARGE, ...)` when `joint`), so the
PAYMENT settling it inherits `tenant=None` too. `tenant_statement` built the
correct joint-aware predicate for its `charges` list and then ignored it thirty
lines below for the deposit aggregate.

That is not a bug to fix once. It is a bug that regenerates every time somebody
needs a number in a new scope. So the predicate is written **once**, here, and
every reader projects from it.

THE TWO RULES THAT MAKE IT CORRECT BY CONSTRUCTION
--------------------------------------------------
1. **A charge is in scope** iff it matches the scope predicate — and for a
   tenant, that means their own charges *plus* the household charges of every
   lease they are on, because on a joint lease each tenant is liable for the
   whole household charge, not a share of it.

2. **A settlement is in scope iff the charge it settles is in scope.** Never
   scope a PAYMENT by its own `tenant`/`lease` columns. This is the rule the old
   code broke, and following it means the joint case needs no special handling
   anywhere: money paid against a household deposit is found by looking at the
   deposit, not at who happened to be recorded as paying.

NAMING
------
Three different quantities were all called `outstanding`. Here they have names
that say which one they are, and the ambiguous word is never used alone:

  outstanding_now   — due on or before as_of, still unpaid ("money owed today")
  outstanding_all   — amount − settled, whatever the due date ("balance on the
                      books", including charges not due yet)
  income_*          — the same, with deposits excluded (they are a refundable
                      liability, never income)
  deposits_held     — money actually received against deposit charges, less
                      deposits returned. A liability, not a receivable.

Public API keys (`outstanding_total` on /api/ledger/summary/) are deliberately
NOT renamed here — the frontend reads them. They keep their names and get their
values from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from decimal import Decimal
from typing import Any

from django.db.models import Count
from django.db.models import Q
from django.db.models import Sum

from .models import EntryType
from .models import LedgerEntry

ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return (value if value is not None else ZERO).quantize(Decimal("0.01"))


def _prefix_q(q: Q, prefix: str) -> Q:
    """Re-root a Q onto a related path: Q(lease_id__in=…) → Q(settles__lease_id__in=…).

    This is what lets rule 2 above be a single line at each use site: take the
    scope predicate written for charges and apply it to the charge a settlement
    points at, with no second predicate to keep in sync.
    """
    shifted = Q()
    shifted.connector = q.connector
    shifted.negated = q.negated
    shifted.children = [
        _prefix_q(child, prefix)
        if isinstance(child, Q)
        else (f"{prefix}__{child[0]}", child[1])
        for child in q.children
    ]
    return shifted


@dataclass(frozen=True)
class Scope:
    """Which slice of the ledger a position covers.

    Construct with the classmethods, never the raw fields — the constructors are
    where the joint-lease resolution happens.
    """

    kind: str
    lease: Any = None
    tenant: Any = None
    property: Any = None
    holding: Any = None
    _lease_ids: tuple = ()

    # ---- constructors ---------------------------------------------------
    @classmethod
    def portfolio(cls) -> Scope:
        return cls("portfolio")

    @classmethod
    def of_lease(cls, lease) -> Scope:
        return cls("lease", lease=lease)

    @classmethod
    def of_property(cls, property) -> Scope:  # noqa: A002 — mirrors the field name
        return cls("property", property=property)

    @classmethod
    def of_holding(cls, holding) -> Scope:
        return cls("holding", holding=holding)

    @classmethod
    def of_tenant(cls, tenant, *, lease=None) -> Scope:
        """A tenant's own charges plus the household charges of their leases.

        The lease ids are resolved eagerly into a tuple rather than expressed as
        a `lease__lease_tenants__tenant=` join, because that join fans out: one
        charge row can come back more than once, and a `Sum()` over a fanned-out
        join silently double-counts. Resolving first costs one cheap query and
        makes every aggregate below safe without `.distinct()`.
        """
        from rentium.leases.models import LeaseTenant

        qs = LeaseTenant.objects.filter(tenant=tenant)
        if lease is not None:
            qs = qs.filter(lease=lease)
        lease_ids = tuple(qs.values_list("lease_id", flat=True).distinct())
        return cls("tenant", tenant=tenant, lease=lease, _lease_ids=lease_ids)

    # ---- the one predicate ----------------------------------------------
    def charge_q(self) -> Q:
        """Rule 1: which charges belong to this scope."""
        if self.kind == "portfolio":
            return Q()
        if self.kind == "lease":
            return Q(lease=self.lease)
        if self.kind == "property":
            return Q(property=self.property)
        if self.kind == "holding":
            return Q(holding=self.holding)
        if self.kind == "tenant":
            # Household charges on a joint lease carry tenant=None — they are
            # owed by this tenant in full, so they must be in scope.
            joint = Q(tenant__isnull=True, lease_id__in=self._lease_ids)
            own = Q(tenant=self.tenant)
            if self.lease is not None:
                own &= Q(lease=self.lease)
            return own | joint
        raise ValueError(f"unknown scope kind: {self.kind!r}")

    def settlement_q(self) -> Q:
        """Rule 2: a settlement is in scope iff its charge is."""
        return _prefix_q(self.charge_q(), "settles")

    # A plain method, not a @property: this class has a FIELD called `property`
    # (mirroring the ledger column), which shadows the builtin inside the class
    # body and would make the decorator resolve to that field's None default.
    def label(self) -> str:
        target = self.lease or self.tenant or self.property or self.holding
        return f"{self.kind}:{target}" if target is not None else self.kind


@dataclass(frozen=True)
class Position:
    """Every financial quantity a reader could want, for one scope, at one date.

    All Decimals, quantized to cents. Callers stringify at their own edge — this
    stays numeric so that agreement between two readers is testable with `==`.
    """

    scope: str
    as_of: _date

    # All charge types.
    charged_total: Decimal
    settled_total: Decimal
    outstanding_now: Decimal
    outstanding_all: Decimal
    open_count: int
    overdue_count: int

    # Income only — deposits are a refundable liability, never income.
    income_charged: Decimal
    income_outstanding_now: Decimal
    income_outstanding_all: Decimal
    income_open_count: int
    income_overdue_count: int

    # Deposits, reported separately so they are disclosed without being misfiled.
    deposits_charged: Decimal
    deposits_settled: Decimal
    deposits_returned: Decimal
    deposits_held: Decimal
    deposits_outstanding: Decimal

    # Damage recovery — contested by nature, so never folded into expected income.
    damage_outstanding_all: Decimal


def financial_position(landlord, *, scope: Scope | None = None, as_of=None) -> Position:
    """The single computation every financial read projects from.

    Adding a reader means calling this and naming the field you want. It must
    never mean writing another aggregate — that is how the readers drifted
    apart in the first place.
    """
    scope = scope or Scope.portfolio()
    as_of = as_of or _date.today()
    charge_q = scope.charge_q()

    base = LedgerEntry.objects.filter(landlord=landlord).filter(charge_q)

    # Every charge in scope, live (not voided), with settlement annotated.
    charges = base.charges().not_voided().with_settlement()
    open_all = charges.open_charges(as_of=as_of)

    all_agg = charges.aggregate(
        charged=Sum("amount"), settled=Sum("settled_amount"), balance=Sum("outstanding"),
    )
    open_agg = open_all.aggregate(total=Sum("outstanding"), count=Count("id"))
    income_open = open_all.income_charges()
    income_open_agg = income_open.aggregate(total=Sum("outstanding"), count=Count("id"))

    income_all = charges.income_charges().aggregate(
        charged=Sum("amount"), balance=Sum("outstanding"),
    )
    deposits_all = charges.deposit_charges().aggregate(
        charged=Sum("amount"), settled=Sum("settled_amount"),
    )
    deposits_open = open_all.deposit_charges().aggregate(total=Sum("outstanding"))
    damage_all = charges.damage_claims().aggregate(balance=Sum("outstanding"))

    # Rule 2 in one line: deposit money actually RECEIVED is found through the
    # charge it settles, never through the payment's own tenant/lease columns.
    # CREDIT is excluded on purpose — a discount is not cash you are holding.
    deposit_payments = (
        LedgerEntry.objects.not_voided()
        .filter(
            landlord=landlord,
            entry_type=EntryType.PAYMENT,
            settles__entry_type=EntryType.DEPOSIT_CHARGE,
        )
        .filter(scope.settlement_q())
        .aggregate(s=Sum("amount"))["s"]
    )
    # A DEPOSIT_RETURN is its own entry, not a settlement, so it scopes directly.
    returned = (
        LedgerEntry.objects.not_voided()
        .filter(landlord=landlord, entry_type=EntryType.DEPOSIT_RETURN)
        .filter(charge_q)
        .aggregate(s=Sum("amount"))["s"]
    )

    received = _money(deposit_payments)
    returned = _money(returned)

    return Position(
        scope=scope.label(),
        as_of=as_of,
        charged_total=_money(all_agg["charged"]),
        settled_total=_money(all_agg["settled"]),
        outstanding_now=_money(open_agg["total"]),
        outstanding_all=_money(all_agg["balance"]),
        open_count=open_agg["count"] or 0,
        overdue_count=open_all.filter(due_date__lt=as_of).count(),
        income_charged=_money(income_all["charged"]),
        income_outstanding_now=_money(income_open_agg["total"]),
        income_outstanding_all=_money(income_all["balance"]),
        income_open_count=income_open_agg["count"] or 0,
        income_overdue_count=income_open.filter(due_date__lt=as_of).count(),
        deposits_charged=_money(deposits_all["charged"]),
        deposits_settled=received,
        deposits_returned=returned,
        deposits_held=received - returned,
        deposits_outstanding=_money(deposits_open["total"]),
        damage_outstanding_all=_money(damage_all["balance"]),
    )


def charges_in_scope(landlord, *, scope: Scope, as_of=None):
    """The annotated charge rows behind a Position, for readers that list them.

    Same predicate as `financial_position`, so a list and the totals beside it
    can never describe different sets of charges.
    """
    return (
        LedgerEntry.objects.filter(landlord=landlord)
        .filter(scope.charge_q())
        .charges()
        .not_voided()
        .with_settlement()
    )
