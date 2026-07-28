"""
Deliberation: deep analysis from a cheap model.

The tests that matter here are the ones proving the DEPTH is structural. If
"consider the windows before the heat pump" only appears when a clever model
happens to think of it, the whole design has failed — it has to fall out of a
declared edge, and it has to be there on Mistral Small.

Everything except GATHER / CHALLENGE / RECOMMEND runs with no model at all,
which is why almost none of these tests need one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from rentium.rama import deliberation, interventions

pytestmark = pytest.mark.django_db


# A portfolio that should surface both envelope and mechanical options: an old
# house with real heating spend.
OLD_LEAKY_HOUSE = {
    "holding": {
        "name": "950 McKenzie Ave",
        "year_built": 1974,
        "heating_type": "gas furnace",
        "has_valuation": True,
        "days_to_renewal": 200,
    },
    "annual_spend": {"UTILITIES": 2400.0, "INSURANCE": 1800.0, "PROPERTY_TAX": 4200.0},
    "has_active_leases": True,
    "vacant_listings": 0,
}


# -------------------------------------------------- the slate is declared
def test_options_come_only_from_the_catalogue():
    """Nothing is invented. This is what a model cannot do here."""
    picked = interventions.candidates(OLD_LEAKY_HOUSE, topic="everything")
    assert picked
    assert all(item.key in interventions.CATALOGUE for item in picked)


def test_an_old_leaky_house_surfaces_both_windows_and_a_heat_pump():
    keys = {i.key for i in interventions.candidates(OLD_LEAKY_HOUSE, topic="energy_retrofit")}
    assert {"window_replacement", "heat_pump"} <= keys


def test_a_house_already_on_a_heat_pump_is_not_offered_one():
    pack = dict(OLD_LEAKY_HOUSE)
    pack["holding"] = dict(pack["holding"], heating_type="heat pump")
    keys = {i.key for i in interventions.candidates(pack, topic="energy_retrofit")}
    assert "heat_pump" not in keys


def test_a_new_house_is_not_offered_windows():
    pack = dict(OLD_LEAKY_HOUSE)
    pack["holding"] = dict(pack["holding"], year_built=2019)
    keys = {i.key for i in interventions.candidates(pack, topic="energy_retrofit")}
    assert "window_replacement" not in keys


def test_the_slate_is_deterministic():
    """Same pack, same slate, same order — which is what lets an eval assert
    that switching model changes the prose and nothing else."""
    a = [i.key for i in interventions.candidates(OLD_LEAKY_HOUSE)]
    b = [i.key for i in interventions.candidates(OLD_LEAKY_HOUSE)]
    assert a == b


def test_the_slate_is_bounded():
    picked = interventions.candidates(OLD_LEAKY_HOUSE, limit=deliberation.MAX_OPTIONS)
    assert len(picked) <= deliberation.MAX_OPTIONS


def test_a_broken_predicate_drops_one_option_not_the_run(monkeypatch):
    exploding = interventions.Intervention(
        key="boom", label="Boom", family="operating",
        applies=lambda p: 1 / 0,
    )
    monkeypatch.setitem(interventions.CATALOGUE, "boom", exploding)
    picked = interventions.candidates(OLD_LEAKY_HOUSE, topic="operating_cost")
    assert "boom" not in {i.key for i in picked}


def test_a_topic_keeps_the_run_on_subject():
    keys = {i.key for i in interventions.candidates(OLD_LEAKY_HOUSE, topic="financing")}
    assert keys <= {"mortgage_renewal"}


# -------------------------------------------- the insight is an edge, not luck
def test_windows_before_heat_pump_is_a_declared_edge():
    """The example from the brief. It must exist because it is in the data,
    not because a model was smart enough to say it."""
    edges = interventions.precedence_edges({"window_replacement", "heat_pump"})
    assert {"from": "window_replacement", "to": "heat_pump"} in [
        {"from": e["from"], "to": e["to"]} for e in edges
    ]
    assert edges[0]["why"]


def test_an_edge_needs_both_ends_on_the_slate():
    assert interventions.precedence_edges({"heat_pump"}) == []


# ------------------------------------------------------------ the contract
def test_a_well_formed_gather_reply_parses():
    slots = interventions.HEAT_PUMP.required_facts
    reply = (
        "FACT unit_cost 18000 CAD ONE_TIME src=WEB url=https://example.ca/x\n"
        "FACT annual_saving 940 CAD ANNUAL src=WEB\n"
        "MISSING rebate_available I need to know which rebates you already claimed\n"
    )
    parsed = deliberation.parse_gather(reply, slots)
    assert parsed.facts["unit_cost"]["value"] == "18000"
    assert parsed.facts["annual_saving"]["period"] == "ANNUAL"
    assert "rebate_available" in parsed.missing
    assert parsed.violations == []


def test_a_silently_skipped_required_slot_is_a_violation():
    """The failure that matters: the model answering about only part of what it
    was asked, and nothing noticing."""
    parsed = deliberation.parse_gather("FACT unit_cost 18000 CAD ONE_TIME src=WEB",
                                       interventions.HEAT_PUMP.required_facts)
    assert any("annual_saving" in v for v in parsed.violations)


def test_prose_instead_of_the_contract_is_a_violation():
    parsed = deliberation.parse_gather(
        "A heat pump would cost roughly $18,000 and save about $940 a year.",
        interventions.HEAT_PUMP.required_facts,
    )
    assert parsed.facts == {}
    assert parsed.violations


def test_an_invented_slot_is_rejected():
    parsed = deliberation.parse_gather(
        "FACT vibes 10 CAD ONE_TIME src=WEB", interventions.HEAT_PUMP.required_facts
    )
    assert any("vibes" in v for v in parsed.violations)


def test_the_contract_forbids_estimating_a_missing_number():
    assert "never estimate a number" in deliberation.GATHER_CONTRACT


# ------------------------------------------------------------- arithmetic
def test_payback_is_net_of_rebate():
    scores = deliberation.score_option(
        {
            "unit_cost": {"value": "18000", "source_type": "WEB"},
            "rebate_available": {"value": "5000", "source_type": "WEB"},
            "annual_saving": {"value": "1000", "source_type": "WEB"},
        }
    )
    assert scores["net_cost"] == "13000"
    assert scores["payback_years"] == "13.0"


def test_an_unscoreable_option_is_kept_not_dropped():
    """It still needs saying; it just cannot be compared."""
    scores = deliberation.score_option({"unit_cost": {"value": "18000"}})
    assert scores["net_cost"] == "18000"
    assert scores["payback_years"] is None


def test_a_spread_option_computes_its_own_saving():
    scores = deliberation.score_option(
        {
            "current_premium": {"value": "1800", "source_type": "LEDGER"},
            "market_premium": {"value": "1400", "source_type": "LANDLORD"},
        }
    )
    assert scores["annual_saving"] == "400"


def test_unscoreable_options_rank_last():
    ranked = deliberation.rank(
        [
            {"catalogue_key": "unknown", "scores": {"payback_years": None}},
            {"catalogue_key": "quick", "scores": {"payback_years": "2.0"}},
        ]
    )
    assert [r["catalogue_key"] for r in ranked] == ["quick", "unknown"]


# ------------------------------------------- self-questioning is a for-loop
def _option(key, cost, saving, source="ESTIMATE"):
    facts = {
        "unit_cost": {"value": cost, "source_type": source},
        "annual_saving": {"value": saving, "source_type": source},
    }
    return {"catalogue_key": key, "facts": facts, "scores": deliberation.score_option(facts)}


def test_a_close_call_produces_a_flip():
    """Two options within a whisker of each other, resting on estimates — the
    landlord should be told the ranking is fragile."""
    flips = deliberation.sensitivity(
        [_option("a", "10000", "1000"), _option("b", "11000", "1050")]
    )
    assert flips
    assert flips[0]["figure"] in ("unit_cost", "annual_saving")
    assert flips[0]["direction"] in ("higher", "lower")


def test_a_clear_winner_produces_no_flip():
    flips = deliberation.sensitivity(
        [_option("a", "1000", "5000"), _option("b", "90000", "100")]
    )
    assert flips == []


def test_a_ledger_figure_is_not_treated_as_an_assumption():
    """Sensitivity is about what we are unsure of. Swinging a number from the
    books would invent doubt that does not exist."""
    flips = deliberation.sensitivity(
        [
            _option("a", "10000", "1000", source="LEDGER"),
            _option("b", "11000", "1050", source="LEDGER"),
        ]
    )
    assert flips == []


def test_sensitivity_needs_something_to_compare():
    assert deliberation.sensitivity([_option("a", "10000", "1000")]) == []


# ---------------------------------------------------- figures and their tokens
def test_every_published_figure_is_a_computed_figure():
    options = [_option("heat_pump", "18000", "940")]
    table = deliberation.figures_for(options)
    assert table
    assert all(f.provenance.source_type == "ESTIMATE" for f in table.values())
    assert all(f.known for f in table.values())


def test_prose_can_only_reference_computed_figures():
    from rentium.rama.render import substitute

    table = deliberation.figures_for([_option("heat_pump", "18000", "940")])
    token = next(iter(table))
    text, violations = substitute(f"Payback is about {{{{{token}}}}}.", table)
    assert violations == []
    assert "$" in text or "years" in text

    _, bad = substitute("It costs {{f99}}.", table)
    assert bad


# --------------------------------------------------------------- the shape
def test_only_three_stages_cost_anything():
    assert deliberation.MODEL_STAGES == {"GATHER", "CHALLENGE", "RECOMMEND"}
    assert len(deliberation.STAGES) == 9


def test_the_budget_is_bounded():
    assert deliberation.MAX_MODEL_CALLS <= deliberation.MAX_OPTIONS + 4


def test_the_pack_is_built_without_a_model(landlord):
    """SCOPE is $0. If this ever needed a model, the cost model breaks."""
    from rentium.properties.models import PropertyHolding

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave", address="950 McKenzie Ave"
    )
    pack = deliberation.build_pack(landlord, holding=holding)
    assert "annual_spend" in pack
    assert pack["holding"]["name"] == "950 McKenzie Ave"
    assert "asserted_facts" in pack


def test_the_pack_carries_landlord_asserted_facts(landlord):
    """The correction loop feeds the analysis — that is the point of it."""
    from rentium.rama import treasurer_facts

    treasurer_facts.write(
        landlord,
        key="extra-rent",
        subject="extra rent",
        statement="We took $2,000/mo from an unrecorded tenant.",
        direction="NEUTRAL",
    )
    pack = deliberation.build_pack(landlord)
    assert pack["asserted_facts"]["usable"]


# ======================================================= the orchestrator
# Driven with an injected turn_runner so the STRUCTURE can be tested without a
# provider — which is the point: the sequence is the code, not the model.
from dataclasses import dataclass as _dc


@_dc
class _Reply:
    reply: str


def _runner(script):
    """A fake model. `script` maps an option label fragment -> reply text."""
    calls = []

    def run(landlord, message, conversation_id, **kwargs):
        calls.append({"message": message, "conversation": conversation_id, **kwargs})
        for fragment, text in script.items():
            if fragment.lower() in message.lower():
                return _Reply(reply=text)
        return _Reply(reply="")

    run.calls = calls
    return run


def _house(landlord):
    from rentium.ledger.models import EntryType, LedgerEntry
    from rentium.properties.models import PropertyHolding

    holding = PropertyHolding.objects.create(
        landlord=landlord, name="950 McKenzie Ave", address="950 McKenzie Ave"
    )
    from rentium.ledger.models import HoldingFinancials

    HoldingFinancials.objects.create(
        holding=holding, landlord=landlord, year_built=1974, heating_type="gas furnace"
    )
    import datetime

    LedgerEntry.objects.create(
        landlord=landlord, holding=holding, entry_type=EntryType.EXPENSE,
        amount="2400.00", effective_date=datetime.date.today(),
        category="UTILITIES", description="Heating",
    )
    return holding


def test_a_run_walks_every_stage_in_order(landlord, settings):
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"window": "FACT unit_cost 12000 CAD ONE_TIME src=LANDLORD\n"
                                "FACT annual_saving 600 CAD ANNUAL src=ESTIMATE\n"
                                "MISSING rebate_available need the rebate\n",
                      "heat pump": "FACT unit_cost 18000 CAD ONE_TIME src=LANDLORD\n"
                                   "FACT annual_saving 940 CAD ANNUAL src=ESTIMATE\n"
                                   "MISSING rebate_available need the rebate\n"})

    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )

    stages = [s.stage for s in row.stages.order_by("order")]
    assert stages[:3] == ["FRAME", "SCOPE", "ENUMERATE"]
    assert "GATHER" in stages
    assert stages[-2:] == ["SCORE", "COMPARE"]
    assert row.status == row.Status.DONE


def test_each_gather_is_its_own_bounded_sub_turn(landlord, settings):
    """The anti-collapse mechanism: separate conversations, so a weak model
    cannot merge two options into one answer."""
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "FACT unit_cost 1 CAD ONE_TIME src=LANDLORD\n"})

    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )

    gathers = row.stages.filter(stage="GATHER")
    conversations = [s.conversation_id for s in gathers]
    assert len(conversations) == len(set(conversations)) >= 2
    assert all(c is not None for c in conversations)


def test_one_option_cannot_see_another(landlord, settings):
    """During GATHER for the heat pump, windows are not in the prompt."""
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "FACT unit_cost 1 CAD ONE_TIME src=LANDLORD\n"})

    deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )

    for call in runner.calls:
        system = call.get("extra_system", "")
        assert not ("Heat pump" in system and "windows" in system.lower())


def test_the_ranking_is_recorded(landlord, settings):
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"window": "FACT unit_cost 12000 CAD ONE_TIME src=LANDLORD\n"
                                "FACT annual_saving 3000 CAD ANNUAL src=LANDLORD\n"
                                "MISSING rebate_available x\n",
                      "heat pump": "FACT unit_cost 18000 CAD ONE_TIME src=LANDLORD\n"
                                   "FACT annual_saving 900 CAD ANNUAL src=LANDLORD\n"
                                   "MISSING rebate_available x\n"})

    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )
    best = row.options.order_by("rank").first()
    assert best.catalogue_key == "window_replacement"  # 4-year payback beats 20
    assert best.rank == 1


def test_the_precedence_edge_survives_into_the_record(landlord, settings):
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "FACT unit_cost 10000 CAD ONE_TIME src=LANDLORD\n"
                          "FACT annual_saving 1000 CAD ANNUAL src=LANDLORD\n"
                          "MISSING rebate_available x\n"})

    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )
    compare = row.stages.get(stage="COMPARE")
    pairs = [(e["from"], e["to"]) for e in compare.output_artifact["precedence"]]
    assert ("window_replacement", "heat_pump") in pairs


def test_an_unverified_web_figure_is_excluded(landlord, settings):
    """The verbatim gate, end to end: a WEB figure absent from every cited
    page must not reach scoring."""
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "FACT unit_cost 77777 CAD ONE_TIME src=WEB\n"
                          "FACT annual_saving 940 CAD ANNUAL src=LANDLORD\n"
                          "MISSING rebate_available x\n"})

    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )
    option = row.options.first()
    assert "unit_cost" not in option.facts        # excluded
    assert "annual_saving" in option.facts        # kept
    gather = row.stages.filter(stage="GATHER").first()
    assert any("77777" in v for v in gather.violations)


def test_a_contract_violation_gets_one_repair_attempt(landlord, settings):
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "a heat pump costs about eighteen thousand dollars"})

    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )
    gather = row.stages.filter(stage="GATHER").first()
    assert gather.retries == deliberation.MAX_STAGE_RETRIES
    assert gather.violations
    assert gather.status == gather.Status.FAILED


def test_a_missing_slot_becomes_a_request_with_a_real_consequence(landlord, settings):
    from rentium.rama.models import TreasurerRequest

    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "FACT unit_cost 10000 CAD ONE_TIME src=LANDLORD\n"
                          "MISSING annual_saving how much would it save\n"
                          "MISSING rebate_available x\n"})

    deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )
    requests = TreasurerRequest.objects.filter(landlord=landlord)
    assert requests.exists()
    assert all(r.why_it_matters for r in requests)
    assert all(r.blocking is False for r in requests)  # never stall


def test_requests_are_capped_and_deduped(landlord, settings):
    from rentium.rama.models import TreasurerRequest

    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": ""})

    deliberation.run(landlord, topic="everything", holding=holding, turn_runner=runner)
    deliberation.run(landlord, topic="everything", holding=holding, turn_runner=runner)

    live = TreasurerRequest.objects.filter(
        landlord=landlord,
        status__in=(TreasurerRequest.Status.OPEN, TreasurerRequest.Status.RELAYED),
    )
    assert live.count() <= TreasurerRequest.MAX_OPEN_PER_LANDLORD
    keys = [r.dedupe_key for r in live]
    assert len(keys) == len(set(keys))


def test_the_call_budget_is_never_exceeded(landlord, settings):
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "nonsense that never parses"})  # forces every retry

    row = deliberation.run(
        landlord, topic="everything", holding=holding, turn_runner=runner
    )
    assert row.calls_used <= deliberation.MAX_MODEL_CALLS


def test_a_run_writes_nothing_to_the_domain(landlord, settings):
    from rentium.ledger.models import LedgerEntry
    from rentium.properties.models import Property

    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    before = (
        Property.objects.filter(landlord=landlord).count(),
        LedgerEntry.objects.filter(landlord=landlord).count(),
    )
    runner = _runner({"": "FACT unit_cost 1 CAD ONE_TIME src=LANDLORD\n"})

    deliberation.run(
        landlord, topic="everything", holding=holding, turn_runner=runner
    )

    assert (
        Property.objects.filter(landlord=landlord).count(),
        LedgerEntry.objects.filter(landlord=landlord).count(),
    ) == before


def test_the_whole_chain_is_reconstructible(landlord, settings):
    """"Why did it recommend that?" must be answerable from the record."""
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    runner = _runner({"": "FACT unit_cost 10000 CAD ONE_TIME src=LANDLORD\n"
                          "FACT annual_saving 1000 CAD ANNUAL src=LANDLORD\n"
                          "MISSING rebate_available x\n"})

    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=runner
    )
    for stage in row.stages.all():
        assert stage.output_artifact is not None
    assert row.stages.filter(stage="GATHER").first().raw_reply
    assert row.options.filter(scores__isnull=False).exists()


# ======================================================== the weekly beat
# One analysis per landlord per week, rotating topics. A background agent that
# produces something every morning trains people to stop reading it, so the
# interesting assertions here are the ones about NOT running.


@pytest.fixture
def quiet_beat(monkeypatch, settings):
    """The beat's job is selection and dedupe, not model output — so the
    default runner is stubbed rather than reaching a provider."""
    settings.RAMA_RESEARCH_BACKEND = "fake"
    from rentium.rama import service

    monkeypatch.setattr(service, "run_turn", _runner({}))


@pytest.fixture
def second_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def _enable(landlord):
    from rentium.rama.models import RamaPreferences

    RamaPreferences.objects.update_or_create(
        landlord=landlord, defaults={"enabled": True}
    )


def test_a_landlord_with_rama_off_is_skipped(landlord, quiet_beat):
    from rentium.rama.models import RamaDeliberation, RamaPreferences
    from rentium.rama.tasks import run_weekly_deliberation

    _house(landlord)
    RamaPreferences.objects.update_or_create(
        landlord=landlord, defaults={"enabled": False}
    )
    assert run_weekly_deliberation()["deliberations"] == 0
    assert RamaDeliberation.objects.count() == 0


def test_a_landlord_with_no_preferences_row_is_skipped(landlord, quiet_beat):
    """Never opt someone in by omission."""
    from rentium.rama.models import RamaPreferences
    from rentium.rama.tasks import run_weekly_deliberation

    _house(landlord)
    RamaPreferences.objects.filter(landlord=landlord).delete()
    assert run_weekly_deliberation()["deliberations"] == 0


def test_an_enabled_landlord_gets_one_deliberation(landlord, quiet_beat):
    from rentium.rama.models import RamaDeliberation
    from rentium.rama.tasks import run_weekly_deliberation

    _house(landlord)
    _enable(landlord)
    assert run_weekly_deliberation()["deliberations"] == 1
    row = RamaDeliberation.objects.get()
    assert row.trigger == "beat"
    assert row.dedupe_key


def test_the_same_week_does_not_run_twice(landlord, quiet_beat):
    """The beat can be replayed — a retry must not double-charge tokens."""
    from rentium.rama.models import RamaDeliberation
    from rentium.rama.tasks import run_weekly_deliberation

    _house(landlord)
    _enable(landlord)
    run_weekly_deliberation()
    assert run_weekly_deliberation()["deliberations"] == 0
    assert RamaDeliberation.objects.count() == 1


def test_the_dedupe_key_is_stamped_at_creation(landlord, quiet_beat):
    """Not afterwards: a run that takes minutes must not leave a window in
    which a second beat starts the same analysis again."""
    from rentium.rama.models import RamaDeliberation
    from rentium.rama.tasks import run_weekly_deliberation

    _house(landlord)
    _enable(landlord)
    seen = {}
    original = RamaDeliberation.objects.create

    def capture(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    RamaDeliberation.objects.create = capture
    try:
        run_weekly_deliberation()
    finally:
        del RamaDeliberation.objects.create
    assert seen.get("dedupe_key")


def test_the_topic_rotates_with_the_week():
    """A quiet week on energy still has to surface something on financing."""
    from rentium.rama.interventions import TOPIC_ROTATION

    assert len(TOPIC_ROTATION) > 1
    assert len(set(TOPIC_ROTATION)) == len(TOPIC_ROTATION)


def test_the_beat_is_registered():
    """Code nobody schedules is code that never runs."""
    from django.conf import settings as django_settings

    entry = django_settings.CELERY_BEAT_SCHEDULE["rama-treasurer-weekly"]
    assert entry["task"].endswith("run_weekly_deliberation")


def test_one_landlord_failing_does_not_stop_the_others(
    landlord, second_landlord, quiet_beat, monkeypatch
):
    from rentium.rama import deliberation, tasks

    _house(landlord)
    _house(second_landlord)
    _enable(landlord)
    _enable(second_landlord)

    seen = []
    real_run = deliberation.run

    def explode_once(who, **kwargs):
        seen.append(who)
        if len(seen) == 1:
            raise RuntimeError("provider down")
        return real_run(who, **kwargs)

    monkeypatch.setattr(deliberation, "run", explode_once)
    assert tasks.run_weekly_deliberation()["deliberations"] == 1
    assert len(seen) == 2


# ==================================================== the two CI gate tests
# These are the rows worth failing a build over: a finance agent that can write
# to the domain, or one whose advice changes when you change the model, is not
# a finance agent — it is a liability.

# The same facts, worded the way five different models might word them. The
# CONTRACT is line-oriented for exactly this reason: prose around it is noise.
DIALECTS = {
    "terse": {
        "window": "FACT unit_cost 12000 CAD ONE_TIME src=LANDLORD\n"
                  "FACT annual_saving 600 CAD ANNUAL src=ESTIMATE\n",
        "heat pump": "FACT unit_cost 18000 CAD ONE_TIME src=LANDLORD\n"
                     "FACT annual_saving 940 CAD ANNUAL src=ESTIMATE\n",
    },
    "chatty": {
        "window": "Sure! Here's what I found for the windows:\n"
                  "FACT unit_cost 12000 CAD ONE_TIME src=LANDLORD\n"
                  "FACT annual_saving 600 CAD ANNUAL src=ESTIMATE\n"
                  "Let me know if you'd like me to dig deeper.\n",
        "heat pump": "Happy to help — heat pump numbers below.\n\n"
                     "FACT unit_cost 18000 CAD ONE_TIME src=LANDLORD\n"
                     "FACT annual_saving 940 CAD ANNUAL src=ESTIMATE\n"
                     "Honestly I'd just do the windows first, personally!\n",
    },
    "fenced": {
        "window": "```\nFACT unit_cost 12000 CAD ONE_TIME src=LANDLORD\n"
                  "FACT annual_saving 600 CAD ANNUAL src=ESTIMATE\n```\n",
        "heat pump": "```\nFACT unit_cost 18000 CAD ONE_TIME src=LANDLORD\n"
                     "FACT annual_saving 940 CAD ANNUAL src=ESTIMATE\n```\n",
    },
}


def _ranking(landlord, holding, script):
    row = deliberation.run(
        landlord, topic="energy_retrofit", holding=holding, turn_runner=_runner(script)
    )
    return [
        (o.catalogue_key, o.rank, o.status)
        for o in row.options.order_by("rank", "catalogue_key")
    ]


@pytest.mark.parametrize("dialect", sorted(DIALECTS))
def test_the_ranking_does_not_depend_on_the_model(landlord, settings, dialect):
    """Provider-neutrality, the load-bearing claim behind "switch to Claude
    later without inconsistencies".

    The model only ever fills slots; enumeration, scoring and ranking are
    Python. So three very differently-worded replies carrying the SAME facts
    must produce byte-identical rankings. Prose may differ — advice may not.
    """
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)
    # Two runs over the SAME holding — a deliberation mutates nothing, so the
    # second sees exactly the portfolio the first did.
    baseline = _ranking(landlord, holding, DIALECTS["terse"])
    assert _ranking(landlord, holding, DIALECTS[dialect]) == baseline


def test_a_model_that_editorialises_cannot_change_the_order(landlord, settings):
    """The chatty dialect lobbies for the windows ("I'd just do the windows
    first, personally"). The arithmetic disagrees — $18,000/$940 pays back
    sooner than $12,000/$600 — and the arithmetic is what ranks.

    Note this is the RANK, not the advice: the windows-before-heat-pump
    precedence edge is reported separately by COMPARE, from the catalogue.
    """
    settings.RAMA_RESEARCH_BACKEND = "fake"
    ranking = _ranking(landlord, _house(landlord), DIALECTS["chatty"])
    scored = [key for key, rank, status in ranking if rank is not None]
    assert scored[0] == "heat_pump"


def test_a_deliberation_writes_nothing_to_the_domain(landlord, settings):
    """The Treasurer is read-only by construction — no tool it can reach takes
    a `confirm`, and it never touches plan_runner. This asserts the outcome
    rather than the mechanism: after a full run, nothing in the domain moved.
    """
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)

    from rentium.leases.models import Lease
    from rentium.ledger.models import LedgerEntry
    from rentium.maintenance.models import WorkOrder
    from rentium.properties.models import Property
    from rentium.rama.models import RamaAutoAction, RamaPendingPlan

    watched = (LedgerEntry, Property, Lease, WorkOrder, RamaPendingPlan,
               RamaAutoAction)
    before = {model: model.objects.count() for model in watched}

    deliberation.run(
        landlord, topic="energy_retrofit", holding=holding,
        turn_runner=_runner(DIALECTS["terse"]),
    )

    after = {model: model.objects.count() for model in watched}
    assert after == before


def test_a_model_asking_for_a_write_gets_nowhere(landlord, settings):
    """A weak model WILL sometimes emit something that looks like a tool call.
    It must land as unparsed prose, not as an action."""
    settings.RAMA_RESEARCH_BACKEND = "fake"
    holding = _house(landlord)

    from rentium.ledger.models import LedgerEntry
    from rentium.rama.models import RamaPendingPlan

    before = (LedgerEntry.objects.count(), RamaPendingPlan.objects.count())
    deliberation.run(
        landlord, topic="energy_retrofit", holding=holding,
        turn_runner=_runner({
            "": 'FACT unit_cost 1 CAD ONE_TIME src=LANDLORD\n'
                'create_expense(amount="18000", confirm="yes")\n'
                '{"tool": "post_charge", "amount": 2000, "confirm": "yes"}\n',
        }),
    )
    assert (LedgerEntry.objects.count(), RamaPendingPlan.objects.count()) == before


def test_the_treasurer_has_no_tool_that_takes_a_confirm():
    """The mechanism behind the two tests above, asserted at import time:
    with no confirm parameter anywhere on its surface, pending_specs is
    provably always empty and no plan can originate from this role."""
    from rentium.rama import registry
    from rentium.rama.roles import TREASURER_TOOLS

    offenders = [
        name
        for name in TREASURER_TOOLS
        if "confirm" in registry.REGISTRY[name].parameters["properties"]
    ]
    assert offenders == []
