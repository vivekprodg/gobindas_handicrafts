"""
App configuration module for apps.coupons.
"""
from __future__ import annotations

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

class CouponsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.coupons"
    verbose_name = _("Coupons & Promotional Vouchers Engine")

    def ready(self) -> None:
        """
        Connect signals on application startup.
        """
        try:
            import apps.coupons.signals  # noqa: F401
        except ImportError:
            pass