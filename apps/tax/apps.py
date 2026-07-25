from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class TaxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tax"
    verbose_name = _("Dynamic Global Tax & Compliance Engine")

    def ready(self) -> None:
        try:
            from . import signals  # noqa: F401
        except ImportError:
            pass