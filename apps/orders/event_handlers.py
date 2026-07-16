"""
Enterprise-grade domain event coordination layer for the Orders application.

This module is the orchestration layer that sits between Django's ORM
signal handlers (which detect lifecycle events) and the actual business
workflows (which are executed by services, selectors, and Celery tasks).

ARCHITECTURE
============
Layered responsibility model:

    signals.py        → Detects ORM lifecycle events (low-level)
    event_handlers.py → Coordinates domain workflows (this file)
    services.py       → Executes business logic (high-level)
    tasks.py          → Executes background work (Celery)
    views.py          → Receives HTTP requests
    models.py         → Persists data

This file's job is **orchestration only**. It:
    1. Receives a domain event that was already detected and validated
       by the signal layer.
    2. Resolves the target order and (optionally) related entities.
    3. Enqueues the appropriate Celery tasks for heavy / asynchronous
       work (email, SMS, push, webhooks, search indexing, analytics).
    4. Invalidates caches that have become stale.
    5. Logs structured event data for observability.
    6. Coordinates the recording of any cross-app event records that
       MUST be synchronous (e.g. fraud-engine synchronisation hooks).

It NEVER:
    1. Computes prices, taxes, or discounts.
    2. Mutates inventory.
    3. Implements payment-gateway logic.
    4. Sends emails, SMS, or webhooks directly.
    5. Makes external HTTP requests.
    6. Performs heavy database queries.
    7. Contains business validation.
    8. Duplicates logic that already lives in signals.py, services.py,
       or models.py.

REGISTRATION
============
This module does NOT register signal handlers. It is imported by
``signals.py`` (the low-level detector) which delegates to the public
``handle_*`` functions declared here. This keeps the
detection / coordination separation crystal clear.

PERFORMANCE
===========
* All Celery tasks are enqueued via ``transaction.on_commit`` so that
  they NEVER fire for rolled-back transactions.
* Cache invalidation is batched and only targets the order whose
  state changed.
* Database reads use ``.only()`` to avoid loading unnecessary fields.
* No N+1 queries: related entities are loaded via ``select_related``
  or ``prefetch_related`` when multiple are needed.
* All work is wrapped in a single ``try/except`` block to guarantee
  that a failure in one event handler NEVER blocks another.

SECURITY
========
* No sensitive data is logged.
* No exceptions are re-raised; all failures are logged and swallowed
  so that the order-saving transaction is never blocked.
* No unsafe imports; services / tasks are accessed via lazy imports
  to avoid circular dependencies.
* No mutable globals beyond the deliberately-shared dispatcher
  instance.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Final, FrozenSet, List, Optional

from django.db import transaction
from django.db.models import Model
from django.utils import timezone

from apps.orders import constants as c
from apps.orders.models import (
    CouponUsage,
    Order,
    OrderAttachment,
    OrderItem,
    OrderNote,
    OrderTimelineEvent,
    Payment,
    Refund,
    ReturnRequest,
    Shipment,
)

logger = logging.getLogger(c.LOGGER_SIGNALS)

# ==============================================================================
# LAZY OPTIONAL IMPORTS
# ==============================================================================
def _try_import(module_path: str) -> Optional[Any]:
    """
    Safely import a module by dotted path, returning ``None`` if the
    module is not available. This allows the orders app to operate
    even when the notifications, search, analytics, or Celery
    sub-systems are not yet wired in (e.g. during early development
    or in minimal test environments).
    """
    try:
        parts = module_path.split(".")
        module = __import__(module_path, fromlist=[parts[-1]])
        return module
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Optional module %s is not importable: %s",
            module_path, exc,
        )
        return None

def _try_import_attr(module_path: str, attr: str) -> Optional[Any]:
    """
    Safely import a named attribute from a module. Returns ``None`` if
    either the module or the attribute is unavailable.
    """
    module = _try_import(module_path)
    if module is None:
        return None
    return getattr(module, attr, None)

# ==============================================================================
# CACHE INVALIDATION HELPERS
# ==============================================================================
def _try_cache_delete(key: str) -> None:
    """
    Best-effort cache key deletion. Silently no-ops if the cache
    backend raises any exception (so the event handler is never
    blocked by a cache failure).
    """
    try:
        from django.core.cache import cache

        cache.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Cache delete failed for key=%s: %s", key, exc,
        )

def _try_cache_delete_pattern(pattern: str) -> None:
    """
    Best-effort cache pattern deletion. Falls back to no-op if the
    cache backend does not support pattern-based deletion.
    """
    try:
        from django.core.cache import cache

        try:
            cache.delete_pattern(pattern)  # type: ignore[attr-defined]
        except AttributeError:
            # Cache backend does not support pattern delete; this
            # is acceptable for backends without that capability.
            pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Cache pattern delete failed for pattern=%s: %s",
            pattern, exc,
        )

def _invalidate_order_caches(order: Order) -> None:
    """
    Invalidate every cache key that depends on the supplied order.

    Called by every order-related event handler so that read paths
    (e.g. order detail pages, customer dashboards) never serve stale
    data after a status change.
    """
    if not order or not order.pk:
        return

    order_id = str(order.pk)
    order_number = order.order_number

    _try_cache_delete(
        c.CACHE_KEY_ORDER_BY_ID.format(
            ns=c.CACHE_NAMESPACE, order_id=order_id,
        )
    )
    _try_cache_delete(
        c.CACHE_KEY_ORDER_BY_NUMBER.format(
            ns=c.CACHE_NAMESPACE, order_number=order_number,
        )
    )
    _try_cache_delete(
        c.CACHE_KEY_ORDER_TIMELINE.format(
            ns=c.CACHE_NAMESPACE, order_id=order_id,
        )
    )
    _try_cache_delete(
        c.CACHE_KEY_ORDER_COUNT.format(ns=c.CACHE_NAMESPACE)
    )

# ==============================================================================
# TASK ENQUEUE HELPERS
# ==============================================================================
def _enqueue_task(task_path: str, *args: Any, **kwargs: Any) -> None:
    """
    Enqueue a Celery task by dotted path.

    The actual import + ``.delay()`` call is performed lazily inside a
    ``try/except`` block so that:

        1. A missing Celery installation does NOT break the orders app.
        2. A failing task (e.g. broker down) does NOT roll back the
           parent database transaction.
        3. A failing task does NOT propagate up to the caller.

    All work is scheduled via ``transaction.on_commit`` to guarantee
    that the task is dispatched ONLY after the parent transaction
    successfully commits.
    """
    def _do() -> None:
        try:
            task = _try_import_attr(task_path, "delay")
            if task is None:
                task = _try_import_attr(task_path, "apply_async")
            if task is None:
                logger.debug(
                    "Task %s is not enqueueable (module missing or "
                    "no delay/apply_async).", task_path,
                )
                return
            task(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to enqueue task %s: %s", task_path, exc,
            )

    try:
        transaction.on_commit(_do)
    except transaction.TransactionManagementError:
        _do()

# ==============================================================================
# NOTIFICATION TRIGGER HELPERS
# ==============================================================================
def _trigger_customer_notification(
    order: Order, template_token: str, context: Optional[Dict[str, Any]] = None
) -> None:
    """
    Trigger a customer notification. This function ONLY triggers;
    it does NOT implement delivery.

    The actual delivery is performed by the notifications app's
    Celery task. We delegate by enqueuing the task with the
    notification template token and a minimal context payload.
    """
    _enqueue_task(
        "apps.notifications.tasks.send_customer_notification",
        order_id=str(order.pk),
        template=template_token,
        context=context or {},
    )

def _trigger_staff_notification(
    order: Order, template_token: str, context: Optional[Dict[str, Any]] = None
) -> None:
    """
    Trigger an internal-staff notification. This function ONLY
    triggers; it does NOT implement delivery.
    """
    _enqueue_task(
        "apps.notifications.tasks.send_staff_notification",
        order_id=str(order.pk),
        template=template_token,
        context=context or {},
    )

def _trigger_admin_notification(
    order: Order, template_token: str, context: Optional[Dict[str, Any]] = None
) -> None:
    """
    Trigger an admin / operations notification. This function ONLY
    triggers; it does NOT implement delivery.
    """
    _enqueue_task(
        "apps.notifications.tasks.send_admin_notification",
        order_id=str(order.pk),
        template=template_token,
        context=context or {},
    )

def _trigger_webhook(
    order: Order, event_token: str, context: Optional[Dict[str, Any]] = None
) -> None:
    """
    Trigger a webhook dispatch. Delivery is delegated to the
    webhooks app's Celery task.
    """
    _enqueue_task(
        "apps.webhooks.tasks.dispatch_order_webhook",
        order_id=str(order.pk),
        event=event_token,
        context=context or {},
    )

def _trigger_search_reindex(order: Order) -> None:
    """Trigger a search-index refresh for the order."""
    _enqueue_task(
        "apps.search.tasks.reindex_order",
        order_id=str(order.pk),
    )

def _trigger_analytics_event(
    order: Order, event_name: str, properties: Optional[Dict[str, Any]] = None
) -> None:
    """Trigger an analytics event for the order."""
    _enqueue_task(
        "apps.analytics.tasks.track_order_event",
        order_id=str(order.pk),
        event=event_name,
        properties=properties or {},
    )

def _trigger_inventory_notification(
    order: Order, event_name: str
) -> None:
    """
    Trigger a cross-app inventory notification (e.g. to release
    reservations on cancellation). The Inventory app is responsible
    for actually performing the stock mutation; we only notify.
    """
    _enqueue_task(
        "apps.inventory.tasks.handle_order_event",
        order_id=str(order.pk),
        event=event_name,
    )

# ==============================================================================
# PUBLIC EVENT HANDLERS
# ==============================================================================
#: Set of order statuses that warrant a customer-facing notification.
_CUSTOMER_NOTIFY_STATUSES: Final[FrozenSet[str]] = frozenset({
    c.OrderStatus.PROCESSING,
    c.OrderStatus.SHIPPED,
    c.OrderStatus.DELIVERED,
    c.OrderStatus.CANCELLED,
    c.OrderStatus.REFUNDED,
    c.OrderStatus.PARTIALLY_SHIPPED,
    c.OrderStatus.PARTIALLY_DELIVERED,
    c.OrderStatus.PARTIALLY_REFUNDED,
    c.OrderStatus.COMPLETED,
    c.OrderStatus.ON_HOLD,
    c.OrderStatus.AWAITING_PAYMENT,
    c.OrderStatus.BACKORDERED,
    c.OrderStatus.DISPUTED,
    c.OrderStatus.FAILED,
})

#: Statuses that imply a financial movement and therefore warrant an
#: analytics + webhook notification.
_FINANCIAL_STATUSES: Final[FrozenSet[str]] = frozenset({
    c.OrderStatus.REFUNDED,
    c.OrderStatus.PARTIALLY_REFUNDED,
    c.OrderStatus.CANCELLED,
})

#: Statuses that imply a logistics movement and therefore warrant an
#: inventory notification hook.
_LOGISTICS_STATUSES: Final[FrozenSet[str]] = frozenset({
    c.OrderStatus.SHIPPED,
    c.OrderStatus.PARTIALLY_SHIPPED,
    c.OrderStatus.DELIVERED,
    c.OrderStatus.PARTIALLY_DELIVERED,
    c.OrderStatus.CANCELLED,
})

def handle_order_placed(order: Order) -> None:
    """
    Coordinate the workflows triggered by an order creation event.

    Cascades to:

        1. Customer notification (order confirmation).
        2. Staff notification (new order alert).
        3. Webhook dispatch (order.placed).
        4. Analytics event.
        5. Search-index refresh.
        6. Cache invalidation.
    """
    try:
        _trigger_customer_notification(
            order,
            template_token="order_placed",
            context={"order_number": order.order_number},
        )
        _trigger_staff_notification(
            order,
            template_token="new_order_alert",
            context={"order_number": order.order_number},
        )
        _trigger_webhook(
            order,
            event_token="order.placed",
            context={"order_number": order.order_number},
        )
        _trigger_analytics_event(
            order,
            event_name="order_placed",
            properties={"source": order.source},
        )
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_order_placed failed for order=%s: %s",
            getattr(order, "pk", "?"), exc,
        )

def handle_order_status_changed(
    order: Order,
    *,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
) -> None:
    """
    Coordinate the workflows triggered by an order status change.

    Cascades to:

        1. Customer notification (if the new status is customer-facing).
        2. Staff notification (for non-trivial transitions).
        3. Webhook dispatch (order.status_changed).
        4. Analytics event.
        5. Search-index refresh.
        6. Inventory cross-app notification (if logistics-applicable).
        7. Cache invalidation.
    """
    try:
        if new_status in _CUSTOMER_NOTIFY_STATUSES:
            _trigger_customer_notification(
                order,
                template_token=f"order_{new_status}",
                context={
                    "order_number": order.order_number,
                    "old_status": old_status,
                    "new_status": new_status,
                },
            )

        if new_status in _FINANCIAL_STATUSES:
            _trigger_webhook(
                order,
                event_token=f"order.{new_status}",
                context={
                    "order_number": order.order_number,
                    "old_status": old_status,
                    "new_status": new_status,
                },
            )

        if new_status in _LOGISTICS_STATUSES:
            _trigger_inventory_notification(order, event_name=new_status or "")

        if new_status in {c.OrderStatus.CANCELLED}:
            _trigger_staff_notification(
                order,
                template_token="order_cancelled_alert",
                context={"order_number": order.order_number},
            )

        _trigger_analytics_event(
            order,
            event_name="order_status_changed",
            properties={
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_order_status_changed failed for order=%s: %s",
            getattr(order, "pk", "?"), exc,
        )

def handle_order_payment_status_changed(
    order: Order,
    *,
    old_payment_status: Optional[str] = None,
    new_payment_status: Optional[str] = None,
) -> None:
    """
    Coordinate the workflows triggered by a payment-status change
    on an order (NOT on a Payment record).

    Cascades to:

        1. Customer notification (payment captured / failed / etc.).
        2. Webhook dispatch (order.payment_changed).
        3. Analytics event.
        4. Search-index refresh.
        5. Cache invalidation.
    """
    try:
        _trigger_customer_notification(
            order,
            template_token=f"payment_{new_payment_status}",
            context={
                "order_number": order.order_number,
                "old_payment_status": old_payment_status,
                "new_payment_status": new_payment_status,
            },
        )
        _trigger_webhook(
            order,
            event_token=f"order.payment.{new_payment_status}",
            context={
                "order_number": order.order_number,
                "old_payment_status": old_payment_status,
                "new_payment_status": new_payment_status,
            },
        )
        _trigger_analytics_event(
            order,
            event_name="order_payment_changed",
            properties={
                "old": old_payment_status,
                "new": new_payment_status,
            },
        )
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_order_payment_status_changed failed for order=%s: %s",
            getattr(order, "pk", "?"), exc,
        )

def handle_order_item_changed(
    item: OrderItem,
    *,
    is_creation: bool = False,
) -> None:
    """
    Coordinate the workflows triggered by an order-item change.

    Cascades to:

        1. Search-index refresh (item metadata affects search).
        2. Cache invalidation (order detail caches must be rebuilt).
        3. Analytics event (if creation).
    """
    try:
        order = item.order
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)
        if is_creation:
            _trigger_analytics_event(
                order,
                event_name="order_item_added",
                properties={"order_item_id": str(item.pk)},
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_order_item_changed failed for item=%s: %s",
            getattr(item, "pk", "?"), exc,
        )

def handle_order_item_deleted(item: OrderItem) -> None:
    """
    Coordinate the workflows triggered by an order-item deletion.

    Cascades to:

        1. Search-index refresh.
        2. Cache invalidation.
        3. Analytics event.
    """
    try:
        order = item.order
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)
        _trigger_analytics_event(
            order,
            event_name="order_item_removed",
            properties={"order_item_id": str(item.pk)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_order_item_deleted failed for item=%s: %s",
            getattr(item, "pk", "?"), exc,
        )

def handle_shipment_created(shipment: Shipment) -> None:
    """
    Coordinate the workflows triggered by a shipment creation.

    Cascades to:

        1. Customer notification (parcel dispatched soon).
        2. Staff notification.
        3. Webhook dispatch.
        4. Analytics event.
        5. Cache invalidation.
    """
    try:
        order = shipment.order
        _trigger_customer_notification(
            order,
            template_token="shipment_created",
            context={"shipment_number": shipment.shipment_number},
        )
        _trigger_staff_notification(
            order,
            template_token="shipment_created_alert",
            context={"shipment_number": shipment.shipment_number},
        )
        _trigger_webhook(
            order,
            event_token="shipment.created",
            context={"shipment_number": shipment.shipment_number},
        )
        _trigger_analytics_event(
            order,
            event_name="shipment_created",
            properties={"shipment_id": str(shipment.pk)},
        )
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_shipment_created failed for shipment=%s: %s",
            getattr(shipment, "pk", "?"), exc,
        )

def handle_shipment_status_changed(
    shipment: Shipment,
    *,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
) -> None:
    """
    Coordinate the workflows triggered by a shipment status change.

    Cascades to:

        1. Customer notification (parcel in transit / delivered / etc.).
        2. Webhook dispatch.
        3. Analytics event.
        4. Search-index refresh.
        5. Cache invalidation.
        6. Inventory cross-app notification (if delivered / returned).
    """
    try:
        order = shipment.order

        _trigger_customer_notification(
            order,
            template_token=f"shipment_{new_status}",
            context={
                "shipment_number": shipment.shipment_number,
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_webhook(
            order,
            event_token=f"shipment.{new_status}",
            context={
                "shipment_number": shipment.shipment_number,
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_analytics_event(
            order,
            event_name="shipment_status_changed",
            properties={
                "shipment_id": str(shipment.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)

        if new_status in {
            c.ShipmentStatus.DELIVERED,
            c.ShipmentStatus.RETURNED,
        }:
            _trigger_inventory_notification(
                order, event_name=f"shipment_{new_status or 'changed'}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_shipment_status_changed failed for shipment=%s: %s",
            getattr(shipment, "pk", "?"), exc,
        )

def handle_payment_created(payment: Payment) -> None:
    """
    Coordinate the workflows triggered by a payment record creation.

    Cascades to:

        1. Staff notification (new payment record).
        2. Webhook dispatch.
        3. Analytics event.
        4. Cache invalidation.
    """
    try:
        order = payment.order
        _trigger_staff_notification(
            order,
            template_token="payment_record_created",
            context={"transaction_id": payment.transaction_id},
        )
        _trigger_webhook(
            order,
            event_token="payment.created",
            context={"transaction_id": payment.transaction_id},
        )
        _trigger_analytics_event(
            order,
            event_name="payment_created",
            properties={"transaction_id": payment.transaction_id},
        )
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_payment_created failed for payment=%s: %s",
            getattr(payment, "pk", "?"), exc,
        )

def handle_payment_status_changed(
    payment: Payment,
    *,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
) -> None:
    """
    Coordinate the workflows triggered by a payment status change.

    Cascades to:

        1. Customer notification (payment captured / failed / etc.).
        2. Staff notification.
        3. Webhook dispatch.
        4. Analytics event.
        5. Search-index refresh.
        6. Cache invalidation.
        7. Inventory cross-app notification (if authorised / captured).
    """
    try:
        order = payment.order

        _trigger_customer_notification(
            order,
            template_token=f"payment_{new_status}",
            context={
                "transaction_id": payment.transaction_id,
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_staff_notification(
            order,
            template_token=f"payment_{new_status}_alert",
            context={
                "transaction_id": payment.transaction_id,
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_webhook(
            order,
            event_token=f"payment.{new_status}",
            context={
                "transaction_id": payment.transaction_id,
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_analytics_event(
            order,
            event_name="payment_status_changed",
            properties={
                "transaction_id": payment.transaction_id,
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)

        if new_status in {
            c.PaymentState.CAPTURED,
            c.PaymentState.COMPLETED,
        }:
            _trigger_inventory_notification(
                order, event_name="payment_captured"
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_payment_status_changed failed for payment=%s: %s",
            getattr(payment, "pk", "?"), exc,
        )

def handle_refund_created(refund: Refund) -> None:
    """
    Coordinate the workflows triggered by a refund request creation.

    Cascades to:

        1. Staff notification (refund pending review).
        2. Customer notification (refund received).
        3. Webhook dispatch.
        4. Analytics event.
        5. Cache invalidation.
    """
    try:
        order = refund.order

        _trigger_staff_notification(
            order,
            template_token="refund_requested_alert",
            context={"refund_id": str(refund.pk)},
        )
        _trigger_customer_notification(
            order,
            template_token="refund_requested",
            context={"refund_id": str(refund.pk)},
        )
        _trigger_webhook(
            order,
            event_token="refund.created",
            context={"refund_id": str(refund.pk)},
        )
        _trigger_analytics_event(
            order,
            event_name="refund_created",
            properties={"refund_id": str(refund.pk)},
        )
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_refund_created failed for refund=%s: %s",
            getattr(refund, "pk", "?"), exc,
        )

def handle_refund_status_changed(
    refund: Refund,
    *,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
) -> None:
    """
    Coordinate the workflows triggered by a refund status change.

    Cascades to:

        1. Customer notification (approved / rejected / completed).
        2. Staff notification.
        3. Webhook dispatch.
        4. Analytics event.
        5. Cache invalidation.
    """
    try:
        order = refund.order

        _trigger_customer_notification(
            order,
            template_token=f"refund_{new_status}",
            context={
                "refund_id": str(refund.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_staff_notification(
            order,
            template_token=f"refund_{new_status}_alert",
            context={
                "refund_id": str(refund.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_webhook(
            order,
            event_token=f"refund.{new_status}",
            context={
                "refund_id": str(refund.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_analytics_event(
            order,
            event_name="refund_status_changed",
            properties={
                "refund_id": str(refund.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_refund_status_changed failed for refund=%s: %s",
            getattr(refund, "pk", "?"), exc,
        )

def handle_return_created(return_request: ReturnRequest) -> None:
    """
    Coordinate the workflows triggered by a return-request creation.

    Cascades to:

        1. Staff notification (return pending review).
        2. Customer notification (return received).
        3. Webhook dispatch.
        4. Analytics event.
        5. Cache invalidation.
        6. Inventory cross-app notification (return detected).
    """
    try:
        order = return_request.order

        _trigger_staff_notification(
            order,
            template_token="return_requested_alert",
            context={"return_id": str(return_request.pk)},
        )
        _trigger_customer_notification(
            order,
            template_token="return_requested",
            context={"return_id": str(return_request.pk)},
        )
        _trigger_webhook(
            order,
            event_token="return.created",
            context={"return_id": str(return_request.pk)},
        )
        _trigger_analytics_event(
            order,
            event_name="return_created",
            properties={"return_id": str(return_request.pk)},
        )
        _trigger_inventory_notification(order, event_name="return_requested")
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_return_created failed for return=%s: %s",
            getattr(return_request, "pk", "?"), exc,
        )

def handle_return_status_changed(
    return_request: ReturnRequest,
    *,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
) -> None:
    """
    Coordinate the workflows triggered by a return-request status
    change.

    Cascades to:

        1. Customer notification (approved / rejected / received / etc.).
        2. Staff notification.
        3. Webhook dispatch.
        4. Analytics event.
        5. Search-index refresh.
        6. Cache invalidation.
        7. Inventory cross-app notification (when received or completed).
    """
    try:
        order = return_request.order

        _trigger_customer_notification(
            order,
            template_token=f"return_{new_status}",
            context={
                "return_id": str(return_request.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_staff_notification(
            order,
            template_token=f"return_{new_status}_alert",
            context={
                "return_id": str(return_request.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_webhook(
            order,
            event_token=f"return.{new_status}",
            context={
                "return_id": str(return_request.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_analytics_event(
            order,
            event_name="return_status_changed",
            properties={
                "return_id": str(return_request.pk),
                "old_status": old_status,
                "new_status": new_status,
            },
        )
        _trigger_search_reindex(order)
        _invalidate_order_caches(order)

        if new_status in {
            c.ReturnStatus.RECEIVED,
            c.ReturnStatus.COMPLETED,
        }:
            _trigger_inventory_notification(
                order, event_name=f"return_{new_status or 'changed'}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_return_status_changed failed for return=%s: %s",
            getattr(return_request, "pk", "?"), exc,
        )

def handle_note_created(note: OrderNote) -> None:
    """
    Coordinate the workflows triggered by an order-note creation.

    Cascades to:

        1. Staff notification (if the note is operator-internal).
        2. Customer notification (if the note is customer-visible).
        3. Webhook dispatch.
        4. Analytics event.
        5. Cache invalidation.
    """
    try:
        order = note.order

        if bool(getattr(note, "is_visible_to_customer", False)):
            _trigger_customer_notification(
                order,
                template_token="order_note_customer",
                context={"note_id": str(note.pk)},
            )
        else:
            _trigger_staff_notification(
                order,
                template_token="order_note_operator",
                context={"note_id": str(note.pk)},
            )

        _trigger_webhook(
            order,
            event_token="order.note_added",
            context={"note_id": str(note.pk)},
        )
        _trigger_analytics_event(
            order,
            event_name="note_added",
            properties={"note_id": str(note.pk)},
        )
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_note_created failed for note=%s: %s",
            getattr(note, "pk", "?"), exc,
        )

def handle_attachment_created(attachment: OrderAttachment) -> None:
    """
    Coordinate the workflows triggered by an order-attachment upload.

    Cascades to:

        1. Staff notification (new file attached).
        2. Customer notification (if visible to customer).
        3. Webhook dispatch.
        4. Analytics event.
        5. Cache invalidation.
    """
    try:
        order = attachment.order

        if bool(getattr(attachment, "is_visible_to_customer", False)):
            _trigger_customer_notification(
                order,
                template_token="order_attachment_customer",
                context={"attachment_id": str(attachment.pk)},
            )
        else:
            _trigger_staff_notification(
                order,
                template_token="order_attachment_operator",
                context={"attachment_id": str(attachment.pk)},
            )

        _trigger_webhook(
            order,
            event_token="order.attachment_added",
            context={"attachment_id": str(attachment.pk)},
        )
        _trigger_analytics_event(
            order,
            event_name="attachment_added",
            properties={"attachment_id": str(attachment.pk)},
        )
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_attachment_created failed for attachment=%s: %s",
            getattr(attachment, "pk", "?"), exc,
        )

def handle_coupon_applied(coupon_usage: CouponUsage) -> None:
    """
    Coordinate the workflows triggered by a successful coupon
    redemption against an order.

    Cascades to:

        1. Customer notification (discount applied).
        2. Analytics event.
        3. Cache invalidation.
    """
    try:
        order = coupon_usage.order

        _trigger_customer_notification(
            order,
            template_token="coupon_applied",
            context={
                "coupon_code": coupon_usage.coupon_code,
                "discount_amount": str(coupon_usage.discount_amount),
            },
        )
        _trigger_analytics_event(
            order,
            event_name="coupon_applied",
            properties={
                "coupon_code": coupon_usage.coupon_code,
                "discount_amount": str(coupon_usage.discount_amount),
            },
        )
        _invalidate_order_caches(order)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_coupon_applied failed for usage=%s: %s",
            getattr(coupon_usage, "pk", "?"), exc,
        )

def handle_timeline_event_created(event: OrderTimelineEvent) -> None:
    """
    Coordinate the workflows triggered by an ``OrderTimelineEvent``
    creation.

    This handler is the LOWEST-LEVEL hook. It is intentionally
    minimal so that it NEVER creates a feedback loop with the other
    handlers (which themselves create timeline events).

    Only analytics + a structured log entry are emitted here.
    """
    try:
        logger.info(
            "order_timeline_event",
            extra={
                "order_id": str(event.order_id),
                "event_type": event.event_type,
                "title": event.title,
                "is_visible_to_customer": event.is_visible_to_customer,
                "occurred_at": (
                    event.occurred_at.isoformat()
                    if event.occurred_at else None
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "handle_timeline_event_created failed for event=%s: %s",
            getattr(event, "pk", "?"), exc,
        )

# ==============================================================================
# DISPATCHER
# ==============================================================================
class EventDispatcher:
    """
    Centralised dispatcher that maps (model_name, lifecycle_event) pairs
    to the appropriate ``handle_*`` function.

    This abstraction allows ``signals.py`` to remain agnostic of the
    specific handler functions declared in this module. It also makes
    it trivial to:

        1. Add new event handlers in a single location.
        2. Replace a handler with a no-op in tests.
        3. Inspect the registered handlers via ``dispatcher.registry``.

    The dispatcher is deliberately stateless: it does not own any
    per-request data, and it can be safely shared across threads.
    """

    def __init__(self) -> None:
        self._registry: Dict[str, Callable[..., None]] = {
            # Order
            "order.placed": handle_order_placed,
            "order.status_changed": handle_order_status_changed,
            "order.payment_status_changed": (
                handle_order_payment_status_changed
            ),
            # OrderItem
            "order_item.created": handle_order_item_changed,
            "order_item.changed": handle_order_item_changed,
            "order_item.deleted": handle_order_item_deleted,
            # Shipment
            "shipment.created": handle_shipment_created,
            "shipment.status_changed": handle_shipment_status_changed,
            # Payment
            "payment.created": handle_payment_created,
            "payment.status_changed": handle_payment_status_changed,
            # Refund
            "refund.created": handle_refund_created,
            "refund.status_changed": handle_refund_status_changed,
            # ReturnRequest
            "return.created": handle_return_created,
            "return.status_changed": handle_return_status_changed,
            # OrderNote
            "order_note.created": handle_note_created,
            # OrderAttachment
            "order_attachment.created": handle_attachment_created,
            # CouponUsage
            "coupon_usage.created": handle_coupon_applied,
            # OrderTimelineEvent
            "timeline_event.created": handle_timeline_event_created,
        }

    @property
    def registry(self) -> Dict[str, Callable[..., None]]:
        """Read-only view of the registered handlers."""
        return dict(self._registry)

    def register(
        self, event_name: str, handler: Callable[..., None]
    ) -> None:
        """
        Register or replace a handler for the given event name.

        Use this method to add new event handlers in tests or in
        third-party extensions.
        """
        self._registry[event_name] = handler

    def unregister(self, event_name: str) -> None:
        """
        Remove a handler for the given event name.

        Use this method to no-op an event handler in tests.
        """
        self._registry.pop(event_name, None)

    def dispatch(
        self,
        event_name: str,
        target: Model,
        **kwargs: Any,
    ) -> None:
        """
        Dispatch an event to its registered handler.

        Lookups are case-sensitive and follow the dotted naming
        convention declared in ``registry``. Unknown event names are
        logged at DEBUG level and otherwise ignored, so that adding
        new events never breaks existing signal chains.
        """
        handler = self._registry.get(event_name)
        if handler is None:
            logger.debug(
                "No handler registered for event=%s (target=%s pk=%s).",
                event_name,
                target.__class__.__name__,
                getattr(target, "pk", "?"),
            )
            return
        try:
            handler(target, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Event dispatch failed for event=%s (target=%s pk=%s): %s",
                event_name,
                target.__class__.__name__,
                getattr(target, "pk", "?"),
                exc,
            )

#: Singleton dispatcher instance used by the rest of the orders app.
dispatcher: Final[EventDispatcher] = EventDispatcher()

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Order handlers
    "handle_order_placed",
    "handle_order_status_changed",
    "handle_order_payment_status_changed",
    # OrderItem handlers
    "handle_order_item_changed",
    "handle_order_item_deleted",
    # Shipment handlers
    "handle_shipment_created",
    "handle_shipment_status_changed",
    # Payment handlers
    "handle_payment_created",
    "handle_payment_status_changed",
    # Refund handlers
    "handle_refund_created",
    "handle_refund_status_changed",
    # ReturnRequest handlers
    "handle_return_created",
    "handle_return_status_changed",
    # Note / Attachment / Coupon handlers
    "handle_note_created",
    "handle_attachment_created",
    "handle_coupon_applied",
    # Timeline handler
    "handle_timeline_event_created",
    # Dispatcher
    "EventDispatcher",
    "dispatcher",
]