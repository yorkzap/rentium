"""
The ledger: single source of truth for every dollar in Rentium.

Rules (enforced in code, not convention):

1. APPEND-ONLY. A posted entry's financial fields can never change and an
   entry can never be deleted. Corrections are a REVERSAL entry (void)
   plus, if needed, a fresh entry. The audit trail is therefore complete
   by construction — for landlords, for the RTB, and for the AI later.

   ONE EXCEPTION, whitelisted and enforced by save(): `paid_on` on an
   EXPENSE. See that field's comment for why it is not a violation.

2. STATUS IS COMPUTED, NEVER STORED. "Paid / partial / overdue" for a
   charge is derived from (charge amount − non-voided settlements) and
   the due date. There is no status column to drift out of sync.

3. IDEMPOTENT WRITES. Every entry can carry a unique idempotency_key.
   Generated charges use natural keys (rent:<lease>:<tenant>:<due_date>,
   rent:<lease>:joint:<due_date> for household charges), API-recorded
   payments use a client-generated key — so a double-click, a network
   retry, or a re-run Celery task can never double-post.

4. ONE LEDGER PER SCOPE. `property` is the partition key (NULL =
   portfolio-wide, e.g. accountant fees). Money in (charges/payments)
   and money out (expenses) live in the same stream, so the financial
   summary, the mobile app, and the AI all read one table.

JOINT (household) charges: a charge may carry tenant=NULL *with a lease
set* — that is a joint-and-several household charge (roommate leases).
Every tenant on the lease owes it, any tenant's PAYMENT (which carries
tenant=<payer>) settles it for the whole household.
"""

import builtins
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Case
from django.db.models import DecimalField
from django.db.models import Exists
from django.db.models import F
from django.db.models import OuterRef
from django.db.models import Q
from django.db.models import Sum
from django.db.models import Value
from django.db.models import When
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from rentium.leases.models import Lease
from rentium.properties.models import Property
from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile


class EntryType(models.TextChoices):
    # Charges — money the tenant owes (a receivable).
    RENT_CHARGE = "RENT_CHARGE", _("Rent Charge")
    UTILITY_CHARGE = "UTILITY_CHARGE", _("Utility Charge")
    DEPOSIT_CHARGE = "DEPOSIT_CHARGE", _("Deposit Charge")
    FEE_CHARGE = "FEE_CHARGE", _("Fee Charge")
    OTHER_CHARGE = "OTHER_CHARGE", _("Other Charge")

    # Settlements — reduce a charge's outstanding balance.
    PAYMENT = "PAYMENT", _("Payment Received")
    CREDIT = "CREDIT", _("Credit / Discount")

    # Money out.
    EXPENSE = "EXPENSE", _("Expense")
    DEPOSIT_RETURN = "DEPOSIT_RETURN", _("Deposit Returned")

    # Corrections.
    REVERSAL = "REVERSAL", _("Reversal (void)")


CHARGE_TYPES = {
    EntryType.RENT_CHARGE,
    EntryType.UTILITY_CHARGE,
    EntryType.DEPOSIT_CHARGE,
    EntryType.FEE_CHARGE,
    EntryType.OTHER_CHARGE,
}

# Deposits are refundable liabilities, not income — excluded on purpose.
INCOME_CHARGE_TYPES = CHARGE_TYPES - {EntryType.DEPOSIT_CHARGE}

SETTLEMENT_TYPES = {EntryType.PAYMENT, EntryType.CREDIT}


class PaymentMethod(models.TextChoices):
    ETRANSFER = "ETRANSFER", _("e-Transfer")
    CASH = "CASH", _("Cash")
    CHEQUE = "CHEQUE", _("Cheque")
    OTHER = "OTHER", _("Other")


class ExpenseCategory(models.TextChoices):
    MAINTENANCE = "MAINTENANCE", _("Maintenance / Repairs")
    UTILITIES = "UTILITIES", _("Utilities (landlord-paid)")
    INSURANCE = "INSURANCE", _("Insurance")
    PROPERTY_TAX = "PROPERTY_TAX", _("Property Tax")
    MORTGAGE = "MORTGAGE", _("Mortgage / Financing")
    STRATA = "STRATA", _("Strata / HOA Fees")
    MANAGEMENT = "MANAGEMENT", _("Management / Professional Fees")
    SUPPLIES = "SUPPLIES", _("Supplies / Furnishings")
    ADVERTISING = "ADVERTISING", _("Advertising / Listing")
    OTHER = "OTHER", _("Other")


class ChargeStatus:
    """Computed, not stored (see rule 2)."""

    VOIDED = "VOIDED"
    PAID = "PAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    OVERDUE = "OVERDUE"
    DUE = "DUE"  # due today, unpaid
    SCHEDULED = "SCHEDULED"  # due in the future


class LedgerEntryQuerySet(models.QuerySet):
    def not_voided(self):
        return self.filter(reversed_by__isnull=True)

    def charges(self):
        return self.filter(entry_type__in=CHARGE_TYPES)

    def income_charges(self):
        return self.filter(entry_type__in=INCOME_CHARGE_TYPES)

    def with_settlement(self):
        """
        Annotate charges with settled_amount / outstanding / is_voided in a
        single query. Voided settlements do not count (rule 1 + 2 together:
        void a payment and the charge reopens automatically).
        """
        active_settlements = Coalesce(
            Sum(
                "settlements__amount",
                filter=Q(settlements__entry_type__in=SETTLEMENT_TYPES)
                & Q(settlements__reversed_by__isnull=True),
            ),
            Value(Decimal("0.00")),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )
        return self.annotate(
            settled_amount=active_settlements,
            outstanding=Case(
                When(reversed_by__isnull=False, then=Value(Decimal("0.00"))),
                default=F("amount") - F("settled_amount"),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
            is_voided=Exists(LedgerEntry.objects.filter(reverses=OuterRef("pk"))),
        )


class LedgerEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # ---- scope (the "which ledger" keys) --------------------------------
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    property = models.ForeignKey(
        Property,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text=_(
            "The ledger this entry belongs to. NULL = portfolio-wide (e.g. accountant fees)."
        ),
    )
    lease = models.ForeignKey(
        Lease,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
    )
    tenant = models.ForeignKey(
        TenantProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text=_(
            "Charges: who owes it — NULL with a lease set means the whole "
            "household owes it jointly. Payments: who paid."
        ),
    )

    # ---- the money -------------------------------------------------------
    entry_type = models.CharField(
        _("Type"), max_length=20, choices=EntryType.choices, db_index=True
    )
    amount = models.DecimalField(
        _("Amount"),
        max_digits=12,
        decimal_places=2,
        help_text=_("Always positive; the entry type carries the direction."),
    )
    due_date = models.DateField(
        _("Due Date"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Charges only."),
    )
    effective_date = models.DateField(
        _("Effective Date"),
        db_index=True,
        help_text=_("Service period start / payment date / date incurred."),
    )
    description = models.CharField(_("Description"), max_length=255)

    # ---- links -----------------------------------------------------------
    settles = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="settlements",
        help_text=_("For PAYMENT/CREDIT: the charge this settles."),
    )
    reverses = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversed_by",
        help_text=_(
            "For REVERSAL: the entry this voids. OneToOne — an entry can be voided exactly once."
        ),
    )
    work_order = models.ForeignKey(
        "maintenance.WorkOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text=_("Expense/charge tied to a maintenance job, if any."),
    )

    # ---- type-specific detail (nullable by design; validated in clean) ----
    payment_method = models.CharField(
        _("Payment Method"),
        max_length=12,
        choices=PaymentMethod.choices,
        blank=True,
        default="",
        help_text=_("PAYMENT / DEPOSIT_RETURN entries."),
    )
    reference_number = models.CharField(_("Reference #"), max_length=100, blank=True)
    category = models.CharField(
        _("Expense Category"),
        max_length=20,
        choices=ExpenseCategory.choices,
        blank=True,
        default="",
    )
    vendor = models.CharField(_("Vendor / Payee"), max_length=150, blank=True)

    # ---- the one mutable field on an otherwise immutable row --------------
    #
    # "Has this actually left my bank account yet?" is NOT a fact about the
    # money. The amount, the date incurred, the payee, the category — everything
    # that says what happened — stays frozen forever, exactly as before. This is a
    # fact about the *settlement* of an expense you already recorded truthfully,
    # and it is genuinely unknown at the moment you record it: you post the hydro
    # bill the day it arrives, and the auto-debit clears four days later.
    #
    # The purist alternative is to void the entry and repost it with the date
    # filled in. That would fill the audit trail with reversals recording nothing
    # anybody cares about, which makes the audit trail WORSE, not better — the
    # whole value of an append-only log is that every line in it means something.
    #
    # So: one whitelisted field, and save() below still refuses every other update
    # exactly as it always did. Note the whitelist works by REQUIRING
    # update_fields, so you cannot mutate paid_on and accidentally carry a stale
    # `amount` along in the same write.
    paid_on = models.DateField(
        _("Left My Account On"),
        null=True,
        blank=True,
        help_text=_(
            "EXPENSE entries only. NULL = recorded, but not yet taken from the "
            "bank. Set it when the payment actually clears."
        ),
    )

    # ---- safety & audit ----------------------------------------------------
    idempotency_key = models.CharField(
        _("Idempotency Key"),
        max_length=180,
        null=True,
        blank=True,
        unique=True,
        help_text=_(
            "Natural key for generated entries; client-generated UUID for API writes. "
            "Duplicate submissions are rejected by the database."
        ),
    )
    metadata = models.JSONField(_("Metadata"), default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_entries_created",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = LedgerEntryQuerySet.as_manager()

    # The complete list of columns that may change after an entry is posted.
    # Adding to this set is a decision about the integrity of the whole ledger,
    # not a convenience — think hard before it grows.
    MUTABLE_AFTER_POST = frozenset({"paid_on"})

    class Meta:
        ordering = ["-effective_date", "-created_at"]
        indexes = [
            models.Index(fields=["landlord", "entry_type", "effective_date"]),
            models.Index(fields=["lease", "entry_type", "due_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(amount__gt=0), name="ledger_amount_positive"
            ),
        ]
        verbose_name = _("Ledger Entry")
        verbose_name_plural = _("Ledger Entries")

    def __str__(self):
        return f"{self.get_entry_type_display()} ${self.amount} — {self.description}"

    # ---- immutability (rule 1) --------------------------------------------
    def save(self, *args, **kwargs):
        if not self._state.adding:
            touched = set(kwargs.get("update_fields") or ())
            if not touched or not touched.issubset(self.MUTABLE_AFTER_POST):
                raise ValidationError(
                    "Ledger entries are immutable. Void this entry (REVERSAL) and "
                    "post a new one instead."
                )
            if self.entry_type != EntryType.EXPENSE:
                raise ValidationError("Only an expense carries a bank-clearing date.")
            # Deliberately no full_clean(): nothing financial moved, and
            # re-validating the whole row would re-run charge/settlement rules
            # against an entry that is changing none of them.
            return super().save(*args, **kwargs)

        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Ledger entries can never be deleted. Void with a REVERSAL entry."
        )

    def clean(self):
        super().clean()
        et = self.entry_type

        if self.property and self.property.landlord_id != self.landlord_id:
            raise ValidationError(
                {"property": _("Property belongs to a different landlord.")}
            )

        if et in CHARGE_TYPES:
            if not self.due_date:
                raise ValidationError({"due_date": _("Charges require a due date.")})
            # A charge names EITHER the tenant who owes it (split billing)
            # OR just the lease (tenant=NULL = joint household charge).
            if not self.tenant_id and not self.lease_id:
                raise ValidationError(
                    {
                        "tenant": _(
                            "Charges must name the tenant who owes them, or carry a "
                            "lease for a joint household charge."
                        )
                    }
                )

        if et in SETTLEMENT_TYPES:
            if not self.settles_id:
                raise ValidationError(
                    {
                        "settles": _(
                            "Payments/credits must reference the charge they settle."
                        )
                    }
                )
            if self.settles and self.settles.entry_type not in CHARGE_TYPES:
                raise ValidationError({"settles": _("Can only settle a charge entry.")})

        if et == EntryType.EXPENSE and not self.category:
            raise ValidationError({"category": _("Expenses require a category.")})

        if self.paid_on and et != EntryType.EXPENSE:
            raise ValidationError(
                {"paid_on": _("Only an expense carries a bank-clearing date.")}
            )

        if et == EntryType.REVERSAL:
            if not self.reverses_id:
                raise ValidationError(
                    {"reverses": _("A reversal must reference the entry it voids.")}
                )
            if self.reverses and self.reverses.entry_type == EntryType.REVERSAL:
                raise ValidationError({"reverses": _("Cannot void a reversal.")})
            if self.reverses and self.amount != self.reverses.amount:
                raise ValidationError(
                    {
                        "amount": _(
                            "A reversal must be equal and opposite to its target."
                        )
                    }
                )

    # ---- computed status (rule 2) -------------------------------------------
    # NOTE: this model has a field named `property`, which shadows the
    # `property` builtin inside the class body — so decorate with the
    # explicit builtin reference.
    @builtins.property
    def voided(self) -> bool:
        return hasattr(self, "reversed_by") and self.reversed_by is not None

    @builtins.property
    def bank_status(self) -> str:
        """Human-readable settlement state for an expense. Nothing else has one."""
        if self.entry_type != EntryType.EXPENSE:
            return ""
        return "PAID" if self.paid_on else "NOT_YET_TAKEN"

    def charge_status(self, today=None) -> str:
        """Prefer annotated querysets (with_settlement) in list views; this
        per-object version is for detail views and services."""
        from datetime import date as date_cls

        if self.entry_type not in CHARGE_TYPES:
            raise ValueError("charge_status() only applies to charge entries.")
        if self.voided:
            return ChargeStatus.VOIDED

        settled = getattr(self, "settled_amount", None)
        if settled is None:
            settled = self.settlements.filter(
                entry_type__in=SETTLEMENT_TYPES, reversed_by__isnull=True
            ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

        today = today or date_cls.today()
        if settled >= self.amount:
            return ChargeStatus.PAID
        if settled > 0:
            return ChargeStatus.PARTIALLY_PAID
        if self.due_date and self.due_date < today:
            return ChargeStatus.OVERDUE
        if self.due_date and self.due_date == today:
            return ChargeStatus.DUE
        return ChargeStatus.SCHEDULED


class LedgerAttachment(models.Model):
    """Receipts / documents for an entry (kept off the ledger row itself)."""

    entry = models.ForeignKey(
        LedgerEntry, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(_("File"), upload_to="ledger_attachments/%Y/%m/")
    label = models.CharField(_("Label"), max_length=150, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Attachment for {self.entry_id}"
