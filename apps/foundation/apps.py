from django.apps import AppConfig

class FoundationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.foundation"

    def ready(self) -> None:
        import apps.foundation.signals