"""Password-reset service + API — token safety and non-enumeration."""

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from rentium.users.services import PasswordResetError
from rentium.users.services import confirm_password_reset
from rentium.users.services import request_password_reset
from rentium.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    """Password-reset endpoints are rate-limited; don't leak counters across tests."""
    cache.clear()


@pytest.mark.django_db
def test_request_password_reset_sends_email_for_known_user():
    user = UserFactory(email="landlord@example.com")
    request_password_reset("landlord@example.com")
    assert len(mail.outbox) == 1
    assert "Reset your Rentium password" in mail.outbox[0].subject
    assert user.email in mail.outbox[0].to
    assert "/auth/reset-password?" in mail.outbox[0].body


@pytest.mark.django_db
def test_request_password_reset_unknown_email_is_silent():
    request_password_reset("nobody@example.com")
    assert mail.outbox == []


@pytest.mark.django_db
def test_confirm_password_reset_sets_new_password():
    user = UserFactory(email="tenant@example.com")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    confirm_password_reset(uid=uid, token=token, new_password="BrandNew-Pass9!")
    user.refresh_from_db()
    assert user.check_password("BrandNew-Pass9!")


@pytest.mark.django_db
def test_confirm_password_reset_rejects_bad_token():
    user = UserFactory()
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    with pytest.raises(PasswordResetError):
        confirm_password_reset(uid=uid, token="not-a-real-token", new_password="BrandNew-Pass9!")


@pytest.mark.django_db
def test_password_reset_api_non_enumeration():
    client = APIClient()
    UserFactory(email="known@example.com")

    known = client.post(
        "/api/users/password-reset/",
        {"email": "known@example.com"},
        format="json",
    )
    unknown = client.post(
        "/api/users/password-reset/",
        {"email": "ghost@example.com"},
        format="json",
    )
    assert known.status_code == 200
    assert unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_password_reset_confirm_api_success():
    user = UserFactory(email="ok@example.com")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    client = APIClient()
    res = client.post(
        "/api/users/password-reset/confirm/",
        {"uid": uid, "token": token, "password": "Another-Strong9!"},
        format="json",
    )
    assert res.status_code == 200
    user.refresh_from_db()
    assert user.check_password("Another-Strong9!")
