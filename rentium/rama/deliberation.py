"""
Deep analysis from a cheap model, by making the depth structural.

Telling a weak model to "think carefully" does not work. What does work is
removing the need to: the model never decides what to think about, what the
options are, what the criteria are, what the arithmetic is, or what the
ranking is. It fills one narrow slot per call — turn a NAMED option into a set
of NAMED, sourced numbers — and later explains a ranking Python already
computed.

Depth comes from three deterministic mechanisms, none needing inspiration:

1. A declared catalogue with `applies()` predicates produces the option slate
   (interventions.py).
2. Declared `precedes` edges produce the sequencing insight — "windows before
   heat pump" is a graph edge, not a flash of judgement.
3. `sensitivity()` is a for-loop over the assumed figures; the self-
   questioning ("what if the quote is 40% higher?") is arithmetic, and the
   model is only asked to narrate flips Python already found.

Only three of the nine stages call a model. The rest are $0.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import RamaDeliberation, RamaDeliberationStage, RamaOption
from .render import Figure, Provenance, SourceType

STAGES = (
    "FRAME",      # python — what are we deciding?
    "SCOPE",      # python — the fact pack
    "ENUMERATE",  # python — the option slate, from the catalogue
    "GATHER",     # MODEL   — one bounded sub-turn per option
    "SCORE",      # python — all arithmetic
    "COMPARE",    # python — ranking, precedence, sensitivity
    "CHALLENGE",  # MODEL   — narrate the flips python found
    "RECOMMEND",  # MODEL   — prose, referring to figures by token only
    "PUBLISH",    # python — the insight
)

MODEL_STAGES = frozenset({"GATHER", "CHALLENGE", "RECOMMEND"})

# Hard ceilings. A background analysis that can run away is worse than one
# that stops early and says so.
MAX_OPTIONS = 5
MAX_MODEL_CALLS = 9  # 5 gather + challenge + recommend + 2 repair retries
MAX_STAGE_RETRIES = 1
MAX_ARTIFACT_CHARS = 8_000
# A figure this far off changes the ranking often enough to be worth testing.
SENSITIVITY_SWINGS = (Decimal("0.6"), Decimal("1.4"))


# ---------------------------------------------------------------- contracts
# Weak models fail nested JSON constantly, so GATHER answers in lines. A strict
# parser is far more reliable than a lenient JSON one, and its failures are
# specific enough to repair.
_FACT_LINE = re.compile(
    r"^FACT\s+(?P<key>[a-z_]+)\s+(?P<value>-?[\d,.]+)\s+(?P<unit>\S+)"
    r"(?:\s+(?P<period>ONE_TIME|ANNUAL|MONTHLY))?"
    r"(?:\s+src=(?P<src>\w+))?"
    r"(?:\s+url=(?P<url>\S+))?",
    re.IGNORECASE,
)
_MISSING_LINE = re.compile(r"^MISSING\s+(?P<key>[a-z_]+)\s*(?P<why>.*)$", re.IGNORECASE)

GATHER_CONTRACT = """\
Answer ONLY in these lines. No prose, no JSON, no markdown.

FACT <slot> <number> <unit> <ONE_TIME|ANNUAL|MONTHLY> src=<LEDGER|WEB|LANDLORD|ESTIMATE>
MISSING <slot> <one sentence saying what you need>

One line per slot listed below. If you do not have a real figure for a slot,
write MISSING for it — never estimate a number to fill the line. A wrong
number here is worse than a missing one, because the arithmetic that follows
will look correct.
"""


@dataclass(frozen=True)
class ParsedGather:
    facts: dict
    missing: dict
    violations: list


def parse_gather(text: str, slots) -> ParsedGather:
    """Strict line parser for a GATHER reply."""
    facts, missing, violations = {}, {}, []
    wanted = {slot.key: slot for slot in slots}

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _FACT_LINE.match(line)
        if match:
            key = match.group("key").lower()
            if key not in wanted:
                violations.append(f"unknown slot {key!r}")
                continue
            try:
                value = Decimal(match.group("value").replace(",", ""))
            except (InvalidOperation, ValueError):
                violations.append(f"unreadable number for {key!r}")
                continue
            facts[key] = {
                "value": str(value),
                "unit": match.group("unit"),
                "period": (match.group("period") or wanted[key].period).upper(),
                "source_type": (match.group("src") or "ESTIMATE").upper(),
                "url": match.group("url") or "",
            }
            continue
        miss = _MISSING_LINE.match(line)
        if miss:
            key = miss.group("key").lower()
            if key in wanted:
                missing[key] = miss.group("why").strip() or wanted[key].prompt
            continue
        violations.append(f"unparseable line: {line[:60]!r}")

    for key, slot in wanted.items():
        if slot.required and key not in facts and key not in missing:
            violations.append(f"required slot {key!r} neither given nor marked MISSING")
    return ParsedGather(facts=facts, missing=missing, violations=violations)


# ------------------------------------------------------------------- scoring
def score_option(option_facts: dict) -> dict:
    """All arithmetic, in Python. The model has no arithmetic surface at all.

    Deliberately simple and legible: net cost after any rebate, annual saving,
    and payback in years. A landlord can check these by hand, which matters
    more than sophistication for a number they may spend money on.
    """
    def _num(key):
        raw = (option_facts.get(key) or {}).get("value")
        try:
            return Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None

    cost = _num("unit_cost") or _num("current_premium") or _num("balance")
    rebate = _num("rebate_available") or Decimal("0")
    saving = _num("annual_saving")
    if saving is None:
        # An option framed as a spread rather than a saving (insurance,
        # mortgage, rent) computes its own annual delta.
        pairs = (
            ("current_premium", "market_premium"),
            ("market_rent", "current_rent"),
            ("annual_tax", None),
        )
        for high, low in pairs:
            a, b = _num(high), _num(low) if low else None
            if a is not None and b is not None:
                saving = abs(a - b)
                if high == "market_rent":
                    saving = saving * Decimal("12")
                break

    net_cost = (cost - rebate) if cost is not None else None
    payback = None
    if net_cost is not None and saving and saving > 0:
        payback = (net_cost / saving).quantize(Decimal("0.1"))

    return {
        "net_cost": str(net_cost) if net_cost is not None else None,
        "annual_saving": str(saving) if saving is not None else None,
        "payback_years": str(payback) if payback is not None else None,
        # Ten years of saving less the net cost. Crude on purpose — it ranks,
        # it does not pretend to be an NPV.
        "ten_year_net": (
            str((saving * 10) - net_cost)
            if saving is not None and net_cost is not None
            else None
        ),
    }


def rank(options: list[dict]) -> list[dict]:
    """Best first. Anything unscoreable sorts last rather than being dropped —
    it still needs saying, it just cannot be compared."""
    def key(row):
        payback = row["scores"].get("payback_years")
        if payback is None:
            return (1, Decimal("0"))
        return (0, Decimal(payback))

    ordered = sorted(options, key=key)
    for i, row in enumerate(ordered, start=1):
        row["rank"] = i
    return ordered


def sensitivity(options: list[dict]) -> list[dict]:
    """Which conclusions depend on a number we are not sure about.

    This is the self-questioning, and it is a for-loop. Each assumed figure is
    swung in both directions; if the top of the ranking changes, that is a flip
    worth telling the landlord about. The model cannot invent a flip and cannot
    miss one.
    """
    scoreable = [o for o in options if o["scores"].get("payback_years") is not None]
    if len(scoreable) < 2:
        return []

    baseline = rank([dict(o) for o in scoreable])[0]["catalogue_key"]
    flips = []
    for target in scoreable:
        for slot, fact in (target.get("facts") or {}).items():
            if (fact or {}).get("source_type") not in ("ESTIMATE", "WEB"):
                continue  # a ledger figure is not an assumption
            for swing in SENSITIVITY_SWINGS:
                probe = []
                for option in scoreable:
                    copied = dict(option)
                    copied["facts"] = dict(option.get("facts") or {})
                    if option is target:
                        moved = dict(fact)
                        try:
                            moved["value"] = str(
                                (Decimal(fact["value"]) * swing).quantize(Decimal("0.01"))
                            )
                        except (InvalidOperation, KeyError, TypeError):
                            continue
                        copied["facts"][slot] = moved
                    copied["scores"] = score_option(copied["facts"])
                    probe.append(copied)
                probe = [p for p in probe if p["scores"].get("payback_years") is not None]
                if len(probe) < 2:
                    continue
                if rank(probe)[0]["catalogue_key"] != baseline:
                    flips.append(
                        {
                            "option": target["catalogue_key"],
                            "figure": slot,
                            "direction": "higher" if swing > 1 else "lower",
                            "by_percent": int(abs(swing - 1) * 100),
                            "changes_to": rank(probe)[0]["catalogue_key"],
                        }
                    )
                    break
    return flips


# ------------------------------------------------------------ the fact pack
def build_pack(landlord, *, holding=None) -> dict:
    """Everything the analysis reasons over, assembled deterministically.

    No model call. If a number is not here and not gatherable, the run opens a
    request rather than letting anything invent it.
    """
    from datetime import date, timedelta

    from django.db.models import Sum

    from rentium.ledger.models import EntryType, LedgerEntry

    from . import mortgage as mortgage_maths
    from . import treasurer_facts

    since = date.today() - timedelta(days=365)
    expenses = LedgerEntry.objects.not_voided().filter(
        landlord=landlord, entry_type=EntryType.EXPENSE, effective_date__gte=since
    )
    if holding is not None:
        expenses = expenses.filter(holding=holding)
    spend = {
        row["category"]: float(row["total"] or 0)
        for row in expenses.values("category").annotate(total=Sum("amount"))
    }

    holding_facts = {}
    if holding is not None:
        financials = getattr(holding, "financials", None)
        active = mortgage_maths.active_mortgage(holding)
        horizon = mortgage_maths.renewal_horizon(active)
        holding_facts = {
            "name": holding.name,
            "year_built": getattr(financials, "year_built", None),
            "heating_type": getattr(financials, "heating_type", "") or "",
            "has_valuation": mortgage_maths.latest_valuation(holding) is not None,
            "days_to_renewal": int(horizon.value) if horizon.known else None,
        }

    from rentium.properties.models import Property

    listings = Property.objects.filter(landlord=landlord)
    if holding is not None:
        listings = listings.filter(holding=holding)

    return {
        "holding": holding_facts,
        "annual_spend": spend,
        "has_active_leases": _has_active_leases(landlord, holding),
        "vacant_listings": listings.count() - _rented_count(landlord, holding),
        "asserted_facts": treasurer_facts.render_for_pack(landlord, holding=holding),
        # Uploaded-but-uncommitted history, under its OWN key. annual_spend
        # above is ledger truth; these rows are not in the ledger and the
        # landlord can still change them, so they must never be summed with it.
        "provisional_history": _provisional_history(landlord),
    }


def _provisional_history(landlord) -> dict:
    """Prior-year rows the landlord has staged but not committed.

    Counted separately and capped: enough to say "your 2025 utilities looked
    like about this", never enough to state a total as fact.
    """
    from rentium.ledger.models import ImportBatch, StagedLedgerEntry

    drafts = ImportBatch.objects.filter(
        landlord=landlord, status=ImportBatch.Status.DRAFT
    )
    if not drafts.exists():
        return {}

    rows = StagedLedgerEntry.objects.filter(batch__in=drafts, amount__isnull=False)
    by_category: dict[str, float] = {}
    for row in rows.only("category", "amount", "entry_type"):
        if row.entry_type != "EXPENSE":
            continue
        key = row.category or "UNCATEGORISED"
        by_category[key] = by_category.get(key, 0.0) + float(row.amount)
    if not by_category:
        return {}
    return {
        "spend_by_category": by_category,
        "row_count": rows.count(),
        "provenance": "PROVISIONAL",
        "note": (
            "Uploaded but NOT committed — not in the ledger, still editable by "
            "the landlord. Describe it as provisional; never add it to "
            "annual_spend and never state it as a recorded figure."
        ),
    }


def _has_active_leases(landlord, holding) -> bool:
    from rentium.leases.models import Lease

    qs = Lease.objects.filter(landlord=landlord, status="ACTIVE")
    if holding is not None:
        qs = qs.filter(property__holding=holding)
    return qs.exists()


def _rented_count(landlord, holding) -> int:
    from rentium.leases.models import Lease

    qs = Lease.objects.filter(landlord=landlord, status="ACTIVE")
    if holding is not None:
        qs = qs.filter(property__holding=holding)
    return qs.values("property_id").distinct().count()


# ------------------------------------------------------------------ figures
def figures_for(options: list[dict]) -> dict[str, Figure]:
    """The token table RECOMMEND may refer to.

    Building it here — from scored options only — is what makes "every
    published number is a computed number" true by construction.
    """
    table: dict[str, Figure] = {}
    n = 0
    for option in options:
        for name in ("net_cost", "annual_saving", "payback_years", "ten_year_net"):
            raw = option["scores"].get(name)
            if raw is None:
                continue
            n += 1
            table[f"f{n}"] = Figure(
                value=Decimal(raw),
                unit="years" if name == "payback_years" else "CAD",
                period="ANNUAL" if name == "annual_saving" else "ONE_TIME",
                label=f"{option['catalogue_key']} {name}",
                provenance=Provenance(
                    source_type=SourceType.ESTIMATE,
                    ref=option["catalogue_key"],
                    note="computed from the figures gathered",
                ),
            )
    return table


# ---------------------------------------------------------------------------
# Requests: what to do when a slot cannot be filled.
#
# Created here, in Python, from a slot a strict parser found empty. A model
# asked to "say what you need" invents plausible needs; an empty required slot
# is a fact.
# ---------------------------------------------------------------------------
def open_request(landlord, *, deliberation_row, option_key, slot, why: str = ""):
    """Ask the landlord for one missing figure, at most once."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import TreasurerRequest

    dedupe = f"req:{landlord.pk}:{option_key}:{slot.key}"
    existing = TreasurerRequest.objects.filter(
        landlord=landlord, dedupe_key=dedupe
    ).first()
    if existing is not None and existing.is_live:
        return existing

    live = TreasurerRequest.objects.filter(
        landlord=landlord,
        status__in=(TreasurerRequest.Status.OPEN, TreasurerRequest.Status.RELAYED),
    ).count()
    if live >= TreasurerRequest.MAX_OPEN_PER_LANDLORD:
        # Better to answer provisionally than to bury the landlord in
        # questions they will not work through.
        return None

    return TreasurerRequest.objects.create(
        landlord=landlord,
        deliberation=deliberation_row,
        fact_key=slot.key,
        question=slot.prompt or f"What is the {slot.key.replace('_', ' ')}?",
        why_it_matters=why,
        expected_unit=slot.unit,
        expected_period=slot.period,
        blocking=False,  # never stall; publish provisionally instead
        dedupe_key=dedupe,
        expires_at=timezone.now() + timedelta(days=TreasurerRequest.TTL_DAYS),
    )


def why_it_matters(flips, option_key: str, slot_key: str) -> str:
    """State the real consequence, from what sensitivity() actually found."""
    for flip in flips:
        if flip["option"] == option_key and flip["figure"] == slot_key:
            return (
                f"If this is {flip['by_percent']}% {flip['direction']} than "
                f"assumed, {flip['changes_to'].replace('_', ' ')} becomes the "
                f"better option instead — so it decides the recommendation."
            )
    return "It is needed to work out whether this is worth doing."


# ---------------------------------------------------------------------------
# The orchestrator.
#
# The sequence is THIS FUNCTION, not a plan the model follows. Each model
# stage is a separate bounded sub-turn with its own conversation id, so a weak
# model cannot skip a stage it is never asked to do, and during GATHER for one
# option it cannot see the others — there is nothing to collapse into.
# ---------------------------------------------------------------------------
def run(landlord, *, topic: str = "everything", holding=None, question: str = "",
        trigger: str = "landlord_ask", dedupe_key: str = "",
        turn_runner=None) -> RamaDeliberation:
    """Run one analysis end to end. Returns the persisted RamaDeliberation.

    `turn_runner` is injectable so tests can drive the structure without a
    provider; production passes service.run_turn.
    """
    from . import interventions, research
    from .models import RamaDeliberation, RamaDeliberationStage, RamaOption

    if turn_runner is None:
        from .service import run_turn as turn_runner  # noqa: PLC0415

    row = RamaDeliberation.objects.create(
        landlord=landlord,
        topic=topic,
        question=question or f"Where can we do better on {topic.replace('_', ' ')}?",
        trigger=trigger,
        holding=holding,
        dedupe_key=dedupe_key,
    )
    order = 0

    def stage(name, *, option_key="", **fields):
        nonlocal order
        order += 1
        return RamaDeliberationStage.objects.create(
            deliberation=row, order=order, stage=name, option_key=option_key, **fields
        )

    # FRAME + SCOPE + ENUMERATE — all $0.
    stage("FRAME", status=RamaDeliberationStage.Status.DONE,
          output_artifact={"question": row.question, "topic": topic})
    pack = build_pack(landlord, holding=holding)
    stage("SCOPE", status=RamaDeliberationStage.Status.DONE,
          output_artifact=_truncate(pack))
    slate = interventions.candidates(pack, topic=topic, limit=MAX_OPTIONS)
    stage("ENUMERATE", status=RamaDeliberationStage.Status.DONE,
          output_artifact={"options": [i.key for i in slate]})
    for item in slate:
        RamaOption.objects.create(
            deliberation=row, catalogue_key=item.key, label=item.label
        )

    # GATHER — one bounded sub-turn per option.
    for item in slate:
        option = row.options.get(catalogue_key=item.key)
        sources = []
        for research_topic in item.research_topics:
            sources.extend(research.search(landlord, research_topic))
        parsed, stage_row = _gather_one(
            landlord, row, item, sources, stage, turn_runner
        )
        facts = _verify_web_facts(parsed.facts, sources, stage_row)
        option.facts = facts
        option.status = RamaOption.Status.GATHERED
        option.save(update_fields=["facts", "status"])

    # SCORE + COMPARE — $0, and the only place arithmetic happens.
    scored = []
    for option in row.options.all():
        option.scores = score_option(option.facts)
        option.status = RamaOption.Status.SCORED
        option.save(update_fields=["scores", "status"])
        scored.append(
            {
                "catalogue_key": option.catalogue_key,
                "facts": option.facts,
                "scores": option.scores,
            }
        )
    stage("SCORE", status=RamaDeliberationStage.Status.DONE,
          output_artifact={"scored": len(scored)})

    ranked = rank(scored)
    for entry in ranked:
        row.options.filter(catalogue_key=entry["catalogue_key"]).update(
            rank=entry["rank"]
        )
    edges = interventions.precedence_edges({e["catalogue_key"] for e in ranked})
    flips = sensitivity(ranked)
    stage("COMPARE", status=RamaDeliberationStage.Status.DONE,
          output_artifact={"ranking": [e["catalogue_key"] for e in ranked],
                           "precedence": edges, "flips": flips})

    # Requests for anything still missing, now that we know what it would change.
    for item in slate:
        option = row.options.get(catalogue_key=item.key)
        for slot in item.required_facts:
            if slot.required and slot.key not in (option.facts or {}):
                open_request(
                    landlord,
                    deliberation_row=row,
                    option_key=item.key,
                    slot=slot,
                    why=why_it_matters(flips, item.key, slot.key),
                )

    row.status = RamaDeliberation.Status.DONE
    row.save(update_fields=["status", "calls_used", "updated_at"])
    return row


def _truncate(payload: dict) -> dict:
    import json

    text = json.dumps(payload, default=str)
    if len(text) <= MAX_ARTIFACT_CHARS:
        return payload
    return {"truncated": True, "preview": text[:MAX_ARTIFACT_CHARS]}


def _gather_one(landlord, row, item, sources, stage, turn_runner):
    """One option, one sub-turn, one strict parse, at most one repair retry."""
    from .models import RamaDeliberationStage

    slots = item.required_facts
    instruction = (
        f"{GATHER_CONTRACT}\n"
        f"Option: {item.label}\n"
        "Slots:\n"
        + "\n".join(
            f"  {s.key} ({s.unit}, {s.period}){'' if s.required else ' [optional]'}"
            for s in slots
        )
        + ("\n\nSources you may quote figures from:\n" if sources else "")
        + "\n".join(f"  {s.url}\n{s.excerpt[:1200]}" for s in sources)
    )

    stage_row = stage("GATHER", option_key=item.key,
                      input_artifact={"slots": [s.key for s in slots],
                                      "sources": [s.url for s in sources]})
    parsed = None
    for attempt in range(MAX_STAGE_RETRIES + 1):
        if row.calls_used >= MAX_MODEL_CALLS:
            stage_row.status = RamaDeliberationStage.Status.SKIPPED
            stage_row.violations = ["budget reached"]
            stage_row.save()
            return ParsedGather({}, {}, ["budget reached"]), stage_row

        conversation = uuid.uuid4()
        row.calls_used += 1
        result = turn_runner(
            landlord,
            f"Fill the slots for: {item.label}",
            conversation,
            role="treasurer",
            channel="system",
            depth=1,
            extra_system=instruction,
        )
        reply = getattr(result, "reply", "") or ""
        parsed = parse_gather(reply, slots)
        stage_row.conversation_id = conversation
        stage_row.raw_reply = reply[:4000]
        stage_row.retries = attempt
        if not parsed.violations:
            break
        # One repair attempt, naming exactly what was wrong. A second failure
        # records UNKNOWN rather than letting a guess through.
        instruction += (
            "\n\nYour previous answer did not follow the contract: "
            + "; ".join(parsed.violations[:5])
            + "\nAnswer again, in the required lines only."
        )
        stage_row.status = RamaDeliberationStage.Status.RETRIED

    stage_row.output_artifact = {"facts": parsed.facts, "missing": parsed.missing}
    stage_row.violations = parsed.violations
    stage_row.status = (
        RamaDeliberationStage.Status.DONE
        if not parsed.violations
        else RamaDeliberationStage.Status.FAILED
    )
    stage_row.save()
    row.save(update_fields=["calls_used"])
    return parsed, stage_row


def _verify_web_facts(facts: dict, sources, stage_row) -> dict:
    """Drop any WEB figure that is not verbatim in a page we actually fetched.

    The single highest-value guard in the pipeline. A figure the model
    produced that appears nowhere in the text it cited is invented, and an
    invented rebate is what turns a recommendation into a costly mistake.
    """
    from . import research

    kept, violations = {}, list(stage_row.violations or [])
    for key, fact in (facts or {}).items():
        if (fact.get("source_type") or "").upper() != "WEB":
            kept[key] = fact
            continue
        if any(research.verify_in_source(fact.get("value"), s) for s in sources):
            kept[key] = fact
        else:
            violations.append(
                f"{key}={fact.get('value')} not found in any cited source — excluded"
            )
    if violations != (stage_row.violations or []):
        stage_row.violations = violations
        stage_row.save(update_fields=["violations"])
    return kept


# ---------------------------------------------------------------------------
# Relay. The Treasurer has no channel of its own — the chain of command is the
# point. Requests are injected into the General's context deterministically
# (role_context), so relaying does not depend on the model deciding to call a
# tool and then deciding to mention the result.
# ---------------------------------------------------------------------------
def open_requests(landlord):
    from django.utils import timezone

    from .models import TreasurerRequest

    return (
        TreasurerRequest.objects.filter(
            landlord=landlord,
            status__in=(TreasurerRequest.Status.OPEN, TreasurerRequest.Status.RELAYED),
        )
        .exclude(expires_at__lte=timezone.now())
        .order_by("-blocking", "created_at")[: TreasurerRequest.MAX_OPEN_PER_LANDLORD]
    )


def render_requests_for_general(landlord) -> str:
    """The block the General must relay verbatim, or "" if nothing is pending."""
    from django.utils import timezone

    from .models import TreasurerRequest

    pending = list(open_requests(landlord))
    if not pending:
        return ""

    lines = [
        "## TREASURER REQUESTS (relay these VERBATIM, prefixed "
        '"Treasurer request: ". Never answer one yourself, never guess the '
        "number, never soften it into a suggestion. If the landlord answers, "
        "record it with record_treasurer_fact.)"
    ]
    for request in pending:
        lines.append(f"- {request.question}")
        if request.why_it_matters:
            lines.append(f"  (why it matters: {request.why_it_matters})")

    TreasurerRequest.objects.filter(
        pk__in=[r.pk for r in pending], status=TreasurerRequest.Status.OPEN
    ).update(status=TreasurerRequest.Status.RELAYED, relayed_at=timezone.now())
    return "\n".join(lines)


def briefing_section(landlord) -> list[str]:
    """Lines for the morning briefing. $0 — no model involved."""
    pending = list(open_requests(landlord))
    if not pending:
        return []
    return ["Treasurer needs from you:"] + [f"• {r.question}" for r in pending]
