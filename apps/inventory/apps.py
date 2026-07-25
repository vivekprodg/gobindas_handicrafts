"""
Enterprise-grade Django application configuration for the Inventory module.
"""

from __future__ import annotations

import importlib
import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

class InventoryConfig(AppConfig):
    """
    Django AppConfig for the inventory application.
    """

    name = "apps.inventory"
    verbose_name = "Inventory Management"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """
        Wire up signal handlers for the inventory application lazily.
        """
        try:
            importlib.import_module("apps.inventory.signals")
        except ImportError as exc:
            logger.warning(
                "Inventory signals module could not be imported during "
                "ready(): %s. Signal listeners inactive.", exc,
            )
        except Exception as exc:
            logger.exception(
                "Unexpected error wiring inventory signals in ready(): %s", exc,
            )