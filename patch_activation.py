"""
One-shot patcher for rentium/leases/models.py.

    python patch_activation.py rentium/leases/models.py

Inserts the ledger + occupancy + event hook into Lease.check_and_activate(),
right after clip_overlapping_month_to_month_leases(). Idempotent: running it
twice does nothing the second time. Makes a .bak backup first.
"""

import sys
from pathlib import Path

ANCHOR = (
    "            self.clip_overlapping_month_to_month_leases()\n            return True"
)

INSERT = """            self.clip_overlapping_month_to_month_leases()

            # --- Ledger + occupancy + event (deferred imports avoid cycles) ---
            # Runs exactly once: the guard at the top of this method returns
            # early unless status was PENDING, and generation is idempotent.
            from rentium.events.registry import publish
            from rentium.ledger.billing import generate_initial_charges
            from rentium.leases.occupancy import open_occupancy

            generate_initial_charges(self)  # deposits, fees, prorated rent schedule
            for lt in self.lease_tenants.filter(tenant__isnull=False, declined=False):
                open_occupancy(lt)          # start the "who lived where when" log
            publish(
                "lease.activated",
                {"lease_id": str(self.pk)},
                property_id=self.property_id,
                lease_id=self.pk,
            )
            return True"""


def main():
    if len(sys.argv) != 2:
        print("Usage: python patch_activation.py rentium/leases/models.py")
        sys.exit(1)

    path = Path(sys.argv[1])
    src = path.read_text()

    if "generate_initial_charges(self)" in src:
        print("Already patched — nothing to do.")
        return

    if ANCHOR not in src:
        print(
            "Could not find the check_and_activate anchor. Is this the right file / unmodified?"
        )
        print("Expected to find this block:\n")
        print(ANCHOR)
        sys.exit(1)

    # Only replace the FIRST occurrence (the one inside check_and_activate).
    patched = src.replace(ANCHOR, INSERT, 1)

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(src)
    path.write_text(patched)
    print(f"Patched {path}")
    print(f"Backup saved to {backup}")


if __name__ == "__main__":
    main()
