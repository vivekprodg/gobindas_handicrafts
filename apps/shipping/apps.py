from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class ShippingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.shipping"
    verbose_name = _("Shipping & Fulfillment Engine")

    def ready(self) -> None:
        try:
            from . import signals  # noqa: F401
        except ImportError:
            pass