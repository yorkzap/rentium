"""
The fields a real tenancy agreement needs that the Lease model didn't have.

Lease was built to run a tenancy (money, status, signatures). It was never
built to BE the agreement — which is why build_lease_pdf() had to write a
disclaimer admitting it was "a summary of the lease terms on file" rather than
a document anyone had actually agreed to.

These are the facts an RTB-1-shaped agreement states and the old model simply
did not record: which day rent falls due, who else lives there, what's included
in the rent (a different question from who pays which BILL), when the deposit
was actually received, what the pet/smoking terms are.

They live in an abstract mixin rather than inline in models.py because they
exist for exactly one consumer — leases/documents.py — and grouping them with
the document layer says so. Django flattens abstract bases at migration time,
so the columns land on leases_lease as if they'd been written inline.

Wire-up in leases/models.py:
    from .agreement import AgreementTerms
    class Lease(AgreementTerms):   # was: class Lease(models.Model)
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class ServiceOrFacility(models.TextChoices):
    """
    What comes WITH the place (RTB-1 §5). Deliberately NOT the same thing as
    Lease.bills_included, which is a BILLING configuration: who pays what
    percentage of the hydro invoice, how it's split between roommates, which
    provider it's with. That's an accounting concern and it drives the ledger.

    This is a contractual one: "does this rental include heat, yes or no." A
    tenant reading their agreement needs the second question answered plainly,
    and it's the answer that binds the landlord — you cannot stop supplying
    heat mid-tenancy because it's a term of the agreement, regardless of how
    the bill is split.
    """

    HEAT = "HEAT", _("Heat")
    ELECTRICITY = "ELECTRICITY", _("Electricity")
    WATER = "WATER", _("Water")
    NATURAL_GAS = "NATURAL_GAS", _("Natural gas")
    HOT_WATER = "HOT_WATER", _("Hot water")
    INTERNET = "INTERNET", _("Internet")
    CABLE = "CABLE", _("Cablevision")
    LAUNDRY_FREE = "LAUNDRY_FREE", _("Laundry (free)")
    LAUNDRY_COIN = "LAUNDRY_COIN", _("Laundry (coin-operated)")
    REFRIGERATOR = "REFRIGERATOR", _("Refrigerator")
    STOVE = "STOVE", _("Stove and oven")
    DISHWASHER = "DISHWASHER", _("Dishwasher")
    WINDOW_COVERINGS = "WINDOW_COVERINGS", _("Window coverings")
    CARPETS = "CARPETS", _("Carpets")
    FURNITURE = "FURNITURE", _("Furniture")
    STORAGE = "STORAGE", _("Storage")
    GARBAGE = "GARBAGE", _("Garbage collection")
    RECYCLING = "RECYCLING", _("Recycling")
    SNOW_REMOVAL = "SNOW_REMOVAL", _("Snow removal")
    LAWN = "LAWN", _("Lawn / garden maintenance")


class AgreementTerms(models.Model):
    """Abstract. Mixed into Lease."""

    class Meta:
        abstract = True

    # --- Rent mechanics -----------------------------------------------------
    rent_due_day = models.PositiveSmallIntegerField(
        _("Rent Due On (day of month)"),
        default=1,
        help_text=_(
            "The day rent is payable each period. The ledger currently generates "
            "charges on the 1st (see ledger/billing.py); this states the agreed "
            "day on the document, and is what the billing engine should read once "
            "non-1st due dates are supported."
        ),
    )

    # --- What's included ----------------------------------------------------
    services_and_facilities = models.JSONField(
        _("Services and Facilities Included in Rent"),
        default=list,
        blank=True,
        help_text=_(
            "Subset of ServiceOrFacility. A CONTRACTUAL statement of what the "
            "rent buys — distinct from bills_included, which is the billing "
            "split. See ServiceOrFacility's docstring."
        ),
    )

    parking_included = models.BooleanField(_("Parking Included"), default=False)
    parking_description = models.CharField(
        _("Parking Details"),
        max_length=200,
        blank=True,
        help_text=_("e.g. 'One uncovered stall, driveway, left side'"),
    )
    parking_extra_charge = models.DecimalField(
        _("Parking Charge (if not included)"),
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
    )

    # --- Who lives there ----------------------------------------------------
    occupants = models.JSONField(
        _("Other Occupants"),
        default=list,
        blank=True,
        help_text=_(
            "Names of people who will live in the unit but are NOT tenants on "
            "this agreement — children, a partner who isn't signing. They have "
            "no tenancy rights and no rent obligation; naming them is what stops "
            "their presence being an unauthorised occupant later."
        ),
    )

    # --- Deposits: when the money actually arrived ---------------------------
    # Deposit AMOUNTS already live on Lease. These are the DATES, which the
    # agreement has to state and which start the statutory clock for returning
    # them at the end of the tenancy.
    security_deposit_received_date = models.DateField(
        _("Security Deposit Received On"), null=True, blank=True
    )
    pet_deposit_received_date = models.DateField(
        _("Pet Damage Deposit Received On"), null=True, blank=True
    )

    # --- Conduct terms ------------------------------------------------------
    # pets_allowed / smoking_allowed already exist as booleans on Lease. These
    # are the qualifications: "one cat under 15lb", "not on the balcony".
    pets_terms = models.TextField(
        _("Pet Terms"),
        blank=True,
        help_text=_("Conditions if pets are allowed — number, size, type, where."),
    )
    smoking_terms = models.TextField(
        _("Smoking Terms"),
        blank=True,
        help_text=_("Conditions or restrictions on smoking, if any."),
    )

    # --- Roommate agreements only -------------------------------------------
    house_rules = models.TextField(
        _("House Rules"),
        blank=True,
        help_text=_(
            "Shared-living ground rules: guests, quiet hours, cleaning rota, "
            "kitchen etiquette. Rendered as its own section of the roommate "
            "agreement. Not used for complete-unit tenancies."
        ),
    )

    # --- Addendum -----------------------------------------------------------
    # `special_terms` (already on Lease) is the free-text additional-terms
    # clause and is rendered in every format. This is deliberately NOT a second
    # field — one place for extra terms, so nothing gets signed twice or, worse,
    # signed in one document and missing from the other.

    def clean(self):
        super().clean()

        if not (1 <= int(self.rent_due_day or 1) <= 28):
            # 29/30/31 would silently skip February. Force a real answer.
            raise ValidationError(
                {
                    "rent_due_day": _(
                        "Pick a day from 1 to 28 — a later day doesn't exist in "
                        "every month, and rent that 'falls due on the 31st' has no "
                        "due date in February."
                    )
                }
            )

        if self.services_and_facilities:
            valid = {c.value for c in ServiceOrFacility}
            invalid = set(self.services_and_facilities) - valid
            if invalid:
                raise ValidationError(
                    {"services_and_facilities": _(f"Unknown services: {invalid}")}
                )

        if self.occupants and not isinstance(self.occupants, list):
            raise ValidationError({"occupants": _("Must be a list of names.")})

        if self.pets_terms and not self.pets_allowed:
            raise ValidationError(
                {
                    "pets_terms": _(
                        "Pets aren't allowed on this lease, so pet terms would "
                        "contradict the agreement. Allow pets, or clear this."
                    )
                }
            )

        if self.house_rules and "ROOMMATE" not in (self.lease_type or ""):
            raise ValidationError(
                {
                    "house_rules": _(
                        "House rules apply to roommate agreements. For a complete "
                        "unit, use Special Terms."
                    )
                }
            )
