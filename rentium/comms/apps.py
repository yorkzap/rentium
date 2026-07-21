from django.apps import AppConfig


class CommsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rentium.comms"

    def ready(self):
        # Register the @on(...) event bridge (events → landlord channels).
        from . import handlers  # noqa: F401
