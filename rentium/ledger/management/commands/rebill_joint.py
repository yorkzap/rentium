"""
One-time (idempotent, re-runnable) converter: for every ACTIVE lease that
bills JOINTLY (roommate agreements), void the old per-tenant generated
charges and repost them as household charges (tenant=NULL).

Only touches charges the billing engine itself generated (recognised by
their natural idempotency keys: rent:/security_deposit:/pet_deposit:/
cleaning_deposit_lease:) — manual one-off charges are left alone. Charges that
already have live payments/credits on them are SKIPPED and reported, since
received money is historical fact; resolve those by hand (void the payment,
re-run, re-record the payment against the new joint charge).

Usage:
    python manage.py rebill_joint --dry-run   # show what would happen
    python manage.py rebill_joint             # do it
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from rentium.leases.models import Lease

from ...billing import generate_initial_charges, lease_is_joint
from ...models import CHARGE_TYPES, LedgerEntry
from ...services import LedgerError, void_entry

GENERATED_KEY_PREFIXES = (
    "rent:",
    "security_deposit:",
    "pet_deposit:",
    "cleaning_deposit_lease:",
)


class Command(BaseCommand):
    help = "Void per-tenant generated charges on joint (roommate) leases and repost them as household charges."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
        parser.add_argument("--lease", type=int, default=None, help="Limit to one lease id.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        leases = Lease.objects.filter(status=Lease.LeaseStatus.ACTIVE)
        if options["lease"]:
            leases = leases.filter(pk=options["lease"])

        joint_leases = [l for l in leases if lease_is_joint(l)]
        if not joint_leases:
            self.stdout.write("No active joint-billing leases found.")
            return

        total_voided = total_skipped = 0
        for lease in joint_leases:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\nLease {lease.pk} ({getattr(lease, 'lease_number', lease.pk)})"
            ))
            old = (
                LedgerEntry.objects.with_settlement()
                .filter(
                    lease=lease,
                    tenant__isnull=False,          # per-tenant = the old model
                    entry_type__in=CHARGE_TYPES,
                    reversed_by__isnull=True,      # not already voided
                )
            )
            # Only the engine's own generated charges; manual ones stay.
            old = [
                c for c in old
                if c.idempotency_key and c.idempotency_key.startswith(GENERATED_KEY_PREFIXES)
            ]
            if not old:
                self.stdout.write("  no per-tenant generated charges to convert")
            with transaction.atomic():
                for charge in old:
                    label = f"{charge.entry_type} ${charge.amount} due {charge.due_date} ({charge.tenant})"
                    if charge.settled_amount and charge.settled_amount > 0:
                        total_skipped += 1
                        self.stdout.write(self.style.WARNING(
                            f"  SKIP (has ${charge.settled_amount} in payments): {label}"
                        ))
                        continue
                    if dry:
                        self.stdout.write(f"  would void: {label}")
                    else:
                        try:
                            void_entry(charge, reason="Converted to joint household billing")
                            self.stdout.write(f"  voided: {label}")
                        except LedgerError as exc:
                            total_skipped += 1
                            self.stdout.write(self.style.WARNING(f"  SKIP ({exc}): {label}"))
                            continue
                    total_voided += 1
                if dry:
                    self.stdout.write("  would repost household charges (deposit/fees/rent schedule)")
                    transaction.set_rollback(True)
                else:
                    # Natural joint keys differ from the old per-tenant keys,
                    # so this posts fresh household charges idempotently.
                    generate_initial_charges(lease)
                    self.stdout.write(self.style.SUCCESS("  reposted household charges"))

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {'Would void' if dry else 'Voided'} {total_voided} charge(s); "
            f"{total_skipped} skipped (already paid — resolve by hand)."
        ))
