from django.urls import path

from . import views

app_name = "comms"

# Authenticated: mounted at api/comms/ in config/urls.py.
urlpatterns = [
    path("channels/", views.list_channels, name="channels"),
    path(
        "channels/telegram/link-code/",
        views.create_link_code,
        name="telegram-link-code",
    ),
    path("channels/<int:channel_id>/", views.channel_detail, name="channel-detail"),
]

# Public: mounted at api/public/ in config/urls.py — see the security-boundary
# comment there. Telegram sends unauthenticated POSTs verified by a secret
# header (checked in the view), not a user session.
public_urlpatterns = [
    path(
        "comms/telegram/webhook/",
        views.telegram_webhook,
        name="telegram-webhook",
    ),
    # WhatsApp (Meta Cloud API): GET handshake + POST messages, HMAC-verified.
    path(
        "comms/whatsapp/webhook/",
        views.whatsapp_webhook,
        name="whatsapp-webhook",
    ),
]
