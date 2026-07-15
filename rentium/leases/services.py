"""
Business logic that spans more than a single model method, or that needs to
be callable identically from more than one place (an API view today, tests,
and eventually agent tooling). Model methods stay on the model when they're
genuinely about that one model's own state (Lease.check_and_activate(), for
example); anything that's closer to "a calculation" than "a state change"
lives here instead.
"""

from decimal import Decimal


def compute_rent_split(rows, total_rent):
    """
    The single source of truth for the "equal split with manual override
    cascading" rule described to tenants/landlords as: edit one person's
    rent and the others automatically absorb the difference, so the total
    always adds up.

    This used to be implemented independently in two places in the
    frontend (CreateLeaseForm.tsx's step 3, and LeaseDetail.tsx's tenant
    roster editor) with no shared backend equivalent — meaning an API
    caller (including a future agent) had no way to compute a valid split
    without reimplementing this algorithm itself, and the two frontend
    copies could silently drift out of sync with each other. Now there's
    exactly one implementation, and both the API (via LeaseViewSet's
    `preview-split` action) and the frontend (which just calls that
    endpoint) go through it.

    Args:
        rows: list of dicts, each with:
            - id: str | None (existing LeaseTenant id, or None for a new,
              not-yet-created row)
            - rent_amount: Decimal | None (the row's current amount; None
              means "not manually set, please compute it")
            - touched: bool (True if a human explicitly typed an amount for
              this row — it should be treated as fixed, not recomputed)
            - has_signed: bool (True if this LeaseTenant has already
              signed — always treated as fixed, regardless of `touched`,
              since a signed tenant's rent_amount is locked at the model
              level and recomputing it here would just be immediately
              rejected on save anyway)
        total_rent: Decimal, the lease's total_rent to split across `rows`

    Returns:
        A new list of dicts in the same shape as `rows`, with `rent_amount`
        filled in as a Decimal (quantized to cents) for every row —
        unchanged for touched/signed rows, freshly computed for the rest so
        the full set sums to `total_rent`.

    A row that's both untouched AND unsigned is "editable" — those are the
    ones that get recomputed. If there are zero editable rows (everyone is
    either touched or signed), nothing changes; if there's nothing left to
    allocate after the fixed rows, editable rows get $0.00 rather than a
    negative number.
    """
    if not rows:
        return []

    total_rent = Decimal(total_rent or "0.00")

    fixed_rows = [r for r in rows if r["touched"] or r["has_signed"]]
    editable_rows = [r for r in rows if not r["touched"] and not r["has_signed"]]

    fixed_sum = sum(
        (Decimal(r["rent_amount"]) if r["rent_amount"] is not None else Decimal("0.00"))
        for r in fixed_rows
    )
    remaining = max(total_rent - fixed_sum, Decimal("0.00"))

    per_editable = (
        (remaining / Decimal(len(editable_rows))).quantize(Decimal("0.01"))
        if editable_rows
        else Decimal("0.00")
    )

    result = []
    for row in rows:
        if row["touched"] or row["has_signed"]:
            amount = (
                Decimal(row["rent_amount"]).quantize(Decimal("0.01"))
                if row["rent_amount"] is not None
                else Decimal("0.00")
            )
        else:
            amount = per_editable
        result.append({**row, "rent_amount": amount})

    return result
