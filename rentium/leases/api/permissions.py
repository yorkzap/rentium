from rest_framework.permissions import BasePermission
from rest_framework.permissions import IsAuthenticated


def _resolve_lease(obj):
    """
    Given any of Lease / LeaseTenant / LeaseDocument / Payment / PaymentReminder /
    RentAdjustment, walk to the parent Lease. Returns None if it can't be resolved.
    """
    # It's a Lease itself
    if hasattr(obj, "landlord") and hasattr(obj, "lease_tenants"):
        return obj

    # It's a RentAdjustment -> lease_tenant -> lease
    if hasattr(obj, "lease_tenant"):
        return obj.lease_tenant.lease

    # It's a LeaseTenant / LeaseDocument / Payment -> lease
    if hasattr(obj, "lease"):
        return obj.lease

    # It's a PaymentReminder -> payment -> lease
    if hasattr(obj, "payment") and hasattr(obj.payment, "lease"):
        return obj.payment.lease

    return None


class IsLandlordOwner(IsAuthenticated):
    """
    Permission to check if the user is a landlord who owns the object
    (directly, or via the object's parent lease).
    """

    def has_object_permission(self, request, view, obj):
        if not hasattr(request.user, "landlord_profile"):
            return False

        lease = _resolve_lease(obj)
        if lease is None:
            return False

        return lease.landlord == request.user.landlord_profile


class IsLandlordOrTenantMember(IsAuthenticated):
    """
    Permission to allow:
    - Landlords who own the object's lease
    - Tenants who are linked members of the lease (LeaseTenant.tenant matches)
    - Tenants with a *pending invite* on the lease that matches their account
      email, so they can view/claim/sign before their TenantProfile is linked
    """

    def has_object_permission(self, request, view, obj):
        user = request.user
        lease = _resolve_lease(obj)

        if lease is None:
            return False

        # Owner OR a co-landlord granted access to this lease (property/lease-
        # scoped, with or without their own portfolio). Without this a co-landlord
        # could see the lease in their list but 403 on opening it.
        from rentium.users.access import accessible_leases

        if accessible_leases(user).filter(pk=lease.pk).exists():
            return True
        if hasattr(user, "landlord_profile"):
            return lease.landlord == user.landlord_profile

        # Tenant path
        if hasattr(user, "tenant_profile"):
            tenant_profile = user.tenant_profile

            is_linked_member = lease.lease_tenants.filter(
                tenant=tenant_profile
            ).exists()
            is_invited_member = lease.lease_tenants.filter(
                tenant__isnull=True, invited_email__iexact=user.email
            ).exists()

            if not (is_linked_member or is_invited_member):
                return False

            # Extra scoping: if the object itself carries a `tenant` FK (LeaseTenant,
            # Payment, RentAdjustment via lease_tenant), make sure it's actually theirs
            # and not just any tenant's record on a lease they happen to share.
            if hasattr(obj, "tenant") and obj.tenant_id is not None:
                return obj.tenant_id == tenant_profile.id

            if hasattr(obj, "lease_tenant"):
                lt = obj.lease_tenant
                if lt.tenant_id is not None:
                    return lt.tenant_id == tenant_profile.id
                return lt.invited_email.lower() == user.email.lower()

            if hasattr(obj, "invited_email"):  # obj is a LeaseTenant itself
                if obj.tenant_id is not None:
                    return obj.tenant_id == tenant_profile.id
                return obj.invited_email.lower() == user.email.lower()

            # Otherwise (Lease, LeaseDocument, etc.) — membership on the lease is enough
            return True

        return False


class LeaseNotLocked(BasePermission):
    """
    Blocks unsafe methods on a Lease (or anything hanging off one) once
    Lease.is_locked() is True — i.e. ACTIVE or beyond. Read access is always
    allowed; past that point only Django admin can modify the lease.

    Stack this alongside IsLandlordOwner / IsLandlordOrTenantMember, it does
    not replace ownership checks.
    """

    message = "This lease has been fully executed and can no longer be edited here."

    SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

    def has_object_permission(self, request, view, obj):
        if request.method in self.SAFE_METHODS:
            return True

        lease = _resolve_lease(obj)
        if lease is None:
            return True  # not lease-related; let other permission classes decide

        return not lease.is_locked()


class LeaseNotLockedOrAmendable(LeaseNotLocked):
    """LeaseNotLocked, except that a LIVE tenancy may have its wording amended.

    Freezing an executed lease completely was right for the deal and wrong for
    everything around it — a landlord who agreed a new house rule, or whose
    service address changed, had no route but Django admin. Tenancies really do
    get amended by agreement, and BC expects the current terms to be recorded.

    Only for the Lease endpoint, and only for the fields in
    `AMENDABLE_WHEN_ACTIVE` — rent, deposits and dates stay frozen because
    ledger charges and statutory clocks are already running against them.
    Anything past ACTIVE stays frozen entirely: those records are what a
    dispute is argued from.

    The real enforcement is in `services.update_lease_record`, the boundary the
    API and RAMA share. This exists so the request is refused at the door with
    a useful message rather than deep inside a serializer.
    """

    def has_object_permission(self, request, view, obj):
        from rentium.leases.services import amendable_fields_for

        if super().has_object_permission(request, view, obj):
            return True

        lease = _resolve_lease(obj)
        if lease is None or lease is not obj:
            # Only the lease itself is amendable this way. Anything hanging off
            # it (a tenant slot, a document) keeps the strict rule.
            return False

        allowed = amendable_fields_for(lease)
        if not allowed:
            return False
        if request.method != "PATCH":
            # A PUT replaces the whole record, which on a live lease means
            # sending the frozen fields back too. Amendments are partial.
            self.message = (
                "This lease is active. Send just the wording you are changing "
                "as a PATCH — a full replace would rewrite terms that have "
                "charges and notice periods running against them."
            )
            return False

        refused = sorted(set(request.data or {}) - allowed)
        if refused:
            self.message = (
                f"This lease is active, so its wording can be amended but the "
                f"deal cannot: {', '.join(refused)}. Use a rent adjustment, or "
                f"terminate and re-issue, so the ledger moves with the change."
            )
            return False
        return True
