"""User-facing auth helpers (password reset, etc.).

Kept in the service layer so views stay thin and the same functions can later
power management commands or RAMA tools without re-implementing email/token
logic in multiple places.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.utils.encoding import force_bytes
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.utils.http import urlsafe_base64_encode

from rentium.users.models import User

logger = logging.getLogger(__name__)


class PasswordResetError(Exception):
    """Invalid or expired reset token / uid."""


def _frontend_reset_url(uid: str, token: str) -> str:
    base = getattr(settings, "FRONTEND_URL", "http://localhost:3000").rstrip("/")
    return f"{base}/auth/reset-password?uid={uid}&token={token}"


def request_password_reset(email: str) -> None:
    """Send a password-reset link if the email belongs to a user.

    Always a no-op from the caller's perspective when the email is unknown —
    do not raise, so API responses cannot be used to enumerate accounts.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return

    try:
        user = User.objects.get(email__iexact=normalized)
    except User.DoesNotExist:
        logger.info("password_reset_request unknown_email")
        return

    if not user.has_usable_password():
        # Invite-only / social accounts without a local password — still send
        # nothing useful to attackers, but log for ops.
        logger.info("password_reset_request unusable_password user_id=%s", user.pk)
        return

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    reset_url = _frontend_reset_url(uid, token)

    subject = "Reset your Rentium password"
    text_body = (
        "You're receiving this because a password reset was requested for "
        f"your Rentium account ({user.email}).\n\n"
        f"Open this link to choose a new password:\n{reset_url}\n\n"
        "This link expires after a short time and can only be used once.\n"
        "If you didn't request a reset, you can ignore this email — your "
        "password will stay the same.\n"
    )
    html_body = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Reset your password</title></head>
<body>
  <div style="max-width:600px;margin:0 auto;padding:20px;font-family:system-ui,sans-serif;">
    <h2 style="color:#0F172A;margin-bottom:20px;">Reset your Rentium password</h2>
    <p style="color:#475569;margin-bottom:20px;">
      We got a request to reset the password for <strong>{user.email}</strong>.
    </p>
    <p style="margin:30px 0;">
      <a href="{reset_url}"
         style="background-color:#0D9488;color:white;padding:12px 24px;text-decoration:none;border-radius:6px;display:inline-block;">
        Choose a new password
      </a>
    </p>
    <p style="color:#64748B;font-size:14px;">
      If the button doesn't work, copy and paste this link into your browser:<br>
      {reset_url}
    </p>
    <p style="color:#64748B;margin-top:40px;font-size:14px;">
      If you didn't request this, ignore this email — your password won't change.
    </p>
  </div>
</body>
</html>
"""

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)
    logger.info("password_reset_request sent user_id=%s", user.pk)


def confirm_password_reset(*, uid: str, token: str, new_password: str) -> User:
    """Validate uid+token and set a new password. Raises PasswordResetError on failure."""
    if not uid or not token or not new_password:
        raise PasswordResetError("Missing reset parameters.")

    try:
        user_id = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=user_id)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError) as exc:
        raise PasswordResetError("This reset link is invalid or has expired.") from exc

    if not default_token_generator.check_token(user, token):
        raise PasswordResetError("This reset link is invalid or has expired.")

    try:
        validate_password(new_password, user=user)
    except ValidationError as exc:
        # Surface the first validator message to the client.
        raise PasswordResetError("; ".join(exc.messages)) from exc

    user.set_password(new_password)
    user.save(update_fields=["password"])
    logger.info("password_reset_confirm success user_id=%s", user.pk)
    return user
