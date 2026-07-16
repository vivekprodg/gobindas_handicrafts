"""
Django application configuration for the Order application.

This file is responsible ONLY for:
    * Declaring the AppConfig metadata
    * Hooking up the global signal registration in `ready()`

The Order app is INVENTORY-AGNOSTIC by design. It does not perform any
business logic, does not compute prices or taxes, does not dispatch
notifications, does not orchestrate inventory, and does not implement
payment gateway logic. All such concerns are owned by their respective
applications (inventory, payments, notifications, tax, coupons, etc.).
"""

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class OrdersConfig(AppConfig):
    """
    Enterprise-grade Django app configuration for the orders module.

    The `ready()` hook is the canonical Django lifecycle extension
    point. It is invoked exactly once per process after the application
    registry is fully populated.

    Responsibilities declared here:
        1. Register post_save / post_delete signal handlers declared in
           `apps.orders.signals` so that the historical audit
           infrastructure (OrderStatusHistory, OrderTimelineEvent,
           return request numbering) is automatically activated.
        2. Remain completely free of business logic, ORM mutations,
           network calls, or any other side effects beyond signal
           registration. Business logic belongs in the service layer
           (apps/orders/services.py), not in the app config.
        3. Use lazy imports inside `ready()` to avoid premature model
           loading during Django's application loading sequence. This
           prevents circular import issues with the signals module
           (which itself imports models from this app).
    """

    # ------------------------------------------------------------------
    # Django-required application metadata
    # ------------------------------------------------------------------
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.orders"
    verbose_name = _("Order Management")

    def ready(self) -> None:
        """
        Safely register the orders app's signal handlers.

        This is the canonical place to wire up cross-app integrations
        that need to react to order lifecycle events. The signal
        registration is wrapped in a defensive try/except block so
        that a misconfigured signals module (or a partially-installed
        dependency) can never prevent the orders app from booting.

        All signal handlers declared in `apps.orders.signals` MUST be
        idempotent and MUST NOT perform any business logic. They
        exist solely to:
            1. Maintain the immutable audit-trail records
               (OrderStatusHistory, OrderTimelineEvent).
            2. Auto-generate human-readable business numbers
               (e.g. return_number).
            3. Provide cross-app integration points for downstream
               consumers (notifications, accounting, etc.).

        All other concerns (financial calculations, inventory
        mutations, email dispatch, etc.) belong in the service layer
        and MUST be called explicitly by views / selectors.
        """
        try:
            # Lazy import to avoid premature model loading and to
            # break potential circular import chains.
            from . import signals  # noqa: F401
        except ImportError as exc:
            # Signals module not yet present. Log and continue so
            # the application still boots in early development
            # environments.
            import logging
            logging.getLogger(__name__).warning(
                "Orders signals module could not be imported during "
                "ready(): %s. Audit-trail signal handlers will be "
                "inactive for this process.",
                exc,
            )
        except Exception as exc:  # pragma: no cover - defensive guard
            # Never propagate startup failures. Production
            # environments must remain bootable even if an optional
            # signal component is broken.
            import logging
            logging.getLogger(__name__).exception(
                "Unexpected error while wiring orders signals in "
                "OrdersConfig.ready(): %s",
                exc,
            )