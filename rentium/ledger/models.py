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

    def damage_claims(self):
        """Charges recovering the cost of damage from a tenant.

        A FEE_CHARGE covers two unrelated things: a late fee (ordinary income,
        collectible alongside rent) and a damage-recovery claim raised off a
        work order. Only the damage claim carries a work_order — billing.py
        never sets one on a late fee — so the two are already distinguishable
        without a new entry type.
        """
        return self.filter(
            entry_type=EntryType.FEE_CHARGE, work_order__isnull=False
        )

    def deposit_charges(self):
        """The mirror of income_charges().

        A deposit is a refundable liability, never income — which is why it is
        excluded from expected/collected/outstanding. But it is still genuinely
        OWED, and dropping it from every bucket meant a $425 deposit could sit
        in the ledger badged "overdue" while the Outstanding tile above it read
        $19.78 and the Overdue tile read 1. Deposits get their own bucket so
        they can be disclosed without being misfiled as income.
        """
        return self.filter(entry_type=EntryType.DEPOSIT_CHARGE)

    def open_charges(self, *, as_of=None):
        """Charges that are due and not yet settled — the shape the summary,
        RAMA's snapshot and the attention feed each used to spell out longhand.

        Chain the cheap classifiers onto the result (.income_charges(),
        .deposit_charges(), .damage_claims(), .expected_income()) so every
        caller partitions the same set rather than re-deriving "what is owed".
        """
        from datetime import date as _date

        qs = self if "outstanding" in self.query.annotations else self.with_settlement()
        return qs.filter(
            reversed_by__isnull=True,
            due_date__lte=as_of or _date.today(),
            outstanding__gt=0,
        )

    def expected_income(self):
        """Income the landlord can actually expect to collect this period.

        Damage claims are excluded deliberately. They are contested by nature
        and can only be kept lawfully at the end of the tenancy — with the
        tenant's written agreement, or via an RTB application inside the
        statutory window (see services.deposit_position, which reports them as
        claims against the deposit alongside those routes). Counting one as
        expected rent overstates the month and invites treating a disputed
        claim as money already earned. It is reported separately, never hidden.
        """
        return self.income_charges().exclude(
            entry_type=EntryType.FEE_CHARGE, work_order__isnull=False
        )

    def with_settlement(self):
        """
        Annotate charges with settled_amount / outstanding / is_voided in a
        single query. Voided settlements do not count (rule 1 + 2 together:
        void a payment and the charge reopens automatically).

        Both money annotations are NULL for non-charge types. Only a charge is
        a receivable, so only a charge can have a balance: nothing ever points
        at an EXPENSE / PAYMENT / CREDIT / DEPOSIT_RETURN / REVERSAL via
        `settles`, which used to leave them annotated `amount - 0 == amount`.
        Every consumer read that as "still owed" — the Financial feed rendered
        a settled expense as "Paid … $31.45 left", and a REVERSAL as a live
        $19.78 balance next to the entry it had just voided. `charge_status`
        (api/views.py) has always null-guarded on CHARGE_TYPES; this is the
        matching half, so the serializer's documented contract — "charges only;
        null otherwise" — is now true of the queryset that feeds it.
        """
        money = DecimalField(max_digits=12, decimal_places=2)
        active_settlements = Coalesce(
            Sum(
                "settlements__amount",
                filter=Q(settlements__entry_type__in=SETTLEMENT_TYPES)
                & Q(settlements__reversed_by__isnull=True),
            ),
            Value(Decimal("0.00")),
            output_field=money,
        )
        # Two passes: a Case cannot reference an aggregate declared in the same
        # annotate() call, so the Sum lands first and the Cases read it by F().
        return self.annotate(_settled=active_settlements).annotate(
            settled_amount=Case(
                When(entry_type__in=CHARGE_TYPES, then=F("_settled")),
                default=Value(None, output_field=money),
                output_field=money,
            ),
            outstanding=Case(
                # Non-charges first: a voided EXPENSE is still not a receivable,
                # so it must fall through to NULL rather than to 0.00 below.
                When(~Q(entry_type__in=CHARGE_TYPES), then=Value(None, output_field=money)),
                When(reversed_by__isnull=False, then=Value(Decimal("0.00"))),
                default=F("amount") - F("_settled"),
                output_field=money,
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
    holding = models.ForeignKey(
        "properties.PropertyHolding",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_entries",
        help_text=_(
            "Physical/legal property scope. Set directly for holding-wide costs "
            "such as tax or mortgage; otherwise derived from property."
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
            models.Index(
                fields=["landlord", "holding", "effective_date"],
                name="ledger_holding_date_idx",
            ),
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
        if self.holding and self.holding.landlord_id != self.landlord_id:
            raise ValidationError(
                {"holding": _("Holding belongs to a different landlord.")}
            )
        if (
            self.property
            and self.holding
            and self.property.holding_id != self.holding_id
        ):
            raise ValidationError(
                {"holding": _("Holding does not contain the selected listing.")}
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


class PropertyBankBalance(models.Model):
    """Landlord-reported bank balance for one holding (house/building) — or
    the whole portfolio when `holding` is null.

    NOT a bank feed: this is what the landlord tells RAMA their account
    holds, as of a date. The min-balance Sergeant compares this figure
    against the landlord's Constitution rules and separately tracks ledger
    movement since `as_of` (drift) so a stale report reads as "stale," not
    as a false balance breach. Real bank integration (Flinks/Plaid CA) is a
    later phase that would swap the SOURCE of these rows, not this shape.
    """

    class Source(models.TextChoices):
        UI = "UI", _("Entered in the app")
        CHAT = "CHAT", _("Entered via RAMA")

    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="bank_balances"
    )
    holding = models.ForeignKey(
        "properties.PropertyHolding",
        on_delete=models.CASCADE,
        related_name="bank_balances",
        null=True,
        blank=True,
        help_text=_("Null = portfolio-wide balance (no per-house breakdown yet)."),
    )
    label = models.CharField(max_length=100, blank=True, default="Operating")
    balance = models.DecimalField(max_digits=12, decimal_places=2)
    as_of = models.DateField()
    updated_via = models.CharField(
        max_length=10, choices=Source.choices, default=Source.UI
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Bank Balance")
        verbose_name_plural = _("Bank Balances")
        ordering = ["landlord", "holding__name"]
        constraints = [
            # One current balance row per (landlord, holding) — corrections
            # overwrite it (this is a reported snapshot, not an append-only
            # ledger; the ledger itself remains append-only as always). Two
            # constraints because Postgres treats NULL as distinct from NULL,
            # so a plain unique on (landlord, holding) would let unlimited
            # portfolio-wide (holding=null) rows through.
            models.UniqueConstraint(
                fields=["landlord", "holding"],
                condition=models.Q(holding__isnull=False),
                name="ledger_bank_balance_one_per_holding",
            ),
            models.UniqueConstraint(
                fields=["landlord"],
                condition=models.Q(holding__isnull=True),
                name="ledger_bank_balance_one_portfolio_wide",
            ),
        ]

    def __str__(self):
        where = self.holding.name if self.holding_id else "portfolio"
        return f"{where}: ${self.balance} as of {self.as_of}"


class ImportBatch(models.Model):
    """One historical-data upload — a batch of StagedLedgerEntry rows the
    landlord can freely edit before anything becomes permanent. Nothing in
    a batch touches the real (append-only) ledger until commit_batch()
    posts each clean row through the normal ledger/services writers inside
    one atomic transaction. This is the ONLY place ledger rows are created
    in bulk from outside the app's own workflows — everything else about
    the ledger's append-only, audited nature is unchanged.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft — still editable")
        COMMITTED = "COMMITTED", _("Committed")
        DISCARDED = "DISCARDED", _("Discarded")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    landlord = models.ForeignKey(
        LandlordProfile, on_delete=models.CASCADE, related_name="import_batches"
    )
    label = models.CharField(max_length=200, blank=True, default="")
    source_file = models.FileField(
        upload_to="ledger_imports/%Y/%m/", blank=True, null=True
    )
    source_filename = models.CharField(max_length=255, blank=True, default="")
    column_map = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Import Batch")
        verbose_name_plural = _("Import Batches")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.label or self.source_filename or self.pk} ({self.status})"


class StagedLedgerEntry(models.Model):
    """One row of a historical import, fully mutable while its batch is
    DRAFT. `issues` is recomputed by staging_services.validate_row() on
    every edit — a row with any issue cannot be committed, so "why won't
    this commit" is always visible right on the row, not a mystery."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        ImportBatch, on_delete=models.CASCADE, related_name="rows"
    )
    row_number = models.PositiveIntegerField(default=0)
    entry_type = models.CharField(
        max_length=20, choices=EntryType.choices, blank=True, default=""
    )
    amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    due_date = models.DateField(null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)
    property = models.ForeignKey(
        Property, on_delete=models.SET_NULL, null=True, blank=True
    )
    lease = models.ForeignKey(Lease, on_delete=models.SET_NULL, null=True, blank=True)
    tenant = models.ForeignKey(
        "users.TenantProfile", on_delete=models.SET_NULL, null=True, blank=True
    )
    category = models.CharField(max_length=30, blank=True, default="")
    vendor = models.CharField(max_length=150, blank=True, default="")
    description = models.CharField(max_length=255, blank=True, default="")
    payment_method = models.CharField(max_length=20, blank=True, default="")
    # For PAYMENT/CREDIT rows: which charge row in THIS batch it settles.
    settles_row = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    raw = models.JSONField(default=dict, blank=True)  # original spreadsheet row
    issues = models.JSONField(default=list, blank=True)
    committed_entry = models.ForeignKey(
        LedgerEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Staged Ledger Entry")
        verbose_name_plural = _("Staged Ledger Entries")
        ordering = ["batch", "row_number"]

    def __str__(self):
        return f"row {self.row_number} of {self.batch_id}: {self.entry_type} {self.amount}"


# ---------------------------------------------------------------------------
# What a property cost, what it is worth, and what is owed on it.
#
# These are what turn "how much rent came in" into "am I actually making
# money". They are separate models rather than fields on PropertyHolding
# because two of the three are inherently HISTORIES: a valuation is only
# meaningful with its date, and a mortgage is superseded at each renewal.
#
# PropertyBankBalance (above) is the cautionary example — it is
# update_or_create per holding, so a correction overwrites the previous figure
# and no trend can ever be computed from it.
# ---------------------------------------------------------------------------
class HoldingFinancials(models.Model):
    """Acquisition facts for one holding. One row; these rarely change."""

    holding = models.OneToOneField(
        "properties.PropertyHolding",
        on_delete=models.CASCADE,
        related_name="financials",
    )
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="holding_financials",
    )
    purchase_price = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    purchase_date = models.DateField(null=True, blank=True)
    land_transfer_tax = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    closing_costs = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    # Capital improvements raise the adjusted cost base and therefore reduce
    # the eventual capital gain — worth tracking from day one, because
    # reconstructing it at sale is how people lose the deduction.
    capital_improvements_to_date = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    year_built = models.PositiveIntegerField(null=True, blank=True)
    heated_area_sqft = models.PositiveIntegerField(null=True, blank=True)
    # Feeds which improvements are even applicable (a heat-pump analysis is
    # meaningless for a place already on a heat pump).
    heating_type = models.CharField(max_length=30, blank=True, default="")
    source_document = models.ForeignKey(
        "rama.RamaDocument",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "holding financials"
        verbose_name_plural = "holding financials"

    def __str__(self):
        return f"Financials for {self.holding_id}"


class HoldingValuation(models.Model):
    """What a holding was worth, on a date, on a stated basis.

    Append-only on purpose: many rows per holding is the point. Equity trend,
    "is this still worth what I paid", and any return calculation need the
    series, not the latest number.
    """

    class Basis(models.TextChoices):
        BC_ASSESSMENT = "BC_ASSESSMENT", _("BC Assessment")
        REALTOR_CMA = "REALTOR_CMA", _("Realtor comparative market analysis")
        APPRAISAL = "APPRAISAL", _("Professional appraisal")
        LANDLORD_ESTIMATE = "LANDLORD_ESTIMATE", _("Landlord's own estimate")
        AUTOMATED = "AUTOMATED", _("Automated estimate")

    holding = models.ForeignKey(
        "properties.PropertyHolding",
        on_delete=models.CASCADE,
        related_name="valuations",
    )
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="holding_valuations",
    )
    as_of = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    basis = models.CharField(max_length=20, choices=Basis.choices)
    source_url = models.URLField(max_length=500, blank=True, default="")
    source_fetched_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-as_of"]
        # One figure per basis per date: a second BC Assessment for the same
        # year is a correction, not a new data point.
        constraints = [
            models.UniqueConstraint(
                fields=["holding", "as_of", "basis"],
                name="holding_valuation_unique_per_basis_date",
            )
        ]
        indexes = [models.Index(fields=["landlord", "holding", "-as_of"])]

    def __str__(self):
        return f"{self.holding_id} worth {self.amount} on {self.as_of} ({self.basis})"


class HoldingMortgage(models.Model):
    """A mortgage on a holding. Many rows, at most one ACTIVE.

    Superseded rather than edited at renewal, so "what rate were we on in
    2024" stays answerable — which is exactly the question a renewal decision
    needs.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        DISCHARGED = "DISCHARGED", _("Discharged")
        SUPERSEDED = "SUPERSEDED", _("Superseded by a renewal")

    class RateType(models.TextChoices):
        FIXED = "FIXED", _("Fixed")
        VARIABLE = "VARIABLE", _("Variable")

    holding = models.ForeignKey(
        "properties.PropertyHolding",
        on_delete=models.CASCADE,
        related_name="mortgages",
    )
    landlord = models.ForeignKey(
        "users.LandlordProfile",
        on_delete=models.CASCADE,
        related_name="holding_mortgages",
    )
    lender = models.CharField(max_length=120, blank=True, default="")
    original_principal = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    current_principal = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    # A balance is only meaningful with the date it was true on; without this
    # the projection below cannot say how stale it is.
    current_principal_as_of = models.DateField(null=True, blank=True)
    rate_percent = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True
    )
    rate_type = models.CharField(
        max_length=10, choices=RateType.choices, blank=True, default=""
    )
    amortization_months = models.PositiveIntegerField(null=True, blank=True)
    payment_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    payment_frequency = models.CharField(max_length=12, blank=True, default="")
    term_start = models.DateField(null=True, blank=True)
    term_end = models.DateField(null=True, blank=True)  # the renewal date
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source_document = models.ForeignKey(
        "rama.RamaDocument",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-term_start", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["holding"],
                condition=models.Q(status="ACTIVE"),
                name="holding_mortgage_single_active",
            )
        ]
        indexes = [models.Index(fields=["landlord", "status", "term_end"])]

    def __str__(self):
        return f"{self.lender or 'Mortgage'} on {self.holding_id} ({self.status})"
