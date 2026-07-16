"""
Enterprise-grade ORM lifecycle signal handlers for the Orders application.

This module coordinates ORM lifecycle events across the entire order
domain. It is the SINGLE place where cross-model audit, timeline, and
synchronization logic is wired together.

ARCHITECTURE
============

The signals in this module are STRICTLY COORDINATORS. They:
    1. Maintain the immutable audit trail
       (OrderStatusHistory, OrderTimelineEvent).
    2. Synchronize denormalized fields (e.g. Order.completed_at).
    3. Detect state transitions and emit appropriate timeline events.
    4. Provide integration points for downstream consumers via
       transaction.on_commit hooks.

They NEVER:
    1. Perform payment processing.
    2. Compute prices, taxes, or discounts.
    3. Mutate inventory.
    4. Send emails, SMS, or webhooks.
    5. Make external HTTP requests.
    6. Run heavy database queries.

All business logic lives in ``services.py`` / ``selectors.py`` /
``tasks.py``. This file contains ONLY event coordination.

REGISTRATION
============
Signal handlers are registered via ``@receiver()`` with explicit
``dispatch_uid`` values to guarantee idempotent registration. The
module is imported by ``apps.py.ready()`` exactly once per process.

PERFORMANCE
===========
* ``pre_save`` handlers cache the previous state in a module-level
  dict to avoid a database roundtrip when ``post_save`` needs to know
  the old state. The cache uses ``.only()`` to fetch only the
  monitored fields.
* ``post_save`` handlers only create timeline events when the
  relevant fields actually changed (dirty-field comparison).
* All cross-model writes are deferred to ``transaction.on_commit``
  to avoid creating events for rolled-back transactions.
* Recursion is prevented by checking for the ``_skip_signals``
  attribute on every instance.
* Bulk operations are handled gracefully: ``bulk_create`` does not
  send signals, and ``bulk_update`` sends per-instance signals that
  are processed correctly by the pre/post save handlers.

SECURITY
========
* No information leakage in exception messages.
* No unsafe exception handling (all exceptions are logged, not
  swallowed silently).
* No mutable globals beyond the deliberately-shared state cache.
* No unsafe imports (models are imported at module top, which is
  safe because ``apps.py.ready()`` runs after model loading).
* No circular imports (services / selectors are NEVER imported here;
  they would create circular dependencies).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from apps.orders import constants as c
from apps.orders.models import (
    CouponUsage,
    Order,
    OrderAttachment,
    OrderItem,
    OrderNote,
    OrderStatusHistory,
    OrderTimelineEvent,
    Payment,
    Refund,
    ReturnRequest,
    Shipment,
)

logger = logging.getLogger(c.LOGGER_SIGNALS)

# ==============================================================================
# INTERNAL STATE CACHE
# ==============================================================================
#: Per-process cache mapping ``"ModelName:pk"`` to a dict of pre-save
#: field values. Used by ``pre_save`` to remember the old state so
#: ``post_save`` can detect dirty fields without a database roundtrip.
#:
#: This is a module-level dict rather than a ``WeakValueDictionary``
#: because:
#:
#:   1. Model instances may not always be weakref-able.
#:   2. The cache is bounded by the number of in-flight saves per
#:      process (typically very small).
#:   3. Entries are explicitly removed in ``post_save`` to prevent
#:      unbounded growth.
_old_state_cache: Dict[str, Dict[str, Any]] = {}

def _cache_key(instance: Any) -> str:
    """
    Generate a stable cache key for an instance.

    For new (unsaved) instances, a transient key based on ``id()``
    is returned. This key is only valid for the duration of a
    single save cycle and is never reused.
    """
    pk = getattr(instance, "pk", None)
    if pk is None:
        return f"new:{instance.__class__.__name__}:{id(instance)}"
    return f"{instance.__class__.__name__}:{pk}"

def _is_signal_disabled(instance: Any) -> bool:
    """
    Return ``True`` if the instance has signals explicitly disabled.

    The convention is: any instance with a truthy ``_skip_signals``
    attribute will be ignored by all signal handlers in this module.
    This is useful for bulk operations, test fixtures, and migration
    data loading.
    """
    return bool(getattr(instance, "_skip_signals", False))

# ==============================================================================
# TIMELINE EVENT HELPER
# ==============================================================================
def _build_timeline_event(
    *,
    order: Order,
    event_type: str,
    title: str,
    description: str = "",
    reference_model: str = "",
    reference_id: str = "",
    is_visible_to_customer: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Create an ``OrderTimelineEvent`` in a way that is safe, idempotent,
    and deferred to transaction commit.

    The actual write is scheduled via ``transaction.on_commit`` to
    ensure the parent transaction commits first. This prevents
    creating timeline events for objects that get rolled back.

    If no active transaction exists (e.g. during tests or management
    commands running outside atomic blocks), the event is created
    immediately.
    """
    def _create() -> None:
        try:
            OrderTimelineEvent.objects.create(
                order=order,
                event_type=event_type,
                title=title,
                description=description or None,
                is_system_event=True,
                is_visible_to_customer=is_visible_to_customer,
                reference_model=reference_model or None,
                reference_id=reference_id or None,
                occurred_at=timezone.now(),
                metadata=metadata or {},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to create OrderTimelineEvent "
                "(order=%s, event_type=%s): %s",
                getattr(order, "pk", "?"), event_type, exc,
            )

    try:
        transaction.on_commit(_create)
    except transaction.TransactionManagementError:
        # No active transaction; execute immediately.
        _create()

# ==============================================================================
# 1. ORDER SIGNALS
# ==============================================================================
@receiver(pre_save, sender=Order, dispatch_uid="orders_pre_save_order")
def orders_pre_save_order(
    sender: type, instance: Order, **kwargs: Any
) -> None:
    """
    Cache the previous state of an ``Order`` and auto-derive
    ``completed_at`` when the order transitions to a terminal-success
    status.

    The auto-derivation of ``completed_at`` is a simple field
    derivation, NOT business logic. It guarantees that any code path
    that sets ``status`` to ``delivered`` or ``completed`` will
    automatically have ``completed_at`` populated.
    """
    if _is_signal_disabled(instance):
        return

    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}

    if instance.pk:
        try:
            old = Order.objects.only(
                "status", "payment_status", "completed_at"
            ).get(pk=instance.pk)
            old_state = {
                "status": old.status,
                "payment_status": old.payment_status,
                "completed_at": old.completed_at,
            }
        except Order.DoesNotExist:
            old_state = {}

    _old_state_cache[cache_key] = old_state

    # Auto-derive completed_at when transitioning to a terminal-success
    # status. This is a simple field derivation, not business logic.
    if instance.status in (c.OrderStatus.DELIVERED, c.OrderStatus.COMPLETED):
        if not instance.completed_at:
            instance.completed_at = timezone.now()

@receiver(post_save, sender=Order, dispatch_uid="orders_post_save_order")
def orders_post_save_order(
    sender: type, instance: Order, created: bool, **kwargs: Any
) -> None:
    """
    Emit an ``ORDER_PLACED`` timeline event on creation, and an
    ``OrderStatusHistory`` row plus a timeline event on every
    subsequent save where ``status`` or ``payment_status`` actually
    changed.
    """
    if _is_signal_disabled(instance):
        _old_state_cache.pop(_cache_key(instance), None)
        return

    cache_key = _cache_key(instance)
    old_state = _old_state_cache.pop(cache_key, {})

    try:
        if created:
            _build_timeline_event(
                order=instance,
                event_type=c.TimelineEventType.ORDER_PLACED,
                title="Order placed",
                description=f"Order {instance.order_number} was created.",
                reference_model="orders.Order",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        # --- Order status transition ---------------------------------
        old_status = old_state.get("status")
        new_status = instance.status
        if old_status and old_status != new_status:
            try:
                old_status_snapshot = old_status
                new_status_snapshot = new_status
                transaction.on_commit(
                    lambda: OrderStatusHistory.objects.create(
                        order=instance,
                        old_status=old_status_snapshot,
                        new_status=new_status_snapshot,
                        remarks="",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Failed to create OrderStatusHistory for order=%s: %s",
                    instance.pk, exc,
                )

            status_event_map: Dict[str, tuple] = {
                c.OrderStatus.PROCESSING: (
                    c.TimelineEventType.ORDER_UPDATED,
                    "Order processing",
                ),
                c.OrderStatus.SHIPPED: (
                    c.TimelineEventType.SHIPMENT_IN_TRANSIT,
                    "Order shipped",
                ),
                c.OrderStatus.DELIVERED: (
                    c.TimelineEventType.ORDER_COMPLETED,
                    "Order delivered",
                ),
                c.OrderStatus.CANCELLED: (
                    c.TimelineEventType.ORDER_CANCELLED,
                    "Order cancelled",
                ),
                c.OrderStatus.REFUNDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Order refunded",
                ),
                c.OrderStatus.ON_HOLD: (
                    c.TimelineEventType.ORDER_UPDATED,
                    "Order on hold",
                ),
                c.OrderStatus.PARTIALLY_SHIPPED: (
                    c.TimelineEventType.SHIPMENT_IN_TRANSIT,
                    "Order partially shipped",
                ),
                c.OrderStatus.PARTIALLY_DELIVERED: (
                    c.TimelineEventType.ORDER_UPDATED,
                    "Order partially delivered",
                ),
                c.OrderStatus.BACKORDERED: (
                    c.TimelineEventType.ORDER_UPDATED,
                    "Order backordered",
                ),
                c.OrderStatus.COMPLETED: (
                    c.TimelineEventType.ORDER_COMPLETED,
                    "Order completed",
                ),
                c.OrderStatus.FAILED: (
                    c.TimelineEventType.ORDER_UPDATED,
                    "Order failed",
                ),
                c.OrderStatus.AWAITING_PAYMENT: (
                    c.TimelineEventType.PAYMENT_INITIATED,
                    "Order awaiting payment",
                ),
                c.OrderStatus.PARTIALLY_REFUNDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Order partially refunded",
                ),
                c.OrderStatus.DISPUTED: (
                    c.TimelineEventType.ORDER_UPDATED,
                    "Order disputed",
                ),
            }
            event_type, title = status_event_map.get(
                new_status,
                (
                    c.TimelineEventType.ORDER_UPDATED,
                    f"Status changed to {new_status}",
                ),
            )
            _build_timeline_event(
                order=instance,
                event_type=event_type,
                title=title,
                description=(
                    f"Status changed from {old_status} to {new_status}."
                ),
                reference_model="orders.Order",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )

        # --- Order payment_status transition --------------------------
        old_payment = old_state.get("payment_status")
        new_payment = instance.payment_status
        if old_payment and old_payment != new_payment:
            payment_event_map: Dict[str, tuple] = {
                c.PaymentStatus.PAID: (
                    c.TimelineEventType.PAYMENT_CAPTURED,
                    "Payment captured",
                ),
                c.PaymentStatus.PARTIALLY_PAID: (
                    c.TimelineEventType.PAYMENT_AUTHORIZED,
                    "Payment partially captured",
                ),
                c.PaymentStatus.FAILED: (
                    c.TimelineEventType.PAYMENT_FAILED,
                    "Payment failed",
                ),
                c.PaymentStatus.REFUNDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Payment refunded",
                ),
                c.PaymentStatus.PARTIALLY_REFUNDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Payment partially refunded",
                ),
                c.PaymentStatus.AUTHORIZED: (
                    c.TimelineEventType.PAYMENT_AUTHORIZED,
                    "Payment authorized",
                ),
                c.PaymentStatus.CAPTURED: (
                    c.TimelineEventType.PAYMENT_CAPTURED,
                    "Payment captured",
                ),
                c.PaymentStatus.VOIDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Payment voided",
                ),
                c.PaymentStatus.DISPUTED: (
                    c.TimelineEventType.PAYMENT_FAILED,
                    "Payment disputed",
                ),
                c.PaymentStatus.EXPIRED: (
                    c.TimelineEventType.PAYMENT_FAILED,
                    "Payment expired",
                ),
            }
            event_type, title = payment_event_map.get(
                new_payment,
                (
                    c.TimelineEventType.PAYMENT_INITIATED,
                    "Payment status updated",
                ),
            )
            _build_timeline_event(
                order=instance,
                event_type=event_type,
                title=title,
                description=(
                    f"Payment status changed from {old_payment} "
                    f"to {new_payment}."
                ),
                reference_model="orders.Order",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Order post_save handler failed for order=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 2. ORDER ITEM SIGNALS
# ==============================================================================
@receiver(pre_save, sender=OrderItem, dispatch_uid="orders_pre_save_order_item")
def orders_pre_save_order_item(
    sender: type, instance: OrderItem, **kwargs: Any
) -> None:
    """Cache the previous ``status`` and ``quantity`` of an ``OrderItem``."""
    if _is_signal_disabled(instance):
        return

    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}

    if instance.pk:
        try:
            old = OrderItem.objects.only("status", "quantity").get(pk=instance.pk)
            old_state = {
                "status": old.status,
                "quantity": old.quantity,
            }
        except OrderItem.DoesNotExist:
            old_state = {}

    _old_state_cache[cache_key] = old_state

@receiver(post_save, sender=OrderItem, dispatch_uid="orders_post_save_order_item")
def orders_post_save_order_item(
    sender: type, instance: OrderItem, created: bool, **kwargs: Any
) -> None:
    """
    Emit an ``ORDER_UPDATED`` timeline event when a line item is
    created or when its status / quantity changes.
    """
    if _is_signal_disabled(instance):
        _old_state_cache.pop(_cache_key(instance), None)
        return

    cache_key = _cache_key(instance)
    old_state = _old_state_cache.pop(cache_key, {})

    try:
        if created:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.ORDER_UPDATED,
                title="Line item added",
                description=(
                    f"Line item {instance.id} was added to the order."
                ),
                reference_model="orders.OrderItem",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        new_status = instance.status
        old_quantity = old_state.get("quantity")
        new_quantity = instance.quantity

        if old_status != new_status or old_quantity != new_quantity:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.ORDER_UPDATED,
                title="Line item updated",
                description=(
                    f"Line item {instance.id} was updated "
                    f"(status: {old_status} \u2192 {new_status}, "
                    f"quantity: {old_quantity} \u2192 {new_quantity})."
                ),
                reference_model="orders.OrderItem",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "OrderItem post_save handler failed for item=%s: %s",
            instance.pk, exc,
        )

@receiver(post_delete, sender=OrderItem, dispatch_uid="orders_post_delete_order_item")
def orders_post_delete_order_item(
    sender: type, instance: OrderItem, **kwargs: Any
) -> None:
    """
    Emit an ``ORDER_UPDATED`` timeline event when a line item is
    permanently removed from an order.
    """
    if _is_signal_disabled(instance):
        return

    try:
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.ORDER_UPDATED,
            title="Line item removed",
            description=f"Line item {instance.id} was removed from the order.",
            reference_model="orders.OrderItem",
            reference_id=str(instance.pk),
            is_visible_to_customer=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "OrderItem post_delete handler failed for item=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 3. SHIPMENT SIGNALS
# ==============================================================================
@receiver(pre_save, sender=Shipment, dispatch_uid="orders_pre_save_shipment")
def orders_pre_save_shipment(
    sender: type, instance: Shipment, **kwargs: Any
) -> None:
    """Cache the previous ``status`` of a ``Shipment``."""
    if _is_signal_disabled(instance):
        return

    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}

    if instance.pk:
        try:
            old = Shipment.objects.only("status").get(pk=instance.pk)
            old_state = {"status": old.status}
        except Shipment.DoesNotExist:
            old_state = {}

    _old_state_cache[cache_key] = old_state

@receiver(post_save, sender=Shipment, dispatch_uid="orders_post_save_shipment")
def orders_post_save_shipment(
    sender: type, instance: Shipment, created: bool, **kwargs: Any
) -> None:
    """
    Emit a ``SHIPMENT_CREATED`` timeline event on creation, and a
    shipment-status timeline event on every subsequent save where
    ``status`` actually changed.
    """
    if _is_signal_disabled(instance):
        _old_state_cache.pop(_cache_key(instance), None)
        return

    cache_key = _cache_key(instance)
    old_state = _old_state_cache.pop(cache_key, {})

    try:
        if created:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.SHIPMENT_CREATED,
                title="Shipment created",
                description=(
                    f"Shipment {instance.shipment_number} was created."
                ),
                reference_model="orders.Shipment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        new_status = instance.status
        if old_status and old_status != new_status:
            shipment_event_map: Dict[str, tuple] = {
                c.ShipmentStatus.DISPATCHED: (
                    c.TimelineEventType.SHIPMENT_PICKED,
                    "Shipment dispatched",
                ),
                c.ShipmentStatus.IN_TRANSIT: (
                    c.TimelineEventType.SHIPMENT_IN_TRANSIT,
                    "Shipment in transit",
                ),
                c.ShipmentStatus.OUT_FOR_DELIVERY: (
                    c.TimelineEventType.SHIPMENT_OUT_FOR_DELIVERY,
                    "Shipment out for delivery",
                ),
                c.ShipmentStatus.DELIVERED: (
                    c.TimelineEventType.SHIPMENT_DELIVERED,
                    "Shipment delivered",
                ),
                c.ShipmentStatus.RETURNED: (
                    c.TimelineEventType.SHIPMENT_RETURNED,
                    "Shipment returned",
                ),
                c.ShipmentStatus.EXCEPTION: (
                    c.TimelineEventType.SHIPMENT_FAILED,
                    "Shipment exception",
                ),
                c.ShipmentStatus.FAILED_ATTEMPT: (
                    c.TimelineEventType.SHIPMENT_FAILED,
                    "Shipment delivery failed",
                ),
                c.ShipmentStatus.AWAITING_PICKUP: (
                    c.TimelineEventType.SHIPMENT_CREATED,
                    "Shipment awaiting pickup",
                ),
                c.ShipmentStatus.PICKED_UP: (
                    c.TimelineEventType.SHIPMENT_PICKED,
                    "Shipment picked up",
                ),
            }
            event_type, title = shipment_event_map.get(
                new_status,
                (
                    c.TimelineEventType.SHIPMENT_IN_TRANSIT,
                    f"Shipment status changed to {new_status}",
                ),
            )
            _build_timeline_event(
                order=instance.order,
                event_type=event_type,
                title=title,
                description=(
                    f"Shipment {instance.shipment_number} status "
                    f"changed from {old_status} to {new_status}."
                ),
                reference_model="orders.Shipment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Shipment post_save handler failed for shipment=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 4. PAYMENT SIGNALS
# ==============================================================================
@receiver(pre_save, sender=Payment, dispatch_uid="orders_pre_save_payment")
def orders_pre_save_payment(
    sender: type, instance: Payment, **kwargs: Any
) -> None:
    """Cache the previous ``status`` of a ``Payment``."""
    if _is_signal_disabled(instance):
        return

    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}

    if instance.pk:
        try:
            old = Payment.objects.only("status", "paid_at").get(pk=instance.pk)
            old_state = {
                "status": old.status,
                "paid_at": old.paid_at,
            }
        except Payment.DoesNotExist:
            old_state = {}

    _old_state_cache[cache_key] = old_state

@receiver(post_save, sender=Payment, dispatch_uid="orders_post_save_payment")
def orders_post_save_payment(
    sender: type, instance: Payment, created: bool, **kwargs: Any
) -> None:
    """
    Emit a ``PAYMENT_INITIATED`` timeline event on creation, and a
    payment-status timeline event on every subsequent save where
    ``status`` actually changed.
    """
    if _is_signal_disabled(instance):
        _old_state_cache.pop(_cache_key(instance), None)
        return

    cache_key = _cache_key(instance)
    old_state = _old_state_cache.pop(cache_key, {})

    try:
        if created:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.PAYMENT_INITIATED,
                title="Payment initiated",
                description=(
                    f"Payment {instance.transaction_id} was initiated."
                ),
                reference_model="orders.Payment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        new_status = instance.status
        if old_status and old_status != new_status:
            payment_event_map: Dict[str, tuple] = {
                c.PaymentState.AUTHORIZED: (
                    c.TimelineEventType.PAYMENT_AUTHORIZED,
                    "Payment authorized",
                ),
                c.PaymentState.CAPTURED: (
                    c.TimelineEventType.PAYMENT_CAPTURED,
                    "Payment captured",
                ),
                c.PaymentState.COMPLETED: (
                    c.TimelineEventType.PAYMENT_CAPTURED,
                    "Payment completed",
                ),
                c.PaymentState.FAILED: (
                    c.TimelineEventType.PAYMENT_FAILED,
                    "Payment failed",
                ),
                c.PaymentState.REFUNDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Payment refunded",
                ),
                c.PaymentState.PARTIALLY_REFUNDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Payment partially refunded",
                ),
                c.PaymentState.VOIDED: (
                    c.TimelineEventType.PAYMENT_REFUNDED,
                    "Payment voided",
                ),
                c.PaymentState.EXPIRED: (
                    c.TimelineEventType.PAYMENT_FAILED,
                    "Payment expired",
                ),
                c.PaymentState.DISPUTED: (
                    c.TimelineEventType.PAYMENT_FAILED,
                    "Payment disputed",
                ),
            }
            event_type, title = payment_event_map.get(
                new_status,
                (
                    c.TimelineEventType.PAYMENT_INITIATED,
                    f"Payment status changed to {new_status}",
                ),
            )
            _build_timeline_event(
                order=instance.order,
                event_type=event_type,
                title=title,
                description=(
                    f"Payment {instance.transaction_id} status "
                    f"changed from {old_status} to {new_status}."
                ),
                reference_model="orders.Payment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Payment post_save handler failed for payment=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 5. REFUND SIGNALS
# ==============================================================================
@receiver(pre_save, sender=Refund, dispatch_uid="orders_pre_save_refund")
def orders_pre_save_refund(
    sender: type, instance: Refund, **kwargs: Any
) -> None:
    """Cache the previous ``status`` of a ``Refund``."""
    if _is_signal_disabled(instance):
        return

    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}

    if instance.pk:
        try:
            old = Refund.objects.only("status").get(pk=instance.pk)
            old_state = {"status": old.status}
        except Refund.DoesNotExist:
            old_state = {}

    _old_state_cache[cache_key] = old_state

@receiver(post_save, sender=Refund, dispatch_uid="orders_post_save_refund")
def orders_post_save_refund(
    sender: type, instance: Refund, created: bool, **kwargs: Any
) -> None:
    """
    Emit a ``REFUND_INITIATED`` timeline event on creation, and a
    refund-status timeline event on every subsequent save where
    ``status`` actually changed.
    """
    if _is_signal_disabled(instance):
        _old_state_cache.pop(_cache_key(instance), None)
        return

    cache_key = _cache_key(instance)
    old_state = _old_state_cache.pop(cache_key, {})

    try:
        if created:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.REFUND_INITIATED,
                title="Refund initiated",
                description=f"Refund #{instance.id} was initiated.",
                reference_model="orders.Refund",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        new_status = instance.status
        if old_status and old_status != new_status:
            refund_event_map: Dict[str, tuple] = {
                c.RefundStatus.APPROVED: (
                    c.TimelineEventType.REFUND_APPROVED,
                    "Refund approved",
                ),
                c.RefundStatus.PROCESSED: (
                    c.TimelineEventType.REFUND_COMPLETED,
                    "Refund processed",
                ),
                c.RefundStatus.REJECTED: (
                    c.TimelineEventType.REFUND_REJECTED,
                    "Refund rejected",
                ),
                c.RefundStatus.PENDING: (
                    c.TimelineEventType.REFUND_INITIATED,
                    "Refund pending gateway",
                ),
                c.RefundStatus.FAILED: (
                    c.TimelineEventType.REFUND_INITIATED,
                    "Refund failed",
                ),
                c.RefundStatus.CANCELLED: (
                    c.TimelineEventType.REFUND_REJECTED,
                    "Refund cancelled",
                ),
            }
            event_type, title = refund_event_map.get(
                new_status,
                (
                    c.TimelineEventType.REFUND_INITIATED,
                    f"Refund status changed to {new_status}",
                ),
            )
            _build_timeline_event(
                order=instance.order,
                event_type=event_type,
                title=title,
                description=(
                    f"Refund #{instance.id} status changed "
                    f"from {old_status} to {new_status}."
                ),
                reference_model="orders.Refund",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Refund post_save handler failed for refund=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 6. RETURN REQUEST SIGNALS
# ==============================================================================
@receiver(pre_save, sender=ReturnRequest, dispatch_uid="orders_pre_save_return_request")
def orders_pre_save_return_request(
    sender: type, instance: ReturnRequest, **kwargs: Any
) -> None:
    """Cache the previous ``status`` of a ``ReturnRequest``."""
    if _is_signal_disabled(instance):
        return

    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}

    if instance.pk:
        try:
            old = ReturnRequest.objects.only("status").get(pk=instance.pk)
            old_state = {"status": old.status}
        except ReturnRequest.DoesNotExist:
            old_state = {}

    _old_state_cache[cache_key] = old_state

@receiver(post_save, sender=ReturnRequest, dispatch_uid="orders_post_save_return_request")
def orders_post_save_return_request(
    sender: type, instance: ReturnRequest, created: bool, **kwargs: Any
) -> None:
    """
    Emit a ``RETURN_REQUESTED`` timeline event on creation, and a
    return-status timeline event on every subsequent save where
    ``status`` actually changed.
    """
    if _is_signal_disabled(instance):
        _old_state_cache.pop(_cache_key(instance), None)
        return

    cache_key = _cache_key(instance)
    old_state = _old_state_cache.pop(cache_key, {})

    try:
        if created:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.RETURN_REQUESTED,
                title="Return requested",
                description=(
                    f"Return request "
                    f"{instance.return_number or instance.id} "
                    f"was created."
                ),
                reference_model="orders.ReturnRequest",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        new_status = instance.status
        if old_status and old_status != new_status:
            return_event_map: Dict[str, tuple] = {
                c.ReturnStatus.REQUESTED: (
                    c.TimelineEventType.RETURN_REQUESTED,
                    "Return requested",
                ),
                c.ReturnStatus.UNDER_REVIEW: (
                    c.TimelineEventType.RETURN_REQUESTED,
                    "Return under review",
                ),
                c.ReturnStatus.APPROVED: (
                    c.TimelineEventType.RETURN_APPROVED,
                    "Return approved",
                ),
                c.ReturnStatus.REJECTED: (
                    c.TimelineEventType.RETURN_REJECTED,
                    "Return rejected",
                ),
                c.ReturnStatus.AWAITING_SHIPMENT: (
                    c.TimelineEventType.RETURN_APPROVED,
                    "Return awaiting shipment",
                ),
                c.ReturnStatus.IN_TRANSIT: (
                    c.TimelineEventType.RETURN_APPROVED,
                    "Return in transit",
                ),
                c.ReturnStatus.RECEIVED: (
                    c.TimelineEventType.RETURN_RECEIVED,
                    "Return received",
                ),
                c.ReturnStatus.INSPECTING: (
                    c.TimelineEventType.RETURN_RECEIVED,
                    "Return inspecting",
                ),
                c.ReturnStatus.REFUND_INITIATED: (
                    c.TimelineEventType.RETURN_RECEIVED,
                    "Return refund initiated",
                ),
                c.ReturnStatus.COMPLETED: (
                    c.TimelineEventType.RETURN_COMPLETED,
                    "Return completed",
                ),
                c.ReturnStatus.CANCELLED: (
                    c.TimelineEventType.RETURN_REJECTED,
                    "Return cancelled",
                ),
            }
            event_type, title = return_event_map.get(
                new_status,
                (
                    c.TimelineEventType.RETURN_REQUESTED,
                    f"Return status changed to {new_status}",
                ),
            )
            _build_timeline_event(
                order=instance.order,
                event_type=event_type,
                title=title,
                description=(
                    f"Return {instance.return_number or instance.id} "
                    f"status changed from {old_status} to {new_status}."
                ),
                reference_model="orders.ReturnRequest",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "ReturnRequest post_save handler failed for return=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 7. ORDER NOTE SIGNALS
# ==============================================================================
@receiver(post_save, sender=OrderNote, dispatch_uid="orders_post_save_order_note")
def orders_post_save_order_note(
    sender: type, instance: OrderNote, created: bool, **kwargs: Any
) -> None:
    """
    Emit a ``NOTE_ADDED`` timeline event when a new note is attached
    to an order. Customer-facing visibility mirrors the note's own
    ``is_visible_to_customer`` flag.
    """
    if _is_signal_disabled(instance):
        return

    if not created:
        return

    try:
        is_visible = bool(instance.is_visible_to_customer)
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.NOTE_ADDED,
            title=f"Note added ({instance.get_note_type_display()})",
            description="A new note was added to this order.",
            reference_model="orders.OrderNote",
            reference_id=str(instance.pk),
            is_visible_to_customer=is_visible,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "OrderNote post_save handler failed for note=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 8. ORDER ATTACHMENT SIGNALS
# ==============================================================================
@receiver(post_save, sender=OrderAttachment, dispatch_uid="orders_post_save_order_attachment")
def orders_post_save_order_attachment(
    sender: type, instance: OrderAttachment, created: bool, **kwargs: Any
) -> None:
    """
    Emit an ``ATTACHMENT_ADDED`` timeline event when a new file is
    attached to an order. Customer-facing visibility mirrors the
    attachment's own ``is_visible_to_customer`` flag.
    """
    if _is_signal_disabled(instance):
        return

    if not created:
        return

    try:
        is_visible = bool(instance.is_visible_to_customer)
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.ATTACHMENT_ADDED,
            title=(
                f"Attachment added "
                f"({instance.get_attachment_type_display()})"
            ),
            description=(
                f"File '{instance.original_filename}' was attached "
                f"to this order."
            ),
            reference_model="orders.OrderAttachment",
            reference_id=str(instance.pk),
            is_visible_to_customer=is_visible,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "OrderAttachment post_save handler failed for attachment=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# 9. COUPON USAGE SIGNALS
# ==============================================================================
@receiver(post_save, sender=CouponUsage, dispatch_uid="orders_post_save_coupon_usage")
def orders_post_save_coupon_usage(
    sender: type, instance: CouponUsage, created: bool, **kwargs: Any
) -> None:
    """
    Emit a ``DISCOUNT_APPLIED`` timeline event when a coupon is
    successfully redeemed against an order.
    """
    if _is_signal_disabled(instance):
        return

    if not created:
        return

    try:
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.DISCOUNT_APPLIED,
            title="Coupon applied",
            description=(
                f"Coupon code '{instance.coupon_code}' was applied "
                f"to this order."
            ),
            reference_model="orders.CouponUsage",
            reference_id=str(instance.pk),
            is_visible_to_customer=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "CouponUsage post_save handler failed for usage=%s: %s",
            instance.pk, exc,
        )

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Order signals
    "orders_pre_save_order",
    "orders_post_save_order",
    # OrderItem signals
    "orders_pre_save_order_item",
    "orders_post_save_order_item",
    "orders_post_delete_order_item",
    # Shipment signals
    "orders_pre_save_shipment",
    "orders_post_save_shipment",
    # Payment signals
    "orders_pre_save_payment",
    "orders_post_save_payment",
    # Refund signals
    "orders_pre_save_refund",
    "orders_post_save_refund",
    # ReturnRequest signals
    "orders_pre_save_return_request",
    "orders_post_save_return_request",
    # OrderNote signals
    "orders_post_save_order_note",
    # OrderAttachment signals
    "orders_post_save_order_attachment",
    # CouponUsage signals
    "orders_post_save_coupon_usage",
]