from django.db import transaction
from rest_framework import serializers

from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile
from rentium.users.models import User

# Define user type choices here
USER_TYPE_CHOICES = [
    ("LANDLORD", "Landlord"),
    ("TENANT", "Tenant"),
]


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "name", "email"]


class LandlordProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    name = serializers.CharField(source="user.name", read_only=True)

    class Meta:
        model = LandlordProfile
        fields = ["id", "email", "name", "province", "country"]


class TenantProfileSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    name = serializers.CharField(source="user.name", read_only=True)

    class Meta:
        model = TenantProfile
        fields = ["id", "email", "name"]


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)
    user_type = serializers.ChoiceField(choices=USER_TYPE_CHOICES)
    province = serializers.CharField(required=False, allow_blank=True)
    country = serializers.CharField(required=False, allow_blank=True)

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
            "phone": {"required": True},  # Make phone required
        }

    def create(self, validated_data):
        user_type = validated_data.pop("user_type")
        province = validated_data.pop("province", "")
        country = validated_data.pop("country", "")

        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            name=validated_data.get("name", ""),
            phone=validated_data.get("phone", ""),
            user_type=user_type,
        )

        if user_type == "LANDLORD":
            LandlordProfile.objects.create(
                user=user, province=province, country=country
            )
        elif user_type == "TENANT":
            TenantProfile.objects.create(user=user)

        return user
