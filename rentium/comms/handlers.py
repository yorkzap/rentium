"""
The event → channel bridge — exactly the extension point events/handlers.py
promises: "Adding mobile push later = add a handler here; zero core-code
changes." This one mirrors LANDLORD-audience domain events to whichever
external channels (Telegram today) that landlord has linked and opted into.

Reuses events/notify.py's ROUTES + rendering + recipient resolution so a
Telegram message says exactly what the in-app bell says — one source of
truth for "what does this event mean," many delivery surfaces.
"""

from __future__ import annotations

import logging

from rentium.events.notify import ROUTES, _landlord_user, _render, _tenant_users
from rentium.events.registry import on

logger = logging.getLogger(__name__)


@on("*")
def bridge_to_channels(event):
    route = ROUTES.get(event.event_type)
    if route is None:
        return
    audience, category = route

    title, body, url = _render(event)
    text = f"{title}\n{body}".strip() if body else title

    from .services import send_to_landlord, send_to_tenant

    if audience in ("LANDLORD", "BOTH"):
        user = _landlord_user(event)
        landlord = getattr(user, "landlord_profile", None) if user else None
        if landlord is not None:
            send_to_landlord(landlord, text, category=category, url=url)

    if audience in ("TENANT", "BOTH"):
        for user in _tenant_users(event):
            tenant = getattr(user, "tenant_profile", None)
            if tenant is not None:
                send_to_tenant(tenant, text, category=category, url=url)
