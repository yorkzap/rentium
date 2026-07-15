from django.apps import AppConfig


class ShowcaseConfig(AppConfig):
    name = "rentium.showcase"
    verbose_name = "Public Showcase"

    def ready(self):
        from . import handlers  # noqa: F401
