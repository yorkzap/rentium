import contextlib

from django.apps import AppConfig


class LeasesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'rentium.leases'

    def ready(self):
        with contextlib.suppress(ImportError):
            import rentium.leases.signals  # noqa: F401
