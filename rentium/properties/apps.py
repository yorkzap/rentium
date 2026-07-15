from django.apps import AppConfig


class PropertiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "rentium.properties"
    verbose_name = "Properties"

    def ready(self):
        # Registers the post_save/post_delete receivers that keep
        # Property.is_furnished in sync with inventory. Must be imported here
        # (not at module level) so the app registry is fully loaded first.
        from . import signals  # noqa: F401
