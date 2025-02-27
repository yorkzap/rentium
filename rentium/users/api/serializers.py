from rest_framework import serializers

from rentium.users.models import User, LandlordProfile, TenantProfile


class UserSerializer(serializers.ModelSerializer[User]):
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
