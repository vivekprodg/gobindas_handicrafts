"""
apps/cart/apps.py

Django application configuration for the cart module.
"""
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class CartConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cart"
    verbose_name = _("Shopping Cart")

    def ready(self) -> None:
        # Connect signal handlers for cache invalidation and cart activity tracking.
        try:
            from . import signals  # noqa: F401
        except ImportError:
            pass