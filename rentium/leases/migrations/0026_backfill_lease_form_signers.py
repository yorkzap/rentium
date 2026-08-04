"""Give already-attached forms the signer rows they were created without.

Signer rows used to be created inside send_form, which meant a form sat there
with nobody bound to it and the landlord had no way to sign their own document
— the Sign button is drawn from exactly this data. attach_form now binds every
slot the lease roster can fill, but forms attached before that shipped are
stranded: no signers, no button, and a downloaded PDF with an empty signature
block and no way to change that.

Idempotent, and skips anything already executed or withdrawn — a completed
form's bytes are frozen and must not be reinterpreted.
"""

from __future__ import annotations

import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def bind_signers_on_existing_forms(apps, schema_editor):
    # The real model, not the historical one: binding reads the lease roster
    # through model properties (LeaseTenant.display_name) that a frozen model
    # does not carry. Safe here because this is a one-shot repair of data the
    # current code owns, and it is wrapped so a failure cannot block a deploy.
    from rentium.leases.form_services import bind_known_signers
    from rentium.leases.lease_forms import LeaseForm

    stranded = LeaseForm.objects.exclude(
        status__in=[LeaseForm.Status.COMPLETED, LeaseForm.Status.VOID]
    ).filter(signers__isnull=True)

    repaired = 0
    for form in stranded.select_related("lease", "template"):
        try:
            if bind_known_signers(form):
                repaired += 1
        except Exception:  # noqa: BLE001 — a bad row must not block the deploy
            logger.exception("could not bind signers on lease form %s", form.pk)
    if repaired:
        logger.info("bound signers on %s existing lease form(s)", repaired)


class Migration(migrations.Migration):
    dependencies = [
        ("leases", "0025_leaseform_leaseformsigner_leaseformsignature_and_more"),
    ]

    operations = [
        migrations.RunPython(
            bind_signers_on_existing_forms,
            # Nothing to undo: the rows are what the current code would create
            # anyway, and deleting them would strand the forms again.
            migrations.RunPython.noop,
        ),
    ]
