from django.apps import AppConfig


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rentium.ledger"

    def ready(self):
        from . import handlers  # noqa: F401
        from . import signals  # noqa: F401
