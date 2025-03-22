from django.db import transaction
from rest_framework import serializers

from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile
from rentium.users.models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "name", "email", "phone", "user_type", "url"]

        extra_kwargs = {
            "url": {"view_name": "api:user-detail", "lookup_field": "pk"},
        }


class LandlordProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = LandlordProfile
        fields = ["id", "user", "province", "country"]


class TenantProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = TenantProfile
        fields = ["id", "user"]


class UserRegistrationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    name = serializers.CharField(max_length=255)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    user_type = serializers.ChoiceField(choices=User.UserType.choices)

    # Fields for landlord profile
    province = serializers.CharField(max_length=100, required=False)
    country = serializers.CharField(max_length=100, required=False)

    def validate(self, data):
        # Validate that province and country are provided for landlords
        if data.get("user_type") == User.UserType.LANDLORD:
            if not data.get("province"):
                raise serializers.ValidationError(
                    {"province": "Province is required for landlords"}
                )
            if not data.get("country"):
                raise serializers.ValidationError(
                    {"country": "Country is required for landlords"}
                )
        return data

    @transaction.atomic
    def create(self, validated_data):
        # Extract profile data
        province = validated_data.pop("province", None)
        country = validated_data.pop("country", None)

        # Create user
        user = User.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            name=validated_data.get("name", ""),
            phone=validated_data.get("phone", ""),
            user_type=validated_data["user_type"],
        )

        # Create profile based on user type
        if user.user_type == User.UserType.LANDLORD:
            LandlordProfile.objects.create(
                user=user, province=province, country=country
            )
        elif user.user_type == User.UserType.TENANT:
            TenantProfile.objects.create(user=user)

        return user
