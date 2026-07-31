"""
Per-tool compactors: one short labelled line of facts per earlier tool call.

History replayed to the model is text-only, so facts a tool discovered last
turn used to evaporate — the structural cause of RAMA's hallucination class
("Room D has no images" invented two turns after find_listings said
otherwise). These digests re-ground those facts each turn as a compact
system section: provider-neutral, token-bounded, and easier for weak models
than raw JSON transcripts. LIVE PORTFOLIO stays authoritative on conflict.
"""

from __future__ import annotations

MAX_LINE_CHARS = 300
MAX_ITEMS = 8


def _clip(text: str, limit: int = MAX_LINE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _listing_bit(row: dict) -> str:
    bits = [str(row.get("name") or row.get("id") or "?")]
    if "image_count" in row:
        bits.append(f"{row['image_count']} imgs")
    lease = row.get("current_lease")
    if lease:
        bits.append(f"lease {lease.get('lease_number')} {lease.get('status')}")
    elif row.get("lease_count"):
        bits.append(f"{row['lease_count']} lease(s)")
    if row.get("open_work_orders"):
        bits.append(f"{row['open_work_orders']} open WO")
    if row.get("vacant_today") is True:
        bits.append("vacant")
    return f"{bits[0]} ({', '.join(bits[1:])})" if bits[1:] else bits[0]


def _digest_find_listings(arguments: dict, result: dict) -> str:
    rows = result.get("listings") or []
    parts = [_listing_bit(r) for r in rows[:MAX_ITEMS]]
    if len(rows) > MAX_ITEMS:
        parts.append(f"+{len(rows) - MAX_ITEMS} more")
    excluded = result.get("excluded") or []
    tail = f"; excluded: {', '.join(str(r.get('name')) for r in excluded)}" if excluded else ""
    return f"matched {len(rows)}: {', '.join(parts) or 'none'}{tail}"


def _digest_find_leases(arguments: dict, result: dict) -> str:
    rows = result.get("leases") or []
    parts = [
        f"{r.get('lease_number')} {r.get('status')} ({r.get('property')})"
        for r in rows[:MAX_ITEMS]
    ]
    if len(rows) > MAX_ITEMS:
        parts.append(f"+{len(rows) - MAX_ITEMS} more")
    return f"matched {len(rows)}: {', '.join(parts) or 'none'}"


def _digest_plan(arguments: dict, result: dict) -> str:
    plan = result.get("plan") or {}
    steps = plan.get("steps") or []
    blocked = plan.get("blocked") or []
    bits = [f"{plan.get('operation')}: {len(steps)} step(s)"]
    if blocked:
        bits.append(
            f"{len(blocked)} blocked ({', '.join(str(b.get('target')) for b in blocked[:4])})"
        )
    return "; ".join(bits)


def _digest_money(arguments: dict, result: dict) -> str:
    """What a money write actually did, with the numbers kept.

    `_digest_generic` looks for created/updated/deleted/done and then falls
    back to list SIZES. A money write matches neither: `record_payment` returns
    {ok, duplicate, entry_id, amount, still_owing}, so it digested to "" and
    `_recent_writes_note` skipped it too. The consequence was that on the turn
    AFTER recording $100, the prompt held no trace that it had happened — and
    the model had to re-derive the answer from live_context, whose top-level
    outstanding_total excludes deposits. It said the payment wasn't there.

    So this keeps the amounts. A digest that drops the number is worse than
    none for financial questions: it proves a call happened while losing the
    only fact the landlord asked about.
    """
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        return f"error: {result['error']}"
    if result.get("needs_confirm"):
        return ""  # a preview is not a fact yet
    if result.get("already_done"):
        return "not recorded — already on the books"

    bits = []
    amount = result.get("amount") or result.get("value") or arguments.get("amount")
    if amount:
        bits.append(f"${amount}")
    target = (
        result.get("charge")
        or result.get("subject")
        or result.get("property")
        or result.get("vendor")
        or ""
    )
    if isinstance(target, dict):
        target = target.get("name") or target.get("scope") or ""
    if target:
        bits.append(f"→ {target}")
    if result.get("duplicate"):
        bits.append("(already recorded, no second entry)")
    if result.get("still_owing") is not None:
        bits.append(f"{result['still_owing']} still owing")
    if result.get("counted_in_totals") is False:
        bits.append("kept OUT of totals (already in the books)")
    return " ".join(str(b) for b in bits if b)


def _digest_generic(arguments: dict, result: dict) -> str:
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        return f"error: {result['error']}"
    # Writes: what happened, to what.
    for verb in ("created", "updated", "deleted", "terminated", "done"):
        if result.get(verb):
            label = ""
            for key in ("property", "lease_number", "name"):
                val = result.get(key)
                if isinstance(val, str) and val:
                    label = val
                    break
                if isinstance(val, dict):
                    label = str(val.get("name") or val.get("lease_number") or "")
                    break
            return f"{verb} {label}".strip()
    # Reads: name the list sizes, which is usually the fact that matters.
    sizes = [
        f"{key}: {len(val)}"
        for key, val in result.items()
        if isinstance(val, list) and key not in ("instructions",)
    ]
    return ", ".join(sizes[:4])


_DIGESTERS = {
    "find_listings": _digest_find_listings,
    "find_leases": _digest_find_leases,
    "plan_operation": _digest_plan,
    "plan_move_tenant": _digest_plan,
    # Money writes: keep the amounts. See _digest_money.
    "record_payment": _digest_money,
    "create_expense": _digest_money,
    "record_treasurer_fact": _digest_money,
    "post_deposit_return": _digest_money,
    "reallocate_expense": _digest_money,
}


def digest_tool_call(tool: str, arguments: dict, result: dict) -> str:
    """One compact line of facts for this call, or '' if nothing useful."""
    fn = _DIGESTERS.get(tool, _digest_generic)
    try:
        line = fn(arguments or {}, result or {})
    except Exception:  # noqa: BLE001 — a digest must never break a turn
        return ""
    return _clip(f"{tool}: {line}") if line else ""
