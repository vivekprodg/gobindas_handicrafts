from django.apps import AppConfig


class HomepageConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.homepage"

    def ready(self) -> None:
        """
        Called when Django starts up. Imports the signals module
        to ensure model event receivers are properly registered.
        """
        from . import signals