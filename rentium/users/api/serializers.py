from django.db import transaction
from rest_framework import serializers

from rentium.core.phone import to_e164
from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile
from rentium.users.models import User


# -----------------------------------------------------------------------------
# USER SERIALIZERS
# -----------------------------------------------------------------------------
class UserSerializer(serializers.ModelSerializer):
    """
    Serializer for the User model.

    Used for:
    - GET /api/users/me/ - Returns the current authenticated user's details
    - PATCH /api/users/me/ - Updates name/phone for the current user
    - GET /api/users/{id}/ - Returns a specific user's details

    Permissions:
    - IsAuthenticated: Only authenticated users can access user data

    `email` and `user_type` are intentionally read-only here, even on PATCH:
    - Changing email needs its own re-verification flow (it's also the
      USERNAME_FIELD used to log in) — not something to allow as a side
      effect of a general profile-update endpoint.
    - Changing user_type after the account exists would orphan whichever of
      LandlordProfile/TenantProfile no longer matches — that's a data
      migration, not a profile edit.
    """

    class Meta:
        model = User
        fields = ["id", "name", "email", "phone", "user_type"]
        read_only_fields = ["id", "email", "user_type"]


class LandlordProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for the LandlordProfile model.

    Used for:
    - GET /api/users/landlord-profile/ - Retrieves landlord profile for current user
    - PUT/PATCH /api/users/landlord-profile/{id}/ - Updates landlord profile

    Features:
    - Includes related user email and name as read-only fields
    - Province and country are editable fields

    Permissions:
    - IsAuthenticated: Users can only access/modify their own profile
    """

    # Include read-only user fields from the related User model
    email = serializers.EmailField(source="user.email", read_only=True)
    name = serializers.CharField(source="user.name", read_only=True)

    class Meta:
        model = LandlordProfile
        fields = ["id", "email", "name", "province", "country"]


class TenantProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for the TenantProfile model.

    Used for:
    - GET /api/users/tenant-profile/ - Retrieves tenant profile for current user
    - PUT/PATCH /api/users/tenant-profile/{id}/ - Updates tenant profile

    Features:
    - Includes related user email and name as read-only fields

    Permissions:
    - IsAuthenticated: Users can only access/modify their own profile
    """

    # Include read-only user fields from the related User model
    email = serializers.EmailField(source="user.email", read_only=True)
    name = serializers.CharField(source="user.name", read_only=True)

    class Meta:
        model = TenantProfile
        fields = ["id", "email", "name"]


# -----------------------------------------------------------------------------
# REGISTRATION SERIALIZER
# -----------------------------------------------------------------------------
class UserRegistrationSerializer(serializers.ModelSerializer):
    """
    Serializer for user registration.

    Used for:
    - POST /api/users/register/ - Register a new user

    Example request body:
    {
        "email": "user@example.com",
        "password": "securepassword123",
        "name": "John Doe",
        "phone": "+123456789",
        "user_type": "LANDLORD",
        "province": "Ontario",  // Optional fields for landlords
        "country": "Canada"     // Optional fields for landlords
    }

    Features:
    - Creates a new user with appropriate profile (Landlord or Tenant)
    - Handles required fields based on user type
    - Securely handles password (write-only)

    Permissions:
    - AllowAny: Anyone can register

    Notes:
    - For Landlord profiles, province and country are required
    - For Tenant profiles, province and country are ignored
    - Phone number is required for all users
    - Email must be unique in the system
    """

    # Fields specific to the registration process
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        help_text="User password - must be secure and will be hashed in storage",
    )
    user_type = serializers.ChoiceField(
        choices=User.UserType.choices,
        help_text="Select whether the user is a LANDLORD or TENANT",
    )

    # Landlord-specific fields (optional for all users)
    province = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional field for landlords. Province/state of operation",
    )
    country = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional field for landlords. Country of operation",
    )

    class Meta:
        model = User
        fields = [
            "email",
            "password",
            "name",
            "phone",
            "user_type",
            "province",
            "country",
        ]
        extra_kwargs = {
            "phone": {
                "required": True,
                "help_text": "Phone number with country code (e.g., +1234567890)",
            },
            "email": {"help_text": "Email address (must be unique)"},
            "name": {"help_text": "Full name of the user"},
        }

    def validate_email(self, value):
        email = (value or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "An account with this email already exists. Try logging in, "
                "or use Forgot password if you need to reset it."
            )
        return email

    def validate_phone(self, value):
        """Normalize to E.164 and reject numbers already on another account."""
        from django.core.exceptions import ValidationError as DjangoValidationError

        try:
            e164 = to_e164(value)
        except DjangoValidationError as exc:
            msg = exc.messages[0] if getattr(exc, "messages", None) else str(exc)
            raise serializers.ValidationError(msg) from exc
        if not e164:
            raise serializers.ValidationError("Phone number is required.")
        if User.objects.filter(phone=e164).exists():
            raise serializers.ValidationError(
                "This phone number is already used on another Rentium account."
            )
        return e164

    def validate(self, data):
        return data

    def create(self, validated_data):
        """
        Creates a new user with the appropriate profile type.

        Process:
        1. Extract profile-specific data (user_type, province, country)
        2. Create the User instance with create_user (handles password hashing)
        3. Create the appropriate profile based on user_type
        4. Return the created User instance

        Note: This method is wrapped in a transaction to ensure data integrity
        """
        user_type = validated_data.pop("user_type")
        province = validated_data.pop("province", "")
        country = validated_data.pop("country", "")

        with transaction.atomic():
            user = User.objects.create_user(
                email=validated_data["email"],
                password=validated_data["password"],
                name=validated_data.get("name", ""),
                phone=validated_data.get("phone", ""),
                user_type=user_type,
            )

            if user_type == User.UserType.LANDLORD:
                LandlordProfile.objects.create(
                    user=user, province=province, country=country
                )
            elif user_type == User.UserType.TENANT:
                TenantProfile.objects.create(user=user)

        return user
