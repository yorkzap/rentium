"""Regression: rentium/ledger/signals.py must actually be connected.

LedgerConfig.ready() imported `handlers` but not `signals`, and nothing else
imported the module, so every receiver in it was silently dead. The damage was
invisible because each receiver swallows its own exceptions — there was no
error to notice, the hooks simply never ran:

  - apply_rent_adjustment    rent adjustments never reconciled into the ledger
  - seed_group_areas         areas never seeded (8 groups existed, 0 areas)
  - seed_property_areas      ditto for standalone complete units
  - close_occupancies_on_end terminated/expired leases left occupancy rows open
  - sync_tenant_occupancy    roommates joining an ACTIVE lease got no occupancy
                             row and no rent charge

Asserting on dispatch_uid (rather than calling the hooks) keeps this test about
the one thing that broke: the module being imported at startup. Every receiver
below declares an explicit dispatch_uid, so a renamed or dropped receiver fails
here loudly.
"""

from django.db.models.signals import post_save

# (dispatch_uid, sender label) for every receiver declared in signals.py.
EXPECTED_RECEIVERS = [
    ("adjustment_to_ledger", "leases.RentAdjustment"),
    ("seed_group_areas", "properties.PropertyGroup"),
    ("seed_property_areas", "properties.Property"),
    ("close_occupancies_on_end", "leases.Lease"),
    ("sync_tenant_occupancy", "leases.LeaseTenant"),
]


def _connected_dispatch_uids():
    """dispatch_uids currently registered on post_save.

    Django keys receivers by (dispatch_uid_or_id, sender_id); a receiver
    registered with an explicit dispatch_uid stores that string verbatim.
    Entry shape widened in Django 5 (lookup_key, receiver, is_async), so index
    rather than unpack.
    """
    return {
        entry[0][0] for entry in post_save.receivers if isinstance(entry[0][0], str)
    }


def test_ledger_signals_module_is_imported_at_startup():
    """The whole bug in one assertion: if ready() stops importing signals,
    nothing else will, and every hook below goes quietly dead."""
    connected = _connected_dispatch_uids()
    missing = [uid for uid, _ in EXPECTED_RECEIVERS if uid not in connected]
    assert not missing, (
        f"ledger signal receivers not connected: {missing}. "
        "LedgerConfig.ready() must import rentium.ledger.signals."
    )


def test_every_expected_receiver_is_individually_connected():
    """Names each receiver separately so a failure says which hook is dead."""
    connected = _connected_dispatch_uids()
    for uid, sender in EXPECTED_RECEIVERS:
        assert uid in connected, f"{uid} (sender {sender}) is not connected"
