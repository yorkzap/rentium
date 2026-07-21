"""
The morning briefing — a deterministic 5-minute-read digest of the whole
portfolio, built from the exact same facts every other RAMA surface uses
(state_of_the_union + open RamaInsights). Pure Python, $0 LLM by default;
`phrase=True` is a seam for an optional General pass later (off by default
so the everyday briefing costs nothing and never hallucinates a number).
"""

from __future__ import annotations

from datetime import date


def _severity_icon(sev: str) -> str:
    return {"URGENT": "🔴", "WARN": "🟡", "INFO": "🔵"}.get(sev, "•")


def build_briefing_text(landlord, *, phrase: bool = False) -> str:
    from .models import RamaInsight
    from .union import state_of_the_union

    snap = state_of_the_union(landlord)
    truth = snap.get("dashboard_truth") or {}
    lines = [f"Good morning — {date.today():%A, %B %-d}."]

    lines.append(
        f"Occupied {truth.get('occupied_today', '?')}. "
        f"This month: ${truth.get('collected_this_month', '0')} collected of "
        f"${truth.get('expected_this_month', '0')} expected."
    )
    outstanding_total = truth.get("outstanding_total", "0.00")
    overdue = truth.get("overdue_count", 0)
    if outstanding_total not in ("0.00", "0", 0):
        lines.append(
            f"Outstanding: ${outstanding_total}"
            + (f" ({overdue} overdue)" if overdue else "") + "."
        )

    insights = list(
        RamaInsight.objects.filter(landlord=landlord, status=RamaInsight.Status.OPEN)
        .order_by("-severity", "-created_at")[:5]
    )
    if insights:
        lines.append(f"\n{len(insights)} open insight(s):")
        for i in insights:
            headline = (i.analysis or i.kind).splitlines()[0][:160]
            lines.append(f"{_severity_icon(i.severity)} {headline}")
    else:
        lines.append("\nNo open insights.")

    today_appts = [
        a for a in (snap.get("upcoming_appointments") or [])
        if a.get("date") == date.today().isoformat()
    ]
    if today_appts:
        lines.append(f"\nToday: {len(today_appts)} appointment(s):")
        for a in today_appts[:5]:
            lines.append(
                f"• {a.get('time_local', '')} — {a.get('property', '')} "
                f"({a.get('kind', '')})"
            )

    text = "\n".join(lines)
    if phrase:
        text = _phrase(landlord, text)
    return text


def _phrase(landlord, deterministic_text: str) -> str:
    """Optional General pass to phrase the briefing more personally. Not
    wired to any beat schedule yet — the seam for later. Every number in
    the deterministic text is treated as ground truth the General must copy
    verbatim, never recompute."""
    from .service import run_turn

    result = run_turn(
        landlord,
        "Phrase this morning briefing warmly and briefly, in second person. "
        "Copy every number exactly — never recompute or round differently.",
        role="general",
        channel="system",
        depth=1,
        extra_system=f"## BRIEFING FACTS (copy exactly)\n{deterministic_text}",
    )
    return result.reply if result.error is None else deterministic_text
