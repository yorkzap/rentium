"""
Comms: channel linking, the Telegram webhook (secret verification, linking
flow, message routing), outbound send, and the event bridge — all with a
fake transport so no test ever calls api.telegram.org.
"""

from unittest import mock

import pytest
from rest_framework.test import APIClient

from rentium.comms.models import ChannelAccount

pytestmark = pytest.mark.django_db


@pytest.fixture
def other_landlord():
    from rentium.users.models import LandlordProfile
    from rentium.users.tests.factories import UserFactory

    return LandlordProfile.objects.create(user=UserFactory())


def _client_for(profile):
    client = APIClient()
    client.force_authenticate(user=profile.user)
    return client


def _webhook(client, body, *, secret="test-secret"):
    return client.post(
        "/api/public/comms/telegram/webhook/",
        body,
        format="json",
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret,
    )


# -------------------------------------------------------------- link code
def test_mint_and_redeem_link_code(landlord):
    account = ChannelAccount.mint_link_code(landlord, ChannelAccount.ChannelType.TELEGRAM)
    assert account.verified is False and account.link_code

    bound = ChannelAccount.redeem_link_code(
        account.link_code.lower(),  # case-insensitive
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="12345",
        display_name="Raj",
    )
    assert bound is not None
    assert bound.verified is True and bound.address == "12345"
    assert bound.link_code == ""

    # A stale/wrong code redeems nothing.
    assert (
        ChannelAccount.redeem_link_code(
            "NOPE", channel_type=ChannelAccount.ChannelType.TELEGRAM, address="999"
        )
        is None
    )


def test_create_link_code_endpoint(landlord, settings):
    settings.TELEGRAM_BOT_USERNAME = "RentiumBot"
    client = _client_for(landlord)
    res = client.post("/api/comms/channels/telegram/link-code/")
    assert res.status_code == 200
    body = res.json()
    assert body["bot_username"] == "RentiumBot"
    assert ChannelAccount.objects.get(landlord=landlord).link_code == body["link_code"]


# ---------------------------------------------------------------- webhook
def test_webhook_rejects_bad_secret(settings):
    settings.TELEGRAM_WEBHOOK_SECRET = "correct-secret"
    client = APIClient()
    res = _webhook(client, {"message": {"chat": {"id": 1}, "text": "hi"}}, secret="wrong")
    assert res.status_code == 403


def test_webhook_link_flow(landlord, settings):
    settings.TELEGRAM_WEBHOOK_SECRET = "test-secret"
    account = ChannelAccount.mint_link_code(landlord, ChannelAccount.ChannelType.TELEGRAM)
    client = APIClient()
    with mock.patch("rentium.comms.telegram.send_message") as sent:
        res = _webhook(
            client,
            {
                "message": {
                    "chat": {"id": 555, "username": "raj"},
                    "text": f"/link {account.link_code}",
                }
            },
        )
    assert res.status_code == 200
    account.refresh_from_db()
    assert account.verified is True and account.address == "555"
    assert sent.call_args[0][0] == "555"
    assert "Linked" in sent.call_args[0][1]


def test_webhook_unlinked_chat_gets_instructions(settings):
    settings.TELEGRAM_WEBHOOK_SECRET = "test-secret"
    client = APIClient()
    with mock.patch("rentium.comms.telegram.send_message") as sent:
        res = _webhook(client, {"message": {"chat": {"id": 999}, "text": "hello"}})
    assert res.status_code == 200
    assert "link" in sent.call_args[0][1].lower()


def test_webhook_linked_chat_enqueues_turn(landlord, settings):
    settings.TELEGRAM_WEBHOOK_SECRET = "test-secret"
    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="777",
        verified=True,
    )
    client = APIClient()
    with mock.patch("rentium.comms.tasks.handle_telegram_message.delay") as delay:
        res = _webhook(client, {"message": {"chat": {"id": 777}, "text": "how's rent?"}})
    assert res.status_code == 200
    delay.assert_called_once_with(str(landlord.pk), "777", "how's rent?", photo_file_id="")


def test_webhook_photo_message_passes_biggest_file_id(landlord, settings):
    settings.TELEGRAM_WEBHOOK_SECRET = "test-secret"
    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="777",
        verified=True,
    )
    client = APIClient()
    msg = {
        "message": {
            "chat": {"id": 777},
            "caption": "add this to Room C",
            "photo": [{"file_id": "small"}, {"file_id": "BIG"}],  # largest is last
        }
    }
    with mock.patch("rentium.comms.tasks.handle_telegram_message.delay") as delay:
        res = _webhook(client, msg)
    assert res.status_code == 200
    delay.assert_called_once_with(
        str(landlord.pk), "777", "add this to Room C", photo_file_id="BIG"
    )


def test_handle_telegram_photo_stages_upload(landlord, settings):
    from rentium.rama.models import RamaPreferences, RamaUpload
    from rentium.rama.providers import Turn
    from rentium.rama.tests import ScriptedProvider

    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = "xai"
    prefs.api_key = "xai-test"
    prefs.save()

    from rentium.comms.tasks import handle_telegram_message

    gif = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
        b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )
    provider = ScriptedProvider([Turn(text="Which listing should it go on?")])
    with mock.patch(
        "rentium.comms.telegram.get_file_bytes", return_value=(gif, "photo.jpg")
    ):
        with mock.patch("rentium.rama.service.get_provider", return_value=provider):
            with mock.patch("rentium.comms.telegram.send_message"):
                handle_telegram_message(
                    str(landlord.pk), "42", "add to Room C", photo_file_id="BIG"
                )
    assert RamaUpload.objects.filter(landlord=landlord).count() == 1


def test_telegram_conversation_id_is_stable_per_chat():
    from rentium.comms.tasks import telegram_conversation_id

    a = telegram_conversation_id("777")
    b = telegram_conversation_id("777")
    c = telegram_conversation_id("778")
    assert a == b and a != c


def test_handle_telegram_message_runs_general_and_replies(landlord, settings):
    from rentium.rama.models import RamaPreferences
    from rentium.rama.providers import Turn
    from rentium.rama.tests import ScriptedProvider

    prefs = RamaPreferences.for_landlord(landlord)
    prefs.enabled = True
    prefs.provider = "xai"
    prefs.api_key = "xai-test"
    prefs.save()

    from rentium.comms.tasks import handle_telegram_message

    provider = ScriptedProvider([Turn(text="Rent is on track this month.")])
    with mock.patch("rentium.rama.service.get_provider", return_value=provider):
        with mock.patch("rentium.comms.telegram.send_message") as sent:
            handle_telegram_message(str(landlord.pk), "42", "how's rent?")
    assert sent.call_args[0] == ("42", "Rent is on track this month.")


# ---------------------------------------------------------------- outbound
def test_send_to_landlord_respects_category_prefs(landlord):
    from rentium.comms.services import send_to_landlord

    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="1",
        verified=True,
        prefs={"categories": ["PAYMENT"]},
    )
    with mock.patch("rentium.comms.telegram.send_message", return_value=True) as sent:
        sent_types = send_to_landlord(landlord, "hi", category="MAINTENANCE")
        assert sent_types == [] and not sent.called
        sent_types = send_to_landlord(landlord, "paid", category="PAYMENT")
        assert sent_types == ["TELEGRAM"] and sent.called


def test_send_to_landlord_skips_unverified_and_inactive(landlord):
    from rentium.comms.services import send_to_landlord

    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="1",
        verified=False,
    )
    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="2",
        verified=True,
        is_active=False,
    )
    with mock.patch("rentium.comms.telegram.send_message") as sent:
        send_to_landlord(landlord, "hi")
    assert not sent.called


# ------------------------------------------------------------ event bridge
# publish()'s handler dispatch runs via transaction.on_commit, which never
# fires inside pytest-django's default (rolled-back) test transaction — so,
# like appointments/tests.py, we publish to create the DomainEvent row and
# then invoke process_domain_event(...) directly to run handlers.
def test_event_bridge_mirrors_landlord_events(landlord, bc_property, bc_lease):
    from rentium.events.registry import publish
    from rentium.events.tasks import process_domain_event

    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="1",
        verified=True,
    )
    event = publish(
        "ledger.payment_posted",
        {"amount": "850.00", "method": "ETRANSFER"},
        property_id=bc_property.pk,
        lease_id=bc_lease.pk,
    )
    with mock.patch("rentium.comms.telegram.send_message", return_value=True) as sent:
        process_domain_event(str(event.id))
    assert sent.called
    assert "Payment recorded" in sent.call_args[0][1]


def test_event_bridge_ignores_tenant_only_events(landlord, bc_property, bc_lease):
    """maintenance.status_changed is TENANT-only — must never reach the
    landlord's channels (that would be someone else's private update)."""
    from rentium.events.registry import publish
    from rentium.events.tasks import process_domain_event

    ChannelAccount.objects.create(
        landlord=landlord,
        channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="1",
        verified=True,
    )
    event = publish(
        "maintenance.status_changed", {"to": "IN_PROGRESS"}, property_id=bc_property.pk
    )
    with mock.patch("rentium.comms.telegram.send_message") as sent:
        process_domain_event(str(event.id))
    assert not sent.called


# ------------------------------------------------------- morning briefing (P6)
def test_build_briefing_text_is_deterministic_and_grounded(landlord, bc_property, bc_lease):
    from rentium.rama.briefing import build_briefing_text
    from rentium.rama.models import RamaInsight

    RamaInsight.objects.create(
        landlord=landlord, kind="rama.sentinel.min_balance", severity="URGENT",
        facts={}, analysis="Wascana is under the minimum.",
    )
    text = build_briefing_text(landlord)
    assert "Good morning" in text
    assert "Occupied" in text
    assert "1 open insight" in text
    assert "Wascana is under the minimum." in text
    # Deterministic — no LLM, so identical input yields identical output.
    assert text == build_briefing_text(landlord)


def test_build_briefing_text_no_open_insights(landlord):
    from rentium.rama.briefing import build_briefing_text

    assert "No open insights." in build_briefing_text(landlord)


def test_send_morning_briefings_only_to_opted_in_channels(landlord, other_landlord):
    from rentium.comms.models import ChannelAccount
    from rentium.comms.tasks import send_morning_briefings

    ChannelAccount.objects.create(
        landlord=landlord, channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="1", verified=True, prefs={"briefing": True},
    )
    # Verified but did NOT opt into the briefing — must be skipped.
    ChannelAccount.objects.create(
        landlord=other_landlord, channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="2", verified=True, prefs={},
    )
    with mock.patch("rentium.comms.telegram.send_message", return_value=True) as sent:
        report = send_morning_briefings()
    assert report == {"briefings_sent": 1, "landlords": 1}
    assert sent.call_args[0][0] == "1"
    assert "Good morning" in sent.call_args[0][1]


def test_channel_prefs_briefing_toggle_via_api(landlord):
    account = ChannelAccount.objects.create(
        landlord=landlord, channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="1", verified=True,
    )
    client = _client_for(landlord)
    res = client.patch(
        f"/api/comms/channels/{account.id}/",
        {"prefs": {"briefing": True}}, format="json",
    )
    assert res.status_code == 200
    account.refresh_from_db()
    assert account.prefs.get("briefing") is True


# ----------------------------------------------------------- tenant channels
def test_tenant_can_mint_link_code(tenant, settings):
    settings.TELEGRAM_BOT_USERNAME = "RentiumBot"
    res = _client_for(tenant).post("/api/comms/channels/telegram/link-code/")
    assert res.status_code == 200
    acct = ChannelAccount.objects.get(tenant=tenant)
    assert acct.tenant_id == tenant.pk and acct.landlord_id is None


def test_tenant_chat_gets_canned_reply_never_runs_rama(tenant, settings):
    """SECURITY: a linked tenant chat must NOT drive a RAMA turn (landlord
    tools). It gets a canned pointer back to the app instead."""
    settings.TELEGRAM_WEBHOOK_SECRET = "test-secret"
    acct = ChannelAccount.mint_link_code(tenant, ChannelAccount.ChannelType.TELEGRAM)
    ChannelAccount.redeem_link_code(
        acct.link_code, channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="777", display_name="Tenant Tim",
    )
    client = APIClient()
    with mock.patch("rentium.comms.telegram.send_message") as sent, mock.patch(
        "rentium.comms.tasks.handle_telegram_message.delay"
    ) as rama:
        res = _webhook(client, {"message": {"chat": {"id": 777}, "text": "how much rent do I owe?"}})
    assert res.status_code == 200
    rama.assert_not_called()  # RAMA never runs for a tenant
    assert "Rentium app" in sent.call_args[0][1]


def test_send_to_tenant_delivers_to_linked_channel(tenant):
    from rentium.comms.services import send_to_tenant

    ChannelAccount.objects.create(
        tenant=tenant, channel_type=ChannelAccount.ChannelType.TELEGRAM,
        address="888", verified=True, is_active=True,
    )
    with mock.patch("rentium.comms.telegram.send_message", return_value=True) as sent:
        out = send_to_tenant(tenant, "A viewing needs your input")
    assert out == ["TELEGRAM"]
    assert sent.call_args[0][0] == "888"


def test_channel_requires_exactly_one_subject(landlord, tenant):
    from django.db import IntegrityError, transaction

    # neither subject
    with pytest.raises(IntegrityError), transaction.atomic():
        ChannelAccount.objects.create(
            channel_type=ChannelAccount.ChannelType.TELEGRAM, address="a1"
        )
    # both subjects
    with pytest.raises(IntegrityError), transaction.atomic():
        ChannelAccount.objects.create(
            landlord=landlord, tenant=tenant,
            channel_type=ChannelAccount.ChannelType.TELEGRAM, address="a2",
        )


# ------------------------------------------------------------ WhatsApp seam
def _wa(client, body):
    return client.post(
        "/api/public/comms/whatsapp/webhook/", body, format="json"
    )


def test_whatsapp_transport_is_safe_noop_when_unconfigured(settings):
    settings.WHATSAPP_TOKEN = ""
    settings.WHATSAPP_PHONE_NUMBER_ID = ""
    from rentium.comms import whatsapp

    # Never raises; just returns False when there's nothing to send with.
    assert whatsapp.send_message("15551234567", "hi") is False


def test_whatsapp_get_handshake(settings):
    settings.WHATSAPP_VERIFY_TOKEN = "verify-me"
    client = APIClient()
    ok = client.get(
        "/api/public/comms/whatsapp/webhook/",
        {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"},
    )
    assert ok.status_code == 200 and ok.json() == 12345
    bad = client.get(
        "/api/public/comms/whatsapp/webhook/",
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"},
    )
    assert bad.status_code == 403


def test_whatsapp_inbound_routes_landlord_to_rama(landlord, settings):
    settings.WHATSAPP_APP_SECRET = ""  # skip signature check in test
    ChannelAccount.objects.create(
        landlord=landlord, channel_type=ChannelAccount.ChannelType.WHATSAPP,
        address="15550001111", verified=True, is_active=True,
    )
    client = APIClient()
    body = {
        "entry": [
            {"changes": [{"value": {"messages": [
                {"from": "15550001111", "text": {"body": "how many listings?"}}
            ]}}]}
        ]
    }
    with mock.patch("rentium.comms.tasks.handle_whatsapp_message.delay") as rama:
        res = _wa(client, body)
    assert res.status_code == 200
    rama.assert_called_once()


def test_whatsapp_inbound_tenant_gets_canned_reply(tenant, settings):
    settings.WHATSAPP_APP_SECRET = ""
    ChannelAccount.objects.create(
        tenant=tenant, channel_type=ChannelAccount.ChannelType.WHATSAPP,
        address="15559998888", verified=True, is_active=True,
    )
    client = APIClient()
    body = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "15559998888", "text": {"body": "hi"}}
    ]}}]}]}
    with mock.patch("rentium.comms.whatsapp.send_message") as sent, mock.patch(
        "rentium.comms.tasks.handle_whatsapp_message.delay"
    ) as rama:
        res = _wa(client, body)
    assert res.status_code == 200
    rama.assert_not_called()
    assert "Rentium app" in sent.call_args[0][1]
