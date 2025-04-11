from rest_framework.permissions import IsAuthenticated

class IsLandlordOwner(IsAuthenticated):
    """
    Permission to check if the user is a landlord who owns the object.
    """
    def has_object_permission(self, request, view, obj):
        if not hasattr(request.user, 'landlord_profile'):
            return False
            
        # Check ownership based on object type
        if hasattr(obj, 'landlord'):
            return obj.landlord == request.user.landlord_profile
        elif hasattr(obj, 'lease') and hasattr(obj.lease, 'landlord'):
            return obj.lease.landlord == request.user.landlord_profile
        elif hasattr(obj, 'payment') and hasattr(obj.payment, 'lease'):
            return obj.payment.lease.landlord == request.user.landlord_profile
            
        return False

class IsLandlordOrTenantMember(IsAuthenticated):
    """
    Permission to allow:
    - Landlords who own the object
    - Tenants who are members of the lease
    """
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # Find the associated lease for various object types
        target_lease = None
        if hasattr(obj, 'landlord') and hasattr(obj, 'lease_tenants'):  # It's a Lease
            target_lease = obj
        elif hasattr(obj, 'lease'):  # It's a LeaseTenant, LeaseDocument, or Payment
            target_lease = obj.lease
        elif hasattr(obj, 'payment') and hasattr(obj.payment, 'lease'):  # It's a PaymentReminder
            target_lease = obj.payment.lease
            
        if not target_lease:
            return False
            
        # Check landlord permission
        if hasattr(user, 'landlord_profile'):
            return target_lease.landlord == user.landlord_profile
            
        # Check tenant permission
        elif hasattr(user, 'tenant_profile'):
            # Check if tenant is part of the lease
            is_lease_member = target_lease.lease_tenants.filter(tenant=user.tenant_profile).exists()
            
            if is_lease_member:
                # Extra check: If accessing a specific tenant or payment record, ensure it's theirs
                if hasattr(obj, 'tenant') and hasattr(obj.tenant, 'id'):
                    return obj.tenant.id == user.tenant_profile.id
                    
                # For other objects, allow access if they're on the lease
                return True
                
        return False