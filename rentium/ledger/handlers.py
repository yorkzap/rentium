"""
Reactions to ledger events. Registered by LedgerConfig.ready().

Why these live here and not inside services.py:

services.record_payment() records the fact that money arrived. That write must
succeed, commit, and be done. Anything that happens *because* money arrived —
stamping a date on a lease, sending an email, recomputing a balance — is a
consequence, and consequences fail in ways the original fact must not care about.
ATOMIC_REQUESTS is on, so a consequence raising inside the service call would roll
back the payment itself: the landlord clicks "Record payment", sees a 500, and the
e-transfer that is sitting in their actual bank account is now absent from the
ledger. The event goes to the outbox, the payment commits, and the consequence
retries downstream where a failure is a retry rather than a lost payment.
"""

import logging

from rentium.events.registry import on

logger = logging.getLogger(__name__)


@on("ledger.payment_posted")
def stamp_deposit_receipt_date(event):
    """
    A deposit has been fully paid -> record the DATE it was received on the lease.

    Lease.security_deposit_received_date and pet_deposit_received_date have existed
    on AgreementTerms since the agreement rewrite, they already print on every lease
    document, and until now literally nothing ever set them — so every agreement this
    app has produced says "Received on: Not yet received", forever, including for
    deposits that were paid months ago.

    They are not decoration. The date a landlord RECEIVES a deposit is what starts the
    statutory clock for returning it at the end of the tenancy (15 days from the later
    of the tenancy ending and the tenant's forwarding address arriving, under the BC
    RTA). A lease that can't say when the deposit was received is a lease that loses
    that argument.
    """
    from .billing import stamp_deposit_received
    from .models import LedgerEntry

    charge_id = (event.payload or {}).get("charge_id")
    if not charge_id:
        return

    charge = LedgerEntry.objects.filter(pk=charge_id).select_related("lease").first()
    if not charge:
        logger.info("ledger.payment_posted for a missing charge (%s)", charge_id)
        return

    stamp_deposit_received(charge)
