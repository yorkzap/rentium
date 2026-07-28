"""
Web research: allowlisted, scrubbed, and verified.

Three guards, and each has a test that would fail loudly if it were removed:

- the model names a TOPIC, never a query or a domain;
- a query carrying tenant details never leaves the system;
- a figure that does not appear verbatim in the page it cites is not usable.

The third is the one that matters most. A confidently wrong rebate amount
reaching a landlord who is about to spend $18,000 is the worst thing this
feature could do.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from rentium.rama import research
from rentium.rama.models import TreasurerSource

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_backend(settings):
    settings.RAMA_RESEARCH_BACKEND = "fake"
    return settings


# ------------------------------------------------------------- the allowlist
def test_an_unknown_topic_is_refused(landlord, fake_backend):
    """The model may only name a key that exists here — it cannot phrase a
    query of its own."""
    assert research.search(landlord, "whatever_i_feel_like") == []


def test_a_known_topic_returns_sources(landlord, fake_backend):
    sources = research.search(landlord, "bc_heat_pump_rebate")
    assert sources
    assert sources[0].domain.endswith("betterhomesbc.ca")
    assert sources[0].topic == "bc_heat_pump_rebate"


def test_a_result_outside_the_allowlist_is_dropped(landlord, fake_backend, monkeypatch):
    """Enforced on the RESULT, not just the query — a provider that ignores a
    site: filter must not smuggle a source in."""
    monkeypatch.setattr(
        research,
        "_fake_results",
        lambda topic: [
            {
                "url": "https://random-blog.example.com/rebates",
                "title": "Some blog",
                "text": "Up to $5,000",
                "status": 200,
            }
        ],
    )
    assert research.search(landlord, "bc_heat_pump_rebate") == []


def test_every_topic_declares_a_query_and_domains():
    for topic, spec in research.RESEARCH_TOPICS.items():
        assert spec["query"], f"{topic} has no query"
        assert spec["domains"], f"{topic} has no domain allowlist"


def test_unconfigured_is_a_safe_no_op(landlord, settings):
    """An unwired provider means no research, never an error."""
    settings.RAMA_RESEARCH_BACKEND = "none"
    assert research.search(landlord, "bc_heat_pump_rebate") == []


# ------------------------------------------------------------------- scrub
@pytest.mark.parametrize(
    "query",
    [
        "heat pump rebate for sarah@example.com",
        "rebates near 250-555-0100",
        "heat pump rebate V8N 1B2",
        "insulation cost for 950 McKenzie Ave",
    ],
)
def test_pii_never_leaves_the_system(landlord, query):
    assert research.scrub(landlord, query) is not None


def test_a_tenant_name_is_refused(landlord, bc_lease):
    from rentium.leases.models import LeaseTenant

    LeaseTenant.objects.create(
        lease=bc_lease,
        rent_amount="850.00",
        invited_name="Sarah Novak",
        invited_email="s@example.com",
    )
    assert research.scrub(landlord, "rebate advice for Sarah Novak") is not None


def test_an_ordinary_query_passes(landlord):
    assert research.scrub(landlord, "BC heat pump rebate amount residential") is None


def test_a_scrub_failure_closes_the_gate_rather_than_opening_it(landlord, monkeypatch):
    """If we cannot prove the query is clean, we do not send it."""
    import rentium.leases.models as lease_models

    class Exploding:
        def filter(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(lease_models.LeaseTenant, "objects", Exploding())
    assert research.scrub(landlord, "perfectly ordinary query") is not None


# ------------------------------------------------------- the verbatim gate
def _source(landlord, text):
    return TreasurerSource.objects.create(
        landlord=landlord,
        topic="bc_heat_pump_rebate",
        url="https://betterhomesbc.ca/x",
        domain="betterhomesbc.ca",
        excerpt=text,
        fetched_at=timezone.now(),
        expires_at=timezone.now() + timedelta(days=30),
    )


def test_a_figure_present_in_the_page_verifies(landlord):
    source = _source(landlord, "may receive up to $5,000 toward a heat pump")
    assert research.verify_in_source(Decimal("5000"), source) is True


def test_a_figure_absent_from_the_page_does_not_verify(landlord):
    """The invented-rebate case. The number is plausible and the citation is
    real; it just is not in the text."""
    source = _source(landlord, "Rebates vary by program and region.")
    assert research.verify_in_source(Decimal("5000"), source) is False


def test_verification_is_not_fooled_by_formatting(landlord):
    source = _source(landlord, "up to $ 9,500.00 for income qualified households")
    assert research.verify_in_source(Decimal("9500"), source) is True


def test_a_source_with_no_text_verifies_nothing(landlord):
    assert research.verify_in_source(Decimal("5000"), _source(landlord, "")) is False
    assert research.verify_in_source(Decimal("5000"), None) is False


def test_the_unverifiable_fixture_really_is_unverifiable(landlord):
    """Guards the eval that depends on this fixture from silently passing: its
    page must genuinely NOT contain the figure the eval expects to be rejected.

    Built directly from the fixture text rather than through search(), because
    a test-only topic has no business being in the production allowlist — and
    search() correctly refuses it.
    """
    from rentium.rama.research_fixtures import FIXTURES

    page = FIXTURES["unverifiable_topic"][0]["text"]
    assert research.verify_in_source(Decimal("5000"), _source(landlord, page)) is False
    # ...while the real fixture for the same topic does contain it, so the eval
    # is testing the gate rather than a typo.
    real = FIXTURES["bc_heat_pump_rebate"][0]["text"]
    assert research.verify_in_source(Decimal("5000"), _source(landlord, real)) is True


# ------------------------------------------------------------ caching / TTL
def test_a_fresh_source_is_reused_rather_than_refetched(landlord, fake_backend, monkeypatch):
    """A re-run of an analysis must not drift because a page changed."""
    first = research.search(landlord, "bc_heat_pump_rebate")
    calls = []
    monkeypatch.setattr(
        research, "_fake_results", lambda topic: calls.append(topic) or []
    )
    second = research.search(landlord, "bc_heat_pump_rebate")
    assert [s.pk for s in second] == [s.pk for s in first]
    assert calls == []


def test_an_expired_source_is_not_reused(landlord, fake_backend):
    sources = research.search(landlord, "bc_heat_pump_rebate")
    TreasurerSource.objects.filter(pk__in=[s.pk for s in sources]).update(
        expires_at=timezone.now() - timedelta(days=1)
    )
    assert research.expire_stale(landlord) >= 1
    stale = TreasurerSource.objects.get(pk=sources[0].pk)
    assert stale.status == TreasurerSource.Status.STALE
    assert stale.is_fresh is False


def test_rates_expire_sooner_than_programs():
    assert (
        research.RESEARCH_TOPICS["bc_mortgage_rates"]["ttl_days"]
        < research.RESEARCH_TOPICS["bc_heat_pump_rebate"]["ttl_days"]
    )


def test_sources_are_landlord_scoped(landlord, other_landlord, fake_backend):
    research.search(landlord, "bc_heat_pump_rebate")
    assert TreasurerSource.objects.filter(landlord=other_landlord).count() == 0


# ---------------------------------------------------------------- the seam
def test_the_provider_call_is_isolated_to_two_functions():
    """The whole point of mirroring comms/whatsapp.py: swapping Firecrawl for
    Tavily must be a two-function change."""
    import inspect

    body = inspect.getsource(research)
    assert body.count("api.firecrawl.dev") == 2
    for name in ("search", "scrub", "verify_in_source", "expire_stale"):
        assert "firecrawl" not in inspect.getsource(getattr(research, name)).lower()


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())
