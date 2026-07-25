from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class OrdersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orders"
    verbose_name = _("Order Management")

    def ready(self) -> None:
        try:
            from . import signals  # noqa: F401
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to register order signals: %s", exc)