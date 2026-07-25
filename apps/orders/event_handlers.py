from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Final, Optional

from django.db import transaction
from django.db.models import Model

from apps.orders import constants as c
from apps.orders.models import (
    CouponUsage, Order, OrderAttachment, OrderItem, OrderNote,
    OrderTimelineEvent, Payment, Refund, ReturnRequest, Shipment,
)

logger = logging.getLogger(c.LOGGER_SIGNALS)

def _try_import_attr(module_path: str, attr: str) -> Optional[Any]:
    try:
        parts = module_path.split(".")
        module = __import__(module_path, fromlist=[parts[-1]])
        return getattr(module, attr, None)
    except Exception:
        return None

def _invalidate_order_caches(order: Order) -> None:
    if not order or not order.pk:
        return
    try:
        from django.core.cache import cache
        cache.delete(c.CACHE_KEY_ORDER_BY_ID.format(ns=c.CACHE_NAMESPACE, order_id=str(order.pk)))
        cache.delete(c.CACHE_KEY_ORDER_BY_NUMBER.format(ns=c.CACHE_NAMESPACE, order_number=order.order_number))
        cache.delete(c.CACHE_KEY_ORDER_TIMELINE.format(ns=c.CACHE_NAMESPACE, order_id=str(order.pk)))
        cache.delete(c.CACHE_KEY_ORDER_COUNT.format(ns=c.CACHE_NAMESPACE))
    except Exception as exc:
        logger.debug("Cache delete failed for order=%s: %s", order.pk, exc)

def _enqueue_task(task_path: str, *args: Any, **kwargs: Any) -> None:
    def _do() -> None:
        try:
            task = _try_import_attr(task_path, "delay") or _try_import_attr(task_path, "apply_async")
            if task:
                task(*args, **kwargs)
        except Exception as exc:
            logger.exception("Failed to enqueue task %s: %s", task_path, exc)

    try:
        transaction.on_commit(_do)
    except transaction.TransactionManagementError:
        _do()

def handle_order_placed(order: Order) -> None:
    try:
        _enqueue_task("apps.notifications.tasks.send_customer_notification", order_id=str(order.pk), template="order_placed", context={"order_number": order.order_number})
        _enqueue_task("apps.notifications.tasks.send_staff_notification", order_id=str(order.pk), template="new_order_alert", context={"order_number": order.order_number})
        _enqueue_task("apps.webhooks.tasks.dispatch_order_webhook", order_id=str(order.pk), event="order.placed", context={"order_number": order.order_number})
        _enqueue_task("apps.analytics.tasks.track_order_event", order_id=str(order.pk), event="order_placed", properties={"source": order.source})
        _enqueue_task("apps.search.tasks.reindex_order", order_id=str(order.pk))
        _invalidate_order_caches(order)
    except Exception as exc:
        logger.exception("handle_order_placed failed: %s", exc)

def handle_order_status_changed(order: Order, *, old_status: Optional[str] = None, new_status: Optional[str] = None) -> None:
    try:
        _enqueue_task("apps.notifications.tasks.send_customer_notification", order_id=str(order.pk), template=f"order_{new_status}", context={"order_number": order.order_number, "old_status": old_status, "new_status": new_status})
        _enqueue_task("apps.webhooks.tasks.dispatch_order_webhook", order_id=str(order.pk), event=f"order.{new_status}", context={"order_number": order.order_number})
        _enqueue_task("apps.analytics.tasks.track_order_event", order_id=str(order.pk), event="order_status_changed", properties={"old_status": old_status, "new_status": new_status})
        _enqueue_task("apps.search.tasks.reindex_order", order_id=str(order.pk))
        _invalidate_order_caches(order)
    except Exception as exc:
        logger.exception("handle_order_status_changed failed: %s", exc)

def handle_order_payment_status_changed(order: Order, *, old_payment_status: Optional[str] = None, new_payment_status: Optional[str] = None) -> None:
    try:
        _enqueue_task("apps.notifications.tasks.send_customer_notification", order_id=str(order.pk), template=f"payment_{new_payment_status}", context={"order_number": order.order_number})
        _invalidate_order_caches(order)
    except Exception as exc:
        logger.exception("handle_order_payment_status_changed failed: %s", exc)

def handle_order_item_changed(item: OrderItem, *, is_creation: bool = False) -> None:
    try:
        _invalidate_order_caches(item.order)
    except Exception as exc:
        logger.exception("handle_order_item_changed failed: %s", exc)

def handle_order_item_deleted(item: OrderItem) -> None:
    try:
        _invalidate_order_caches(item.order)
    except Exception as exc:
        logger.exception("handle_order_item_deleted failed: %s", exc)

def handle_shipment_created(shipment: Shipment) -> None:
    try:
        _invalidate_order_caches(shipment.order)
    except Exception as exc:
        logger.exception("handle_shipment_created failed: %s", exc)

def handle_shipment_status_changed(shipment: Shipment, *, old_status: Optional[str] = None, new_status: Optional[str] = None) -> None:
    try:
        _invalidate_order_caches(shipment.order)
    except Exception as exc:
        logger.exception("handle_shipment_status_changed failed: %s", exc)

def handle_payment_created(payment: Payment) -> None:
    try:
        _invalidate_order_caches(payment.order)
    except Exception as exc:
        logger.exception("handle_payment_created failed: %s", exc)

def handle_payment_status_changed(payment: Payment, *, old_status: Optional[str] = None, new_status: Optional[str] = None) -> None:
    try:
        _invalidate_order_caches(payment.order)
    except Exception as exc:
        logger.exception("handle_payment_status_changed failed: %s", exc)

def handle_refund_created(refund: Refund) -> None:
    try:
        _invalidate_order_caches(refund.order)
    except Exception as exc:
        logger.exception("handle_refund_created failed: %s", exc)

def handle_refund_status_changed(refund: Refund, *, old_status: Optional[str] = None, new_status: Optional[str] = None) -> None:
    try:
        _invalidate_order_caches(refund.order)
    except Exception as exc:
        logger.exception("handle_refund_status_changed failed: %s", exc)

def handle_return_created(return_request: ReturnRequest) -> None:
    try:
        _invalidate_order_caches(return_request.order)
    except Exception as exc:
        logger.exception("handle_return_created failed: %s", exc)

def handle_return_status_changed(return_request: ReturnRequest, *, old_status: Optional[str] = None, new_status: Optional[str] = None) -> None:
    try:
        _invalidate_order_caches(return_request.order)
    except Exception as exc:
        logger.exception("handle_return_status_changed failed: %s", exc)

def handle_note_created(note: OrderNote) -> None:
    try:
        _invalidate_order_caches(note.order)
    except Exception as exc:
        logger.exception("handle_note_created failed: %s", exc)

def handle_attachment_created(attachment: OrderAttachment) -> None:
    try:
        _invalidate_order_caches(attachment.order)
    except Exception as exc:
        logger.exception("handle_attachment_created failed: %s", exc)

def handle_coupon_applied(coupon_usage: CouponUsage) -> None:
    try:
        _invalidate_order_caches(coupon_usage.order)
    except Exception as exc:
        logger.exception("handle_coupon_applied failed: %s", exc)

def handle_timeline_event_created(event: OrderTimelineEvent) -> None:
    pass

class EventDispatcher:
    def __init__(self) -> None:
        self._registry: Dict[str, Callable[..., None]] = {
            "order.placed": handle_order_placed,
            "order.status_changed": handle_order_status_changed,
            "order.payment_status_changed": handle_order_payment_status_changed,
            "order_item.created": handle_order_item_changed,
            "order_item.changed": handle_order_item_changed,
            "order_item.deleted": handle_order_item_deleted,
            "shipment.created": handle_shipment_created,
            "shipment.status_changed": handle_shipment_status_changed,
            "payment.created": handle_payment_created,
            "payment.status_changed": handle_payment_status_changed,
            "refund.created": handle_refund_created,
            "refund.status_changed": handle_refund_status_changed,
            "return.created": handle_return_created,
            "return.status_changed": handle_return_status_changed,
            "order_note.created": handle_note_created,
            "order_attachment.created": handle_attachment_created,
            "coupon_usage.created": handle_coupon_applied,
            "timeline_event.created": handle_timeline_event_created,
        }

    def dispatch(self, event_name: str, target: Model, **kwargs: Any) -> None:
        handler = self._registry.get(event_name)
        if handler:
            try:
                handler(target, **kwargs)
            except Exception as exc:
                logger.exception("Event dispatch failed for %s: %s", event_name, exc)

dispatcher: Final[EventDispatcher] = EventDispatcher()

__all__ = [
    "handle_order_placed", "handle_order_status_changed", "handle_order_payment_status_changed",
    "handle_order_item_changed", "handle_order_item_deleted", "handle_shipment_created",
    "handle_shipment_status_changed", "handle_payment_created", "handle_payment_status_changed",
    "handle_refund_created", "handle_refund_status_changed", "handle_return_created",
    "handle_return_status_changed", "handle_note_created", "handle_attachment_created",
    "handle_coupon_applied", "handle_timeline_event_created", "EventDispatcher", "dispatcher",
]