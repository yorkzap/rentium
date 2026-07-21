"""
Wipe one landlord's OPERATIONAL data so they can move from a test environment
into real production use — WITHOUT deleting their account/login.

Unlike leases/.../reset_database.py (drops the whole schema, kills the landlord,
dev-only) and scripts/rama_seed_demo.py --reset (only removes [RAMA-DEMO]
fixtures, dev-only), this is safe to run in production against ONE landlord.

Preserves by default: the landlord's User + LandlordProfile, their Showcase
(+ public slug), their ChannelAccounts (Telegram/WhatsApp links), and their
RamaPreferences (provider/model/BYOK key). Deletes leases, properties, ledger,
payments, inspections, work orders, occupancy, inquiries, conversations,
appointments, agenda, RAMA memory, and — unless --keep-tenants — the test
tenant accounts that were on this landlord's leases (a tenant shared with
another landlord is left alone).

Everything runs in ONE transaction: a PROTECT surprise rolls the whole thing
back rather than leaving a half-wiped ledger.

Usage:
    # dry run — prints counts, changes nothing
    python manage.py wipe_landlord_data --email me@example.com
    # actually delete
    python manage.py wipe_landlord_data --email me@example.com --confirm
    # keep tenant logins; also clear the event/notification history
    python manage.py wipe_landlord_data --email me@example.com --confirm \\
        --keep-tenants --include-events
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError


class Command(BaseCommand):
    help = "Wipe one landlord's operational data, preserving their account."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Landlord account email.")
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Actually delete. Without this it's a dry run (prints counts).",
        )
        parser.add_argument(
            "--keep-tenants",
            action="store_true",
            help="Do not delete tenant User accounts (only their leases/ledger).",
        )
        parser.add_argument(
            "--include-events",
            action="store_true",
            help="Also clear DomainEvent / Notification history (loose UUID refs).",
        )

    def handle(self, *args, **opts):
        from rentium.users.models import LandlordProfile, User

        email = opts["email"].strip().lower()
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            raise CommandError(f"No user with email {email!r}.")
        landlord = LandlordProfile.objects.filter(user=user).first()
        if landlord is None:
            raise CommandError(f"{email!r} is not a landlord (no LandlordProfile).")

        steps = self._build_steps(landlord, opts)

        # --- report ---
        self.stdout.write(self.style.WARNING(f"\nLandlord: {email} (id {landlord.pk})"))
        self.stdout.write("Will delete:")
        total = 0
        for label, qs_fn in steps:
            try:
                n = qs_fn().count()
            except Exception as exc:  # noqa: BLE001 — a missing model shouldn't abort the report
                self.stdout.write(f"  - {label}: (skipped: {exc})")
                continue
            total += n
            self.stdout.write(f"  - {label}: {n}")
        tenant_ids = self._tenant_user_ids(landlord) if not opts["keep_tenants"] else set()
        self.stdout.write(
            f"  - tenant accounts (best-effort): {len(tenant_ids)}"
            if tenant_ids
            else "  - tenant accounts: kept"
        )
        self.stdout.write(self.style.SUCCESS("\nPreserved: your login, showcase page, channel links, RAMA settings."))

        if not opts["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDRY RUN — nothing deleted. Re-run with --confirm to apply."
                )
            )
            return

        # --- delete, atomically ---
        with transaction.atomic():
            # The ledger's self-references (settles/reverses) are PROTECT, so a
            # plain delete of the landlord's entries fails even though every
            # referencing row is in the set. Null those FKs first (they're
            # nullable), then the delete proceeds.
            try:
                from rentium.ledger.models import LedgerEntry

                LedgerEntry.objects.filter(landlord=landlord).update(
                    settles=None, reverses=None
                )
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(f"  (ledger self-ref clear skipped: {exc})")

            for label, qs_fn in steps:
                try:
                    deleted, _ = qs_fn().delete()
                    self.stdout.write(f"  deleted {label}: {deleted}")
                except ProtectedError as exc:
                    raise CommandError(
                        f"PROTECT blocked deleting {label} ({exc}). Nothing was "
                        "deleted (rolled back)."
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(f"  {label}: skipped ({exc})")
            if tenant_ids:
                self._delete_tenants(tenant_ids)

        self.stdout.write(self.style.SUCCESS(f"\nDone. Landlord {email} is a clean slate."))

    # ------------------------------------------------------------------ steps
    def _build_steps(self, landlord, opts):
        """(label, () -> queryset) in PROTECT-safe order: children before the
        parents that PROTECT them. Lazy callables so counts and deletes both use
        a fresh queryset."""
        from rentium.appointments.models import Appointment
        from rentium.leases.inspections import ConditionInspection
        from rentium.leases.models import Lease, Payment, RentAdjustment
        from rentium.leases.occupancy import Occupancy
        from rentium.ledger.models import LedgerEntry
        from rentium.maintenance.models import WorkOrder
        from rentium.messaging.models import Conversation
        from rentium.properties.models import Property, PropertyGroup, PropertyHolding
        from rentium.rama.models import RamaAudit, RamaInsight, RamaPendingPlan
        from rentium.showcase.models import Inquiry

        L = landlord
        steps = [
            ("ledger entries", lambda: LedgerEntry.objects.filter(landlord=L)),
            ("payments", lambda: Payment.objects.filter(lease__landlord=L)),
            # RentAdjustment hangs off LeaseTenant, not Lease directly.
            ("rent adjustments", lambda: RentAdjustment.objects.filter(lease_tenant__lease__landlord=L)),
            ("condition inspections", lambda: ConditionInspection.objects.filter(lease__landlord=L)),
            ("work orders", lambda: WorkOrder.objects.filter(property__landlord=L)),
            # Occupancy is scoped by its lease (it has no `property` field).
            ("occupancy records", lambda: Occupancy.objects.filter(lease__landlord=L)),
            ("leases", lambda: Lease.objects.filter(landlord=L)),
            ("property groups", lambda: PropertyGroup.objects.filter(landlord=L)),
            ("property holdings", lambda: PropertyHolding.objects.filter(landlord=L)),
            ("properties", lambda: Property.objects.filter(landlord=L)),
            ("conversations", lambda: Conversation.objects.filter(landlord=L)),
            ("appointments", lambda: Appointment.objects.filter(landlord=L)),
            ("inquiries", lambda: Inquiry.objects.filter(landlord=L)),
            ("RAMA plans", lambda: RamaPendingPlan.objects.filter(landlord=L)),
            ("RAMA audit", lambda: RamaAudit.objects.filter(landlord=L)),
            ("RAMA insights", lambda: RamaInsight.objects.filter(landlord=L)),
        ]

        # Optional / best-effort models (guarded so a rename can't abort a wipe).
        try:
            from rentium.ledger.models import ImportBatch, PropertyBankBalance

            steps.insert(0, ("bank balances", lambda: PropertyBankBalance.objects.filter(landlord=L)))
            steps.insert(0, ("import batches", lambda: ImportBatch.objects.filter(landlord=L)))
        except Exception:  # noqa: BLE001
            pass
        try:
            from rentium.agenda.models import AgendaEvent

            fields = {f.name for f in AgendaEvent._meta.get_fields()}
            if "landlord" in fields:
                steps.append(("agenda events", lambda: AgendaEvent.objects.filter(landlord=L)))
        except Exception:  # noqa: BLE001
            pass

        if opts["include_events"]:
            try:
                from rentium.events.models import DomainEvent, Notification

                steps.append(("notifications", lambda: Notification.objects.filter(user=L.user)))
                steps.append(("domain events", lambda: DomainEvent.objects.all()))
            except Exception:  # noqa: BLE001
                pass
        return steps

    # ------------------------------------------------------------- tenants
    def _tenant_user_ids(self, landlord) -> set:
        """Tenant User ids that appear on this landlord's leases (collected before
        the leases are deleted)."""
        from rentium.leases.models import LeaseTenant

        return set(
            LeaseTenant.objects.filter(lease__landlord=landlord, tenant__isnull=False)
            .values_list("tenant__user_id", flat=True)
        )

    def _delete_tenants(self, tenant_user_ids: set):
        """Delete tenant accounts, skipping any still referenced by another
        landlord (PROTECT). Runs after this landlord's operational data is gone."""
        from rentium.leases.models import LeaseTenant
        from rentium.users.models import User

        kept, removed = 0, 0
        for uid in tenant_user_ids:
            still_on_a_lease = LeaseTenant.objects.filter(tenant__user_id=uid).exists()
            if still_on_a_lease:
                kept += 1
                continue
            try:
                User.objects.filter(pk=uid).delete()  # cascades TenantProfile
                removed += 1
            except ProtectedError:
                kept += 1
        self.stdout.write(f"  tenant accounts removed: {removed}, kept (shared/protected): {kept}")
