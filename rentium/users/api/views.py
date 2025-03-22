from django.db import IntegrityError
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.mixins import UpdateModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile
from rentium.users.models import User

from .serializers import LandlordProfileSerializer
from .serializers import TenantProfileSerializer
from .serializers import UserRegistrationSerializer
from .serializers import UserSerializer


class UserViewSet(RetrieveModelMixin, ListModelMixin, UpdateModelMixin, GenericViewSet):
    serializer_class = UserSerializer
    queryset = User.objects.all()
    lookup_field = "pk"
    permission_classes = [IsAuthenticated]

    def get_queryset(self, *args, **kwargs):
        assert isinstance(self.request.user.id, int)
        return self.queryset.filter(id=self.request.user.id)

    @action(detail=False)
    def me(self, request):
        serializer = UserSerializer(request.user, context={"request": request})
        return Response(status=status.HTTP_200_OK, data=serializer.data)


class LandlordProfileViewSet(viewsets.ModelViewSet):
    serializer_class = LandlordProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return LandlordProfile.objects.filter(user=self.request.user)


class TenantProfileViewSet(viewsets.ModelViewSet):
    serializer_class = TenantProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return TenantProfile.objects.filter(user=self.request.user)


class UserRegistrationView(APIView):
    """
    API endpoint for user registration.
    """

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = UserRegistrationSerializer(data=request.data)

        if serializer.is_valid():
            try:
                user = serializer.save()
                return Response(
                    {"message": "User registered successfully", "user_id": user.id},
                    status=status.HTTP_201_CREATED,
                )
            except IntegrityError as e:
                # Check if this is a duplicate email error
                if "users_user_email_key" in str(e):
                    return Response(
                        {"error": "A user with this email already exists."},
                        status=status.HTTP_409_CONFLICT,
                    )
                # Handle other integrity errors
                return Response(
                    {"error": "Registration failed due to a database constraint."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
