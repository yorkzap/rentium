"""
"Siya's cleaning and security deposits were received."

That sentence names a person, two charges, and an event. Every figure it
implies is already on the books: which lease she is on, what the two deposits
are, how much is still owing on each. RAMA answered:

    "I need the exact amounts from the lease to record the payments. What are
     the cleaning deposit and security deposit amounts for Siya's lease?"

and when pushed, offered to record $400 of *income* as a Treasurer fact against
a lease number it had quoted correctly all along — which would have left both
deposit charges unpaid, the deposits-held figure at zero, and $400 double
counted the day the real payment was entered.

Three separate holes, each of which alone was enough to cause it:

  * charges could not be found by tenant. `_open_charges` matched description
    and property only, and no deposit description contains a tenant's name.
  * `amount` was required. "The deposits were received" states the event and
    lets the books state the figure, which is the normal way a landlord says
    this — and the only reason RAMA had to ask.
  * a Treasurer fact would happily restate money that an open CHARGE was
    waiting for. The fact store is for what the ledger CANNOT hold.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from rentium.ledger.models import EntryType, LedgerEntry
from rentium.rama import registry

pytestmark = pytest.mark.django_db

TODAY = datetime.date.today()


# --------------------------------------------------------------- the world
@pytest.fixture
def siya(landlord, bc_lease):
    """A tenant on the lease, by name, the way the landlord refers to her."""
    from rentium.leases.models import LeaseTenant
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    person = TenantProfile.objects.create(user=UserFactory(name="Siya Gulati"))
    LeaseTenant.objects.create(
        lease=bc_lease,
        tenant=person,
        rent_amount="850.00",
        invited_name="Siya Gulati",
        is_primary_tenant=True,
    )
    return person


def _deposit(landlord, lease, *, description, kind, amount, tenant=None):
    from rentium.ledger import services as ledger_services

    charge, _ = ledger_services.post_charge(
        landlord=landlord,
        property=lease.property,
        lease=lease,
        tenant=tenant,
        entry_type=EntryType.DEPOSIT_CHARGE,
        amount=Decimal(amount),
        due_date=TODAY - datetime.timedelta(days=20),
        description=description,
        metadata={"kind": kind},
    )
    return charge


@pytest.fixture
def her_deposits(landlord, bc_lease, siya):
    """$250 security + $150 cleaning, both outstanding, charged at signing."""
    return [
        _deposit(
            landlord,
            bc_lease,
            description="Security deposit — due on signing",
            kind="security_deposit",
            amount="250.00",
        ),
        _deposit(
            landlord,
            bc_lease,
            description="Cleaning deposit — due on signing",
            kind="cleaning_deposit_lease",
            amount="150.00",
        ),
    ]


def _pay(landlord, **kwargs):
    return registry.execute("record_payment", kwargs, landlord=landlord)


# ================================================== the transcript, replayed
def test_the_deposits_were_received_needs_no_amount_from_the_landlord(
    landlord, her_deposits,
):
    """The whole bug in one call. The landlord states the event; the books
    state the figures. Asking for them is asking the landlord to read out data
    RAMA is already holding."""
    result = _pay(
        landlord,
        charge_query="Siya cleaning and security deposits",
        payment_method="etransfer",
    )

    assert "question_for_user" not in result, result
    assert result.get("needs_confirm") is True, result
    rows = result["preview"]["allocations"]
    assert {row["payment"] for row in rows} == {"250.00", "150.00"}
    assert result["preview"]["total"] == "400.00"


def test_the_derived_amount_says_where_it_came_from(landlord, her_deposits):
    """A number RAMA supplied rather than the landlord must be labelled as
    such, or the preview reads as though they had stated $400 themselves."""
    preview = _pay(
        landlord,
        charge_query="Siya cleaning and security deposits",
        payment_method="etransfer",
    )["preview"]
    assert "outstanding" in preview["amount_source"].casefold()
    assert "did not state" in preview["amount_source"].casefold()


def test_confirming_settles_both_deposits(landlord, her_deposits):
    result = _pay(
        landlord,
        charge_query="Siya cleaning and security deposits",
        payment_method="etransfer",
        confirm="yes",
    )
    assert result.get("ok"), result
    for charge in her_deposits:
        charge.refresh_from_db()
        assert charge.charge_status() == "PAID"


def test_the_money_then_shows_as_held(landlord, her_deposits):
    """"Deposits held $0.00" was the symptom the landlord actually saw."""
    from rentium.ledger import services as ledger_services

    _pay(
        landlord,
        charge_query="Siya cleaning and security deposits",
        payment_method="etransfer",
        confirm="yes",
    )
    assert ledger_services.deposits_held(landlord) == Decimal("400.00")


# ================================================ finding charges by person
def test_a_tenant_name_scopes_the_charges(landlord, bc_lease, siya, her_deposits):
    """Two tenants, two sets of deposits, one name in the sentence. The name
    has to narrow it — no deposit description has ever contained one."""
    from rentium.leases.models import Lease, LeaseTenant
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    other_lease = Lease.objects.create(
        landlord=landlord,
        property=bc_lease.property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=TODAY,
        is_month_to_month=True,
        total_rent="900.00",
    )
    other = TenantProfile.objects.create(user=UserFactory(name="Marcus Webb"))
    LeaseTenant.objects.create(
        lease=other_lease,
        tenant=other,
        rent_amount="900.00",
        invited_name="Marcus Webb",
        is_primary_tenant=True,
    )
    _deposit(
        landlord,
        other_lease,
        description="Security deposit — due on signing",
        kind="security_deposit",
        amount="450.00",
    )

    result = _pay(
        landlord,
        charge_query="deposits",
        tenant_query="Siya",
        payment_method="etransfer",
    )
    assert "question_for_user" not in result, result
    assert "preview" in result, result
    assert result["preview"]["total"] == "400.00", result


def test_the_name_is_read_out_of_the_charge_query_too(
    landlord, bc_lease, siya, her_deposits,
):
    """Models put the whole sentence in charge_query. "Siya" is a person in
    this portfolio, so it scopes rather than being matched against wording no
    charge description will ever have."""
    from rentium.leases.models import Lease, LeaseTenant
    from rentium.users.models import TenantProfile
    from rentium.users.tests.factories import UserFactory

    other_lease = Lease.objects.create(
        landlord=landlord,
        property=bc_lease.property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=TODAY,
        is_month_to_month=True,
        total_rent="900.00",
    )
    other = TenantProfile.objects.create(user=UserFactory(name="Marcus Webb"))
    LeaseTenant.objects.create(
        lease=other_lease,
        tenant=other,
        rent_amount="900.00",
        invited_name="Marcus Webb",
        is_primary_tenant=True,
    )
    _deposit(
        landlord,
        other_lease,
        description="Security deposit — due on signing",
        kind="security_deposit",
        amount="450.00",
    )

    result = _pay(
        landlord,
        charge_query="Siya's cleaning and security deposits were received",
        payment_method="etransfer",
    )
    assert result["preview"]["total"] == "400.00"


def test_a_stated_amount_still_wins(landlord, her_deposits):
    """Deriving is a fallback for silence, never an override. $100 against the
    security deposit stays $100."""
    preview = _pay(
        landlord,
        charge_query="Siya security deposit",
        amount="100.00",
        payment_method="etransfer",
    )["preview"]
    assert preview["this_payment"] == "100.00"
    assert preview["still_owing_after"] == "150.00"


# ================================================== what it will NOT derive
def test_an_unscoped_payment_with_no_amount_asks(landlord, her_deposits):
    """"A payment came in" says nothing about which charge or how much.
    Settling every open charge in the portfolio is not a reading of that."""
    result = _pay(landlord, payment_method="etransfer")
    assert "question_for_user" in result
    assert not LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).exists()


def test_a_derived_amount_never_spans_two_leases(landlord, bc_lease, siya):
    """One sentence about money is one payment. If the match crosses leases,
    the scope was too loose to turn into a figure."""
    from rentium.leases.models import Lease

    _deposit(
        landlord,
        bc_lease,
        description="Security deposit — due on signing",
        kind="security_deposit",
        amount="250.00",
    )
    second = Lease.objects.create(
        landlord=landlord,
        property=bc_lease.property,
        lease_type=Lease.LeaseType.BC_RESIDENTIAL_TENANCY,
        status=Lease.LeaseStatus.ACTIVE,
        start_date=TODAY,
        is_month_to_month=True,
        total_rent="900.00",
    )
    _deposit(
        landlord,
        second,
        description="Security deposit — due on signing",
        kind="security_deposit",
        amount="450.00",
    )

    result = _pay(landlord, charge_query="deposit", payment_method="etransfer")
    assert "question_for_user" in result
    assert not LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).exists()


def test_deriving_an_amount_does_not_skip_the_confirmation(landlord, her_deposits):
    """RAMA supplying the figure makes the preview MORE necessary, not less."""
    _pay(
        landlord,
        charge_query="Siya cleaning and security deposits",
        payment_method="etransfer",
    )
    assert not LedgerEntry.objects.filter(entry_type=EntryType.PAYMENT).exists()


def test_the_payment_method_is_still_asked_for(landlord, her_deposits):
    """The books hold the amounts. They do not hold how the money arrived, and
    that one is a real question — not a figure RAMA is making the landlord
    look up for it."""
    result = _pay(landlord, charge_query="Siya cleaning and security deposits")
    assert result["needs"] == "payment_method"


# ============================================ the Treasurer fact it fell to
#
# Checked through `already_done_for`, which is where the refusal actually runs:
# service._refuse_if_already_done consults it before any preview reaches the
# landlord, and plan_runner.validate_plan again between preview and confirm.
def _fact_refusal(landlord, **kwargs) -> str | None:
    from rentium.rama.tool_meta import already_done_for

    return already_done_for("record_treasurer_fact", landlord, **kwargs)


def test_a_deposit_payment_is_not_a_treasurer_fact(landlord, her_deposits):
    """What RAMA actually offered: "$400.00 income for Siya's cleaning and
    security deposits". Wrong store, wrong sign, wrong effect — the charges
    stay unpaid and nothing outside a Monday deliberation reads the fact."""
    refusal = _fact_refusal(
        landlord,
        subject="Siya deposits received",
        fact="Received $400.00 for Siya Gulati's cleaning and security deposits",
        amount="400.00",
        direction="INCOME",
    )
    assert refusal, "a $400 fact against $400 of open deposit charges stood"
    assert "record_payment" in refusal


def test_the_refusal_names_the_charges_that_are_waiting(landlord, her_deposits):
    refusal = _fact_refusal(
        landlord,
        subject="Siya deposits received",
        fact="Received $400 for the deposits",
        amount="400.00",
    )
    assert "Security deposit" in refusal
    assert "Cleaning deposit" in refusal


def test_the_preview_never_reaches_the_landlord(landlord, her_deposits):
    """End to end through the seam the turn actually uses: the proposal is
    replaced by the refusal, so there is nothing to say yes to."""
    from rentium.rama.service import _refuse_if_already_done

    arguments = {
        "subject": "Siya deposits received",
        "fact": "Received $400.00 for the cleaning and security deposits",
        "amount": "400.00",
    }
    proposal = registry.execute("record_treasurer_fact", arguments, landlord=landlord)
    assert proposal["needs_confirm"] is True  # the tool itself still previews

    refused = _refuse_if_already_done(
        "record_treasurer_fact", arguments, proposal, landlord,
    )
    assert refused["already_done"] is True
    assert "record_payment" in refused["error"]


def test_a_genuine_gap_fact_is_still_recordable(landlord, bc_lease):
    """The guard must not swallow the case the fact store exists for: money
    the ledger has no charge for at all."""
    refusal = _fact_refusal(
        landlord,
        subject="Off-book rent 2025",
        fact="We took $2,000 rent from another tenant during 2025 that was "
        "never entered",
        amount="2000.00",
    )
    assert refusal is None

    result = registry.execute(
        "record_treasurer_fact",
        {
            "subject": "Off-book rent 2025",
            "fact": "We took $2,000 rent from another tenant during 2025 that "
            "was never entered",
            "amount": "2000.00",
        },
        landlord=landlord,
    )
    assert result.get("needs_confirm") is True, result


def test_a_partial_match_is_not_read_as_the_whole_transfer(landlord, her_deposits):
    """$250 + $150 are open. A fact asserting $900 matches neither one nor any
    combination of them, and must not be refused on their account."""
    assert _fact_refusal(
        landlord,
        subject="Insurance rebate",
        fact="Received a $900 insurance rebate that never hit the books",
        amount="900.00",
    ) is None


# ================================== the class: asking for its own data
#
# The tools above close this one instance. The guard below is what catches the
# next one — any turn that ends by asking the landlord to read out something
# RAMA has read access to gets one round back through the model with the tools
# that hold the answer, before the question can reach a person.
@pytest.mark.parametrize(
    "reply",
    [
        # Verbatim from the transcript.
        "I need the exact amounts from the lease to record the payments. What "
        "are the cleaning deposit and security deposit amounts for Siya's lease?",
        "What is the monthly rent on that lease?",
        "Could you tell me the outstanding balance on her account?",
        "How much is the security deposit?",
        "Can you confirm the lease number for that tenancy?",
        "Please tell me the amounts for the two deposits.",
    ],
)
def test_a_question_the_database_answers_is_caught(reply):
    from rentium.rama.service import asks_for_what_the_books_hold

    assert asks_for_what_the_books_hold(reply) is True


@pytest.mark.parametrize(
    "reply",
    [
        # Nothing on record says how money physically arrived. Refusing to ask
        # this would push RAMA into guessing a payment method onto a financial
        # record — a worse failure than one more question.
        "How did the $400 come in — e-transfer, cash, or cheque?",
        "What day did the money land?",
        "Did she pay the whole amount, or part of it?",
        # Confirmations are not lookups.
        "I'll record $250 against the security deposit and $150 against the "
        "cleaning deposit. Shall I go ahead?",
        "Reply yes and I'll post both deposit payments.",
        # Answers, not questions.
        "Her security deposit is $250 and the cleaning deposit is $150, both "
        "still outstanding.",
        "The deposits total $400 and neither has been paid.",
    ],
)
def test_a_real_question_still_gets_asked(reply):
    from rentium.rama.service import asks_for_what_the_books_hold

    assert asks_for_what_the_books_hold(reply) is False


def test_the_instruction_names_where_to_look():
    """"Be more helpful" does not survive a weak model. The nudge has to name
    the tools, or it produces the same question with an apology on it."""
    from rentium.rama.service import _LOOK_IT_UP

    for tool in ("tenant_statement", "charge_schedule", "record_payment"):
        assert tool in _LOOK_IT_UP
    # The specific escape hatch for this transcript: the money-in tool no
    # longer needs the figure it was asking for.
    assert "WITHOUT an amount" in _LOOK_IT_UP


def test_both_personas_carry_the_rule():
    """The guard is the net; the persona is what stops the model reaching it.
    A rule that only exists as a post-hoc check costs a whole extra round trip
    every time it fires — and it has to be on BOTH prompts, because the General
    fronted this conversation and the Corporal holds record_payment."""
    from rentium.rama.roles import CORPORAL_PROMPT, GENERAL_PROMPT

    for prompt in (CORPORAL_PROMPT, GENERAL_PROMPT):
        flat = " ".join(prompt.split())
        assert "NEVER ASK THE LANDLORD FOR SOMETHING IN THEIR OWN RECORDS" in flat
        assert "record_payment takes NO amount" in flat
        assert "MONEY RECEIVED IS A LEDGER PAYMENT, NEVER A TREASURER FACT" in flat


def test_the_general_cannot_invent_a_treasurer_request():
    """It relayed "Treasurer request: Record $400.00 income ..." with no
    TreasurerRequest anywhere in the database. That prefix means the finance
    head asked; on its own proposal it invents a colleague's instruction."""
    from rentium.rama.roles import GENERAL_PROMPT

    flat = " ".join(GENERAL_PROMPT.split())
    assert 'NEVER write "Treasurer request:" unless' in flat
