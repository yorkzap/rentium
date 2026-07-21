from django.apps import AppConfig


class RamaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rentium.rama"
    verbose_name = "RAMA"

    def ready(self):
        # Registers the Sergeant-finding @on(...) handlers.
        from . import handlers  # noqa: F401
