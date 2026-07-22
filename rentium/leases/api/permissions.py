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

        # Landlord who owns the lease — or a co-landlord granted access to it
        # (property/lease-scoped). Without the second check a co-landlord could
        # see the lease in their list but 403 on opening it.
        if hasattr(user, "landlord_profile"):
            if lease.landlord == user.landlord_profile:
                return True
            from rentium.users.access import accessible_leases

            return accessible_leases(user).filter(pk=lease.pk).exists()

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
