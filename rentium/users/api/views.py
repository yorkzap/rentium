# Import allauth modules for email verification
from allauth.account.models import EmailAddress
from allauth.account.models import EmailConfirmation
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.utils import send_email_confirmation
from django.db import IntegrityError
from rest_framework import status
from rest_framework import viewsets
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.decorators import action
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
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


class CustomObtainAuthToken(ObtainAuthToken):
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]

        # Check if user's email is verified using EmailAddress model
        from allauth.account.models import EmailAddress

        email_verified = EmailAddress.objects.filter(
            user=user, primary=True, verified=True
        ).exists()

        if not email_verified:
            return Response(
                {
                    "detail": "Email not verified. Please verify your email before logging in."
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        token, created = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})


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
                # Create user from serializer
                user = serializer.save()

                # Create an email address instance for the user
                email_address, created = EmailAddress.objects.get_or_create(
                    user=user,
                    email=user.email,
                    defaults={"primary": True, "verified": False},
                )

                # Send confirmation email
                send_email_confirmation(request, user, signup=True)

                return Response(
                    {
                        "message": "User registered successfully. Please check your email for verification instructions.",
                        "user_id": user.id,
                    },
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


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_email_confirm(request):
    """
    Custom API endpoint to handle email verification from the frontend.
    """
    key = request.data.get("key")
    if not key:
        return Response(
            {"detail": "Missing verification key"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # First try with HMAC (more secure)
        confirmation = EmailConfirmationHMAC.from_key(key)
        if not confirmation:
            # Fall back to regular confirmation
            try:
                confirmation = EmailConfirmation.objects.get(key=key)
            except EmailConfirmation.DoesNotExist:
                return Response(
                    {"detail": "Invalid verification key"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        confirmation.confirm(request)
        return Response({"detail": "Email successfully verified"})
    except Exception as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes([AllowAny])
def resend_verification_email(request):
    """
    Resend the verification email to an unverified user.
    """
    email = request.data.get("email")
    if not email:
        return Response(
            {"detail": "Email is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    try:
        user = User.objects.get(email=email)
        email_address = EmailAddress.objects.filter(user=user, primary=True).first()

        # Check if email is already verified
        if email_address and email_address.verified:
            return Response(
                {"message": "This email is already verified"}, status=status.HTTP_200_OK
            )

        # Send verification email using allauth
        send_email_confirmation(request, user, signup=False)

        return Response(
            {"message": "Verification email sent. Please check your inbox."},
            status=status.HTTP_200_OK,
        )
    except User.DoesNotExist:
        # For security reasons, don't reveal that the email doesn't exist
        return Response(
            {"message": "If this email exists, a verification link has been sent"},
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        print(f"Error sending verification email: {str(e)}")
        return Response(
            {"detail": "An error occurred while sending the verification email."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
