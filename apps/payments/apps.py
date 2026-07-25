from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payments"
    verbose_name = _("Payment Gateways & Financial Reconciliation Engine")

    def ready(self) -> None:
        try:
            from . import signals  # noqa: F401
        except ImportError:
            pass