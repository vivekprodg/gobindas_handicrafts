from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class CustomersConfig(AppConfig):
    """
    Django application configuration for the ecommerce customer module.
    Provides application-level settings, lifecycle hooks, and metadata 
    for the Django administrative interface.
    """
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.customers"
    verbose_name = _("Customer Management")

    def ready(self) -> None:
        """
        Initialization execution hook invoked when the application registry is fully populated.
        Used primarily for connecting signal handlers to avoid premature model imports.
        """
        # Implicitly load signals if they are added in the future
        try:
            import customers.signals  # noqa: F401
        except ImportError:
            pass