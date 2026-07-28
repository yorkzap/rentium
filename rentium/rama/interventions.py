"""
The things a landlord could actually do to net more money.

This file is why the Treasurer can deliberate on a cheap model. The option
slate is not something the model brainstorms — it is a declared catalogue,
filtered by predicates over the fact pack. "Have you considered windows before
a heat pump?" is not an insight the model has to arrive at; it is a `precedes`
edge in a dataclass, and `deliberation.compare()` emits it whether the model is
Gemini Flash or the best model in the world.

That is the whole design bet: capability grows by adding entries here, not by
upgrading the model. A better model writes a better *explanation* of the same
ranking.

Nothing in this file calls an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class FactSlot:
    """A number an option needs before it can be scored."""

    key: str
    unit: str = "CAD"  # CAD | CAD/yr | percent | years | kWh
    period: str = "ONE_TIME"  # ONE_TIME | ANNUAL | MONTHLY
    # Where this number can legitimately come from. A slot sourceable only
    # from LANDLORD must never be filled by research or a guess.
    sourceable: str = "WEB"  # WEB | LEDGER | LANDLORD | ESTIMATE
    required: bool = True
    prompt: str = ""  # what to ask the landlord if it cannot be found


@dataclass(frozen=True)
class Intervention:
    key: str
    label: str
    family: str  # envelope | mechanical | operating | revenue | financing | tax
    applies: Callable[[dict], bool]
    required_facts: tuple[FactSlot, ...] = ()
    # Deterministic ordering. "window_replacement precedes heat_pump" means a
    # heat pump's sizing and payback depend on the envelope, so recommending
    # them in the wrong order wastes money. The model never decides this.
    precedes: tuple[str, ...] = ()
    disruption: int = 0  # 0-3, DECLARED, never model-judged
    tenant_impact: str = "none"  # none | notice | consent | displacement
    research_topics: tuple[str, ...] = ()
    reversible: bool = True
    note: str = ""


def _spend(pack: dict, category: str) -> float:
    return float((pack.get("annual_spend") or {}).get(category) or 0)


def _holding(pack: dict) -> dict:
    return pack.get("holding") or {}


# ---------------------------------------------------------------------------
# The catalogue. Deliberately broad from day one — "make more money" is not
# only about energy retrofits, and a slate that only ever offers insulation
# would be a narrow assistant wearing a finance hat.
# ---------------------------------------------------------------------------

WINDOW_REPLACEMENT = Intervention(
    key="window_replacement",
    label="Replace or upgrade windows",
    family="envelope",
    applies=lambda p: _spend(p, "UTILITIES") > 800
    and (_holding(p).get("year_built") or 9999) < 1995,
    required_facts=(
        FactSlot("unit_cost", prompt="What would a quote for the windows be?"),
        FactSlot("annual_saving", unit="CAD", period="ANNUAL"),
        FactSlot("rebate_available", required=False),
    ),
    # THE EDGE. A heat pump sized against a leaky envelope is oversized and
    # underperforms; doing the envelope first changes the right heat pump.
    precedes=("heat_pump", "furnace_replacement"),
    disruption=2,
    tenant_impact="notice",
    research_topics=("bc_window_rebate",),
    note="Sizing and payback for heating equipment depend on the envelope.",
)

HEAT_PUMP = Intervention(
    key="heat_pump",
    label="Install a heat pump",
    family="mechanical",
    applies=lambda p: _spend(p, "UTILITIES") > 600
    and _holding(p).get("heating_type", "") not in ("heat pump", "heat_pump"),
    required_facts=(
        FactSlot("unit_cost", prompt="What has an installer quoted?"),
        FactSlot("annual_saving", unit="CAD", period="ANNUAL"),
        FactSlot("rebate_available", required=False),
    ),
    disruption=2,
    tenant_impact="notice",
    research_topics=("bc_heat_pump_rebate", "cleanbc_income_qualified"),
)

ATTIC_INSULATION = Intervention(
    key="attic_insulation",
    label="Top up attic insulation",
    family="envelope",
    applies=lambda p: _spend(p, "UTILITIES") > 500
    and (_holding(p).get("year_built") or 9999) < 2005,
    required_facts=(
        FactSlot("unit_cost"),
        FactSlot("annual_saving", unit="CAD", period="ANNUAL"),
    ),
    precedes=("heat_pump",),
    disruption=1,
    research_topics=("bc_insulation_rebate",),
)

INSURANCE_RESHOP = Intervention(
    key="insurance_reshop",
    label="Re-shop the insurance",
    family="operating",
    applies=lambda p: _spend(p, "INSURANCE") > 0,
    required_facts=(
        FactSlot("current_premium", unit="CAD", period="ANNUAL", sourceable="LEDGER"),
        FactSlot(
            "market_premium",
            unit="CAD",
            period="ANNUAL",
            prompt="What have other insurers quoted?",
        ),
    ),
    disruption=0,
    note="No capital, no disruption — usually the cheapest money on the table.",
)

PROPERTY_TAX_APPEAL = Intervention(
    key="property_tax_appeal",
    label="Appeal the assessment",
    family="operating",
    applies=lambda p: _spend(p, "PROPERTY_TAX") > 0
    and bool(_holding(p).get("has_valuation")),
    required_facts=(
        FactSlot("assessed_value", sourceable="LEDGER"),
        FactSlot("market_value", sourceable="LANDLORD"),
        FactSlot("annual_tax", unit="CAD", period="ANNUAL", sourceable="LEDGER"),
    ),
    disruption=0,
    note="Only worth it where the assessment is above what the place would sell for.",
)

RENT_TO_MARKET = Intervention(
    key="rent_to_market",
    label="Close the gap to market rent",
    family="revenue",
    applies=lambda p: bool(p.get("has_active_leases")),
    required_facts=(
        FactSlot("current_rent", unit="CAD", period="MONTHLY", sourceable="LEDGER"),
        FactSlot(
            "market_rent",
            unit="CAD",
            period="MONTHLY",
            prompt="What are comparable units renting for?",
        ),
    ),
    disruption=0,
    tenant_impact="notice",
    note=(
        "Rent increases are capped and require notice in BC — this is about "
        "the allowed increase, not a jump to market."
    ),
)

VACANCY_REDUCTION = Intervention(
    key="vacancy_reduction",
    label="Shorten the turnover gap",
    family="revenue",
    applies=lambda p: float(p.get("vacant_listings") or 0) > 0,
    required_facts=(
        FactSlot("days_vacant", unit="days", sourceable="LEDGER"),
        FactSlot("monthly_rent", unit="CAD", period="MONTHLY", sourceable="LEDGER"),
    ),
    disruption=0,
)

MORTGAGE_RENEWAL = Intervention(
    key="mortgage_renewal",
    label="Plan the mortgage renewal",
    family="financing",
    applies=lambda p: (_holding(p).get("days_to_renewal") or 99999) < 365,
    required_facts=(
        FactSlot("current_rate", unit="percent", sourceable="LEDGER"),
        FactSlot(
            "available_rate",
            unit="percent",
            prompt="What rate have you been offered?",
        ),
        FactSlot("balance", sourceable="LEDGER"),
    ),
    disruption=0,
    note="The one dated decision here — the window closes on the term end.",
)

EXPENSE_CAPTURE = Intervention(
    key="expense_capture",
    label="Capture the expenses that aren't being claimed",
    family="tax",
    applies=lambda p: True,  # always worth checking
    required_facts=(
        FactSlot("claimed_total", unit="CAD", period="ANNUAL", sourceable="LEDGER"),
        FactSlot("marginal_rate", unit="percent", sourceable="ESTIMATE", required=False),
    ),
    disruption=0,
    note="Deductions already earned but never recorded are free money.",
)

CATALOGUE: dict[str, Intervention] = {
    i.key: i
    for i in (
        WINDOW_REPLACEMENT,
        HEAT_PUMP,
        ATTIC_INSULATION,
        INSURANCE_RESHOP,
        PROPERTY_TAX_APPEAL,
        RENT_TO_MARKET,
        VACANCY_REDUCTION,
        MORTGAGE_RENEWAL,
        EXPENSE_CAPTURE,
    )
}

# Which topics draw on which families, so a run stays on subject rather than
# proposing a heat pump when asked about financing.
TOPICS: dict[str, tuple[str, ...]] = {
    "energy_retrofit": ("envelope", "mechanical"),
    "operating_cost": ("operating",),
    "revenue": ("revenue",),
    "financing": ("financing",),
    "tax": ("tax",),
    "everything": ("envelope", "mechanical", "operating", "revenue", "financing", "tax"),
}
TOPIC_ROTATION = ("operating_cost", "revenue", "energy_retrofit", "financing", "tax")


def candidates(pack: dict, *, topic: str = "everything", limit: int = 5) -> list[Intervention]:
    """The option slate for this portfolio, from the catalogue only.

    Deterministic: same pack, same topic, same slate, in the same order — which
    is what lets an eval assert that switching provider changes the prose and
    nothing else.
    """
    families = set(TOPICS.get(topic) or TOPICS["everything"])
    picked = [
        item
        for item in CATALOGUE.values()
        if item.family in families and _safe_applies(item, pack)
    ]
    # Cheapest and least disruptive first — a deterministic prior, so the model
    # is never asked to rank before the numbers exist.
    picked.sort(key=lambda i: (i.disruption, i.key))
    return picked[:limit]


def _safe_applies(item: Intervention, pack: dict) -> bool:
    """A broken predicate must drop one option, never break the run."""
    try:
        return bool(item.applies(pack))
    except Exception:  # noqa: BLE001
        return False


def precedence_edges(keys) -> list[dict]:
    """Ordering constraints among the chosen options.

    This is the heat-pump-vs-windows insight, produced by walking declared
    edges. It exists whether or not the model would have thought of it.
    """
    chosen = set(keys)
    edges = []
    for key in sorted(chosen):
        item = CATALOGUE.get(key)
        if item is None:
            continue
        for later in item.precedes:
            if later in chosen:
                edges.append(
                    {
                        "from": key,
                        "to": later,
                        "why": item.note
                        or f"{item.label} should come before {CATALOGUE[later].label}.",
                    }
                )
    return edges
