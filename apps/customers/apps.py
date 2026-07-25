from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class CustomersConfig(AppConfig):
    """
    Application configuration for the Customer Management module.
    """
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.customers"
    verbose_name = _("Customer Management")

    def ready(self) -> None:
        """
        Registers signal handlers upon app startup.
        """
        try:
            import apps.customers.signals  # noqa: F401
        except ImportError:
            pass