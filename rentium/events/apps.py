from django.apps import AppConfig


class EventsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rentium.events"

    def ready(self):
        # Import handlers + notification fan-out so their @on(...) hooks register.
        from . import handlers  # noqa: F401
        from . import notify  # noqa: F401
