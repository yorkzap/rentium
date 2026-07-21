"""
One-command Telegram bot wiring.

The RAMA↔Telegram pipeline is fully built (comms app); the only thing that
can't live in code is registering the webhook with Telegram, which needs the
real bot token and the public URL. Run this once after setting the env vars:

    # .env (see config/settings/base.py):
    #   TELEGRAM_BOT_TOKEN=123456:ABC...        (from @BotFather)
    #   TELEGRAM_BOT_USERNAME=RentiumBot
    #   TELEGRAM_WEBHOOK_SECRET=<any long random string>

    python manage.py set_telegram_webhook --url https://api.rentium.ca/api/public/comms/telegram/webhook/

    python manage.py set_telegram_webhook --info      # show current registration
    python manage.py set_telegram_webhook --delete    # unregister

The secret is sent as `secret_token`; Telegram then echoes it on every update
in the X-Telegram-Bot-Api-Secret-Token header, which comms/api/views.py checks.
"""

from __future__ import annotations

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Register (or inspect/delete) the Telegram webhook for the Rentium bot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            help="Public HTTPS URL of the webhook endpoint "
            "(…/api/public/comms/telegram/webhook/).",
        )
        parser.add_argument("--info", action="store_true", help="Show current webhook info.")
        parser.add_argument("--delete", action="store_true", help="Delete the webhook.")

    def handle(self, *args, **opts):
        token = (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()
        if not token:
            raise CommandError(
                "TELEGRAM_BOT_TOKEN is not set. Add it to your environment first "
                "(get it from @BotFather)."
            )
        base = f"https://api.telegram.org/bot{token}"

        if opts["info"]:
            self._show(requests.get(f"{base}/getWebhookInfo", timeout=15))
            return

        if opts["delete"]:
            self._show(requests.post(f"{base}/deleteWebhook", timeout=15))
            return

        webhook_path = "/api/public/comms/telegram/webhook/"
        url = (opts.get("url") or getattr(settings, "TELEGRAM_WEBHOOK_URL", "") or "").strip()
        if not url:
            raise CommandError(
                "Pass --url <your API base, e.g. https://api.rentium.ca> "
                "(the /api/public/comms/telegram/webhook/ path is added for you), "
                "or --info / --delete."
            )
        # Be forgiving: accept a bare host and append the correct path so nobody
        # has to guess it (that was a repeated 404).
        if "comms/telegram/webhook" not in url:
            url = url.rstrip("/") + webhook_path
        secret = (getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()
        if not secret:
            raise CommandError(
                "TELEGRAM_WEBHOOK_SECRET is not set — the webhook must have a secret, "
                "it is the ONLY authentication on that public endpoint."
            )
        payload = {
            "url": url,
            "secret_token": secret,
            "allowed_updates": ["message"],
        }
        self._show(requests.post(f"{base}/setWebhook", json=payload, timeout=15))
        self.stdout.write(
            self.style.WARNING(
                f"Webhook set to {url}\n"
                "IMPORTANT: the SERVER process that answers that URL (gunicorn/"
                "the django container) must ALSO have TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_WEBHOOK_SECRET in its environment — not just this shell. "
                "Put them in your env file and RESTART the container, or /link "
                "will silently do nothing."
            )
        )

    def _show(self, response):
        try:
            data = response.json()
        except ValueError:
            raise CommandError(f"Telegram returned non-JSON ({response.status_code}).")
        if not data.get("ok"):
            raise CommandError(f"Telegram error: {data}")
        self.stdout.write(self.style.SUCCESS(f"OK: {data.get('result', data.get('description'))}"))
