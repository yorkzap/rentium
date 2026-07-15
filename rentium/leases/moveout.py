"""
Move-out workflow: notices to end tenancy + mutual agreements (RTB-8).

One model carries all three real-world paths:

  TENANT_NOTICE      Tenant gives written notice. If the requested end date
                     satisfies the rules (tenancy_rules.py), it is ACCEPTED
                     AUTOMATICALLY — valid notice needs no landlord approval.
  LANDLORD_NOTICE    Landlord serves notice for landlord use (moving in
                     etc.). Validated against the landlord notice period
                     (3 clear months under the BC RTA; no minimum when the
                     landlord shares common areas with the tenancy).
  MUTUAL_AGREEMENT   Either party proposes ending on a date shorter than
                     their notice period allows. Nothing happens until the
                     OTHER party signs (BC form RTB-8). Until accepted,
                     the tenant still owes rent through the full notice
                     period; if declined, nothing changes.

On acceptance (any kind), apply_to_lease():
  - stamps lease.move_out_date (and pulls in end_date if later/absent),
  - voids all UNSETTLED charges due after the effective end (rent for
    months that will never happen),
  - for mutual agreements, applies the landlord's chosen rent_handling to
    the FINAL month: VOID_FINAL (cancel it), PRORATE_FINAL (credit the
    unused days), or NONE (keep as billed / settle manually).
Charges with money on them are never touched — received money is
historical fact (same principle as everywhere else in the ledger).

The rules that governed the decision are frozen into rules_snapshot at
creation, so the record stays auditable even if the lease's area-sharing
flags change later.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class MoveOutRequest(models.Model):
    class Kind(models.TextChoices):
        TENANT_NOTICE = "TENANT_NOTICE", _("Tenant Notice to End Tenancy")
        LANDLORD_NOTICE = "LANDLORD_NOTICE", _("Landlord Notice (Landlord Use)")
        MUTUAL_AGREEMENT = "MUTUAL_AGREEMENT", _("Mutual Agreement to End Tenancy")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Awaiting Signature")
        ACCEPTED = "ACCEPTED", _("Accepted")
        DECLINED = "DECLINED", _("Declined")
        CANCELLED = "CANCELLED", _("Cancelled")

    class InitiatedBy(models.TextChoices):
        TENANT = "TENANT", _("Tenant")
        LANDLORD = "LANDLORD", _("Landlord")

    class RentHandling(models.TextChoices):
        NONE = "NONE", _("Keep final month as billed")
        VOID_FINAL = "VOID_FINAL", _("Void the final month's rent")
        PRORATE_FINAL = "PRORATE_FINAL", _("Prorate the final month (credit unused days)")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    lease = models.ForeignKey(
        "leases.Lease", on_delete=models.CASCADE, related_name="moveout_requests"
    )
    lease_tenant = models.ForeignKey(
        "leases.LeaseTenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="moveout_requests",
        help_text=_("The tenant slot that initiated / is named on the request."),
    )
    initiated_by = models.CharField(max_length=10, choices=InitiatedBy.choices)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    requested_end_date = models.DateField(
        _("Requested End Date"),
        help_text=_("The date the tenancy is proposed to end (tenant vacates by 1 p.m.)."),
    )
    effective_end_date = models.DateField(
        _("Effective End Date"),
        null=True,
        blank=True,
        help_text=_("Set on acceptance — the date the tenancy actually ends."),
    )
    reason = models.TextField(_("Reason"), blank=True)
    decline_reason = models.TextField(_("Decline Reason"), blank=True)
    form_type = models.CharField(
        _("Agreement Form"),
        max_length=20,
        blank=True,
        help_text=_("e.g. RTB-8 for BC mutual agreements."),
    )
    rent_handling = models.CharField(
        max_length=15, choices=RentHandling.choices, default=RentHandling.NONE,
        help_text=_(
            "For mutual agreements: what happens to the final month's rent "
            "once accepted. Chosen by the landlord."
        ),
    )
    tenant_signed = models.BooleanField(default=False)
    tenant_signed_at = models.DateTimeField(null=True, blank=True)
    landlord_signed = models.BooleanField(default=False)
    landlord_signed_at = models.DateTimeField(null=True, blank=True)
    # The tenancy_rules payload frozen at submission time — auditability
    # (and the AI controller can read WHY a request auto-accepted).
    rules_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Move-Out Request")
        verbose_name_plural = _("Move-Out Requests")
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.get_kind_display()} [{self.status}] on lease "
            f"{self.lease.lease_number or self.lease_id} -> {self.requested_end_date}"
        )

    def clean(self):
        super().clean()
        if self.requested_end_date and self.requested_end_date < date.today():
            raise ValidationError(
                {"requested_end_date": _("The end date cannot be in the past.")}
            )

    # ------------------------------------------------------------ actions
    def sign(self, *, as_landlord: bool):
        now = timezone.now()
        if as_landlord:
            self.landlord_signed, self.landlord_signed_at = True, now
        else:
            self.tenant_signed, self.tenant_signed_at = True, now

    def accept(self, *, effective_end_date: date | None = None, rent_handling: str | None = None):
        """
        Finalize: both signatures present (or auto-accept for a valid
        notice), stamp the effective date, and apply the lease/ledger side
        effects. Idempotent-ish: raises if not PENDING.
        """
        if self.status != self.Status.PENDING:
            raise ValidationError(_("Only a pending request can be accepted."))
        self.effective_end_date = effective_end_date or self.requested_end_date
        if rent_handling:
            self.rent_handling = rent_handling
        self.status = self.Status.ACCEPTED
        self.save()
        self.apply_to_lease()
        self._publish("lease.moveout_accepted")

    def decline(self, *, reason: str = ""):
        if self.status != self.Status.PENDING:
            raise ValidationError(_("Only a pending request can be declined."))
        self.status = self.Status.DECLINED
        self.decline_reason = reason
        self.save(update_fields=["status", "decline_reason", "updated_at"])
        self._publish("lease.moveout_declined")

    def cancel(self):
        if self.status != self.Status.PENDING:
            raise ValidationError(_("Only a pending request can be cancelled."))
        self.status = self.Status.CANCELLED
        self.save(update_fields=["status", "updated_at"])

    def _publish(self, event_type: str):
        from rentium.events.registry import publish

        publish(
            event_type,
            {
                "moveout_id": str(self.pk),
                "lease_id": str(self.lease_id),
                "kind": self.kind,
                "initiated_by": self.initiated_by,
                "requested_end_date": self.requested_end_date.isoformat(),
                "effective_end_date": self.effective_end_date.isoformat()
                if self.effective_end_date
                else None,
                "rent_handling": self.rent_handling,
            },
            property_id=self.lease.property_id,
            lease_id=self.lease_id,
        )

    # -------------------------------------------------------- side effects
    def apply_to_lease(self):
        """
        The ONLY place a move-out mutates the lease + ledger. Deliberately
        an intentional exception to the 'locked leases are admin-only'
        rule — this is a system consequence of a lawful notice / signed
        agreement, not a manual edit (same reasoning as
        clip_overlapping_month_to_month_leases()).
        """
        end = self.effective_end_date
        lease = self.lease

        # 1) Lease dates + audit note.
        note = (
            f"[System] Tenancy ending {end} per {self.get_kind_display()} "
            f"({self.form_type or 'written notice'}) accepted "
            f"{timezone.now().date()}."
        )
        lease.move_out_date = end
        if lease.is_month_to_month or (lease.end_date and lease.end_date > end) or not lease.end_date:
            lease.is_month_to_month = False
            lease.end_date = end
        lease.special_terms = (
            f"{lease.special_terms}\n\n{note}".strip() if lease.special_terms else note
        )
        lease.save()

        # 2) Void every UNSETTLED charge due after the tenancy ends (rent
        #    for months that will never happen). Paid/partial charges are
        #    untouched.
        from rentium.ledger.models import CHARGE_TYPES, LedgerEntry
        from rentium.ledger.services import post_credit, void_entry

        future = LedgerEntry.objects.with_settlement().filter(
            lease=lease,
            entry_type__in=CHARGE_TYPES,
            reversed_by__isnull=True,
            due_date__gt=end,
        )
        for charge in future:
            if charge.settled_amount and charge.settled_amount > 0:
                continue
            void_entry(charge, reason=f"Tenancy ends {end} — charge beyond end of tenancy")

        # 3) Final month handling (mutual agreements let the landlord
        #    choose; statutory notices bill the final month in full).
        if self.rent_handling == self.RentHandling.NONE:
            return
        first_of_final = end.replace(day=1)
        final_rent = (
            LedgerEntry.objects.with_settlement()
            .filter(
                lease=lease,
                entry_type="RENT_CHARGE",
                reversed_by__isnull=True,
                due_date__gte=first_of_final,
                due_date__lte=end,
            )
            .order_by("due_date")
        )
        for charge in final_rent:
            settled = charge.settled_amount or Decimal("0.00")
            if self.rent_handling == self.RentHandling.VOID_FINAL:
                if settled > 0:
                    continue  # money already on it — leave for manual resolution
                void_entry(
                    charge,
                    reason=f"Mutual agreement ({self.form_type or 'signed'}): final month's rent waived",
                )
            elif self.rent_handling == self.RentHandling.PRORATE_FINAL:
                from .tenancy_rules import last_day_of_month

                days_in_month = last_day_of_month(end).day
                unused_days = days_in_month - end.day
                if unused_days <= 0:
                    continue
                credit = (
                    Decimal(charge.amount) * Decimal(unused_days) / Decimal(days_in_month)
                ).quantize(Decimal("0.01"))
                outstanding = Decimal(charge.amount) - settled
                credit = min(credit, outstanding)
                if credit <= 0:
                    continue
                post_credit(
                    charge=charge,
                    amount=credit,
                    reason=(
                        f"Prorated final month — tenancy ends {end} "
                        f"({unused_days} unused day(s))"
                    ),
                    idempotency_key=f"moveout-prorate:{self.pk}:{charge.pk}",
                    metadata={"moveout_id": str(self.pk)},
                )
