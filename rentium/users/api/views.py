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
from rest_framework.decorators import throttle_classes
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.mixins import UpdateModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from rentium.users.models import LandlordProfile
from rentium.users.models import TenantProfile
from rentium.users.models import User
from rentium.users.services import PasswordResetError
from rentium.users.services import confirm_password_reset
from rentium.users.services import request_password_reset

from .serializers import LandlordProfileSerializer
from .serializers import TenantProfileSerializer
from .serializers import UserRegistrationSerializer
from .serializers import UserSerializer


class PasswordResetThrottle(AnonRateThrottle):
    """Tight limit: password-reset emails are easy to abuse for spam."""

    scope = "password_reset"


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
                    "detail": (
                        "Email not verified. Check your inbox for the "
                        "verification link, or resend it from this screen."
                    ),
                    "code": "EMAIL_NOT_VERIFIED",
                    "email": user.email,
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

    @action(detail=False, methods=["get", "patch"])
    def me(self, request):
        """
        GET returns the current user's profile. PATCH updates it — only
        `name` and `phone` are actually writable (see UserSerializer's
        read_only_fields for why email/user_type aren't editable here).

        This was previously GET-only (no `methods=` on the @action decorator
        defaults to GET), which is why phone-number edits from the frontend
        were silently discarded — the request never reached this branch at
        all, DRF rejected it with 405 before the view code ever ran.
        """
        if request.method == "PATCH":
            serializer = UserSerializer(
                request.user,
                data=request.data,
                partial=True,
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(status=status.HTTP_200_OK, data=serializer.data)

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
        if not serializer.is_valid():
            # Flatten field errors into a clear client-facing shape.
            errors = serializer.errors
            # Prefer first email/phone message when present.
            for field in ("email", "phone", "password", "name"):
                if field in errors:
                    msg = errors[field]
                    if isinstance(msg, (list, tuple)):
                        msg = msg[0]
                    return Response(
                        {
                            "error": str(msg),
                            "code": f"{field.upper()}_INVALID",
                            "field": field,
                            "errors": errors,
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            return Response(
                {"error": "Registration failed.", "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user = serializer.save()
            EmailAddress.objects.get_or_create(
                user=user,
                email=user.email,
                defaults={"primary": True, "verified": False},
            )
            email_sent = True
            email_error = ""
            try:
                send_email_confirmation(request, user, signup=True)
            except Exception as exc:  # noqa: BLE001 — account exists; mail is separate
                email_sent = False
                email_error = str(exc)[:300]
                import logging

                logging.getLogger(__name__).exception(
                    "registration_email_failed user_id=%s", user.pk
                )

            return Response(
                {
                    "message": (
                        "Account created. Check your email for a verification link."
                        if email_sent
                        else (
                            "Account created, but we could not send the verification "
                            "email yet (mail provider not fully set up). You can resend "
                            "from the login page once email is working."
                        )
                    ),
                    "user_id": user.id,
                    "email_sent": email_sent,
                    "email_error": email_error or None,
                    "code": "REGISTERED" if email_sent else "REGISTERED_EMAIL_PENDING",
                },
                status=status.HTTP_201_CREATED,
            )
        except IntegrityError as e:
            err = str(e)
            if "users_user_email_key" in err or "email" in err.lower():
                return Response(
                    {
                        "error": (
                            "An account with this email already exists. "
                            "Try logging in or Forgot password."
                        ),
                        "code": "EMAIL_EXISTS",
                        "field": "email",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            if "phone" in err.lower():
                return Response(
                    {
                        "error": "This phone number is already used on another account.",
                        "code": "PHONE_EXISTS",
                        "field": "phone",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                {"error": "Registration failed due to a database constraint."},
                status=status.HTTP_400_BAD_REQUEST,
            )


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


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_request_view(request):
    """
    Request a password-reset email. Always returns the same success message so
    the endpoint cannot be used to discover whether an email is registered.
    """
    email = (request.data.get("email") or "").strip()
    if not email:
        return Response(
            {"detail": "Email is required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        request_password_reset(email)
    except Exception:
        # Log server-side but still return the generic client message — a
        # SendGrid blip shouldn't confirm "this email exists" either.
        import logging

        logging.getLogger(__name__).exception("password_reset_request failed")
    return Response(
        {
            "message": (
                "If an account exists for that email, we've sent a reset link. "
                "Check your inbox (and spam) in a minute or two."
            )
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def password_reset_confirm_view(request):
    """
    Complete a password reset using the uid + token from the email link.
    """
    uid = request.data.get("uid") or ""
    token = request.data.get("token") or ""
    new_password = request.data.get("password") or request.data.get("new_password") or ""

    try:
        confirm_password_reset(uid=uid, token=token, new_password=new_password)
    except PasswordResetError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {"message": "Password updated. You can log in with your new password."},
        status=status.HTTP_200_OK,
    )
