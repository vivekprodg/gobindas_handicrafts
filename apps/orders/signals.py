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

_old_state_cache: Dict[str, Dict[str, Any]] = {}

def _cache_key(instance: Any) -> str:
    pk = getattr(instance, "pk", None)
    if pk is None:
        return f"new:{instance.__class__.__name__}:{id(instance)}"
    return f"{instance.__class__.__name__}:{pk}"

def _is_signal_disabled(instance: Any) -> bool:
    return bool(getattr(instance, "_skip_signals", False))

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
        except Exception as exc:
            logger.exception("Failed to create OrderTimelineEvent: %s", exc)

    try:
        transaction.on_commit(_create)
    except transaction.TransactionManagementError:
        _create()

@receiver(pre_save, sender=Order, dispatch_uid="orders_pre_save_order")
def orders_pre_save_order(sender: type, instance: Order, **kwargs: Any) -> None:
    if _is_signal_disabled(instance):
        return
    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}
    if instance.pk:
        try:
            old = Order.objects.only("status", "payment_status", "completed_at").get(pk=instance.pk)
            old_state = {
                "status": old.status,
                "payment_status": old.payment_status,
                "completed_at": old.completed_at,
            }
        except Order.DoesNotExist:
            old_state = {}
    _old_state_cache[cache_key] = old_state

    if instance.status in (c.OrderStatus.DELIVERED, c.OrderStatus.COMPLETED) and not instance.completed_at:
        instance.completed_at = timezone.now()

@receiver(post_save, sender=Order, dispatch_uid="orders_post_save_order")
def orders_post_save_order(sender: type, instance: Order, created: bool, **kwargs: Any) -> None:
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
                title="Order Placed",
                description=f"Order #{instance.order_number} was created.",
                reference_model="orders.Order",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        new_status = instance.status
        if old_status and old_status != new_status:
            _build_timeline_event(
                order=instance,
                event_type=c.TimelineEventType.ORDER_UPDATED,
                title=f"Order Status: {new_status.title()}",
                description=f"Status changed from {old_status} to {new_status}.",
                reference_model="orders.Order",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )

        old_payment = old_state.get("payment_status")
        new_payment = instance.payment_status
        if old_payment and old_payment != new_payment:
            _build_timeline_event(
                order=instance,
                event_type=c.TimelineEventType.PAYMENT_CAPTURED if new_payment == c.PaymentStatus.PAID else c.TimelineEventType.PAYMENT_INITIATED,
                title=f"Payment Status: {new_payment.title()}",
                description=f"Payment status changed from {old_payment} to {new_payment}.",
                reference_model="orders.Order",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:
        logger.exception("Order post_save signal failed: %s", exc)

@receiver(pre_save, sender=OrderItem, dispatch_uid="orders_pre_save_order_item")
def orders_pre_save_order_item(sender: type, instance: OrderItem, **kwargs: Any) -> None:
    if _is_signal_disabled(instance):
        return
    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}
    if instance.pk:
        try:
            old = OrderItem.objects.only("status", "quantity").get(pk=instance.pk)
            old_state = {"status": old.status, "quantity": old.quantity}
        except OrderItem.DoesNotExist:
            old_state = {}
    _old_state_cache[cache_key] = old_state

@receiver(post_save, sender=OrderItem, dispatch_uid="orders_post_save_order_item")
def orders_post_save_order_item(sender: type, instance: OrderItem, created: bool, **kwargs: Any) -> None:
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
                title="Item Added",
                description=f"Added {instance.quantity} x {instance.product_name_snapshot or 'Item'}.",
                reference_model="orders.OrderItem",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        old_qty = old_state.get("quantity")
        if old_status != instance.status or old_qty != instance.quantity:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.ORDER_UPDATED,
                title="Item Updated",
                description=f"Updated item #{instance.pk}.",
                reference_model="orders.OrderItem",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:
        logger.exception("OrderItem post_save signal failed: %s", exc)

@receiver(post_delete, sender=OrderItem, dispatch_uid="orders_post_delete_order_item")
def orders_post_delete_order_item(sender: type, instance: OrderItem, **kwargs: Any) -> None:
    if _is_signal_disabled(instance):
        return
    try:
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.ORDER_UPDATED,
            title="Item Removed",
            description=f"Removed item #{instance.pk}.",
            reference_model="orders.OrderItem",
            reference_id=str(instance.pk),
            is_visible_to_customer=True,
        )
    except Exception as exc:
        logger.exception("OrderItem post_delete signal failed: %s", exc)

@receiver(pre_save, sender=Shipment, dispatch_uid="orders_pre_save_shipment")
def orders_pre_save_shipment(sender: type, instance: Shipment, **kwargs: Any) -> None:
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
def orders_post_save_shipment(sender: type, instance: Shipment, created: bool, **kwargs: Any) -> None:
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
                title="Shipment Created",
                description=f"Shipment #{instance.shipment_number} created.",
                reference_model="orders.Shipment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        if old_status and old_status != instance.status:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.SHIPMENT_IN_TRANSIT if instance.status == c.ShipmentStatus.IN_TRANSIT else c.TimelineEventType.SHIPMENT_DELIVERED,
                title=f"Shipment: {instance.get_status_display()}",
                description=f"Shipment #{instance.shipment_number} status changed to {instance.status}.",
                reference_model="orders.Shipment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:
        logger.exception("Shipment post_save signal failed: %s", exc)

@receiver(pre_save, sender=Payment, dispatch_uid="orders_pre_save_payment")
def orders_pre_save_payment(sender: type, instance: Payment, **kwargs: Any) -> None:
    if _is_signal_disabled(instance):
        return
    cache_key = _cache_key(instance)
    old_state: Dict[str, Any] = {}
    if instance.pk:
        try:
            old = Payment.objects.only("status", "paid_at").get(pk=instance.pk)
            old_state = {"status": old.status, "paid_at": old.paid_at}
        except Payment.DoesNotExist:
            old_state = {}
    _old_state_cache[cache_key] = old_state

@receiver(post_save, sender=Payment, dispatch_uid="orders_post_save_payment")
def orders_post_save_payment(sender: type, instance: Payment, created: bool, **kwargs: Any) -> None:
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
                title="Payment Initiated",
                description=f"Payment txn {instance.transaction_id} initiated.",
                reference_model="orders.Payment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        if old_status and old_status != instance.status:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.PAYMENT_CAPTURED if instance.status in {c.PaymentState.CAPTURED, c.PaymentState.COMPLETED} else c.TimelineEventType.PAYMENT_FAILED,
                title=f"Payment: {instance.get_status_display() if hasattr(instance, 'get_status_display') else instance.status}",
                description=f"Payment txn {instance.transaction_id} changed from {old_status} to {instance.status}.",
                reference_model="orders.Payment",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:
        logger.exception("Payment post_save signal failed: %s", exc)

@receiver(pre_save, sender=Refund, dispatch_uid="orders_pre_save_refund")
def orders_pre_save_refund(sender: type, instance: Refund, **kwargs: Any) -> None:
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
def orders_post_save_refund(sender: type, instance: Refund, created: bool, **kwargs: Any) -> None:
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
                title="Refund Requested",
                description=f"Refund #{instance.pk} requested for amount {instance.amount}.",
                reference_model="orders.Refund",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        if old_status and old_status != instance.status:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.REFUND_COMPLETED if instance.status == c.RefundStatus.PROCESSED else c.TimelineEventType.REFUND_INITIATED,
                title=f"Refund: {instance.status.title()}",
                description=f"Refund #{instance.pk} status changed to {instance.status}.",
                reference_model="orders.Refund",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:
        logger.exception("Refund post_save signal failed: %s", exc)

@receiver(pre_save, sender=ReturnRequest, dispatch_uid="orders_pre_save_return_request")
def orders_pre_save_return_request(sender: type, instance: ReturnRequest, **kwargs: Any) -> None:
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
def orders_post_save_return_request(sender: type, instance: ReturnRequest, created: bool, **kwargs: Any) -> None:
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
                title="Return Requested",
                description=f"Return request {instance.return_number or instance.pk} submitted.",
                reference_model="orders.ReturnRequest",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
            return

        old_status = old_state.get("status")
        if old_status and old_status != instance.status:
            _build_timeline_event(
                order=instance.order,
                event_type=c.TimelineEventType.RETURN_COMPLETED if instance.status == c.ReturnStatus.COMPLETED else c.TimelineEventType.RETURN_APPROVED,
                title=f"Return: {instance.status.title()}",
                description=f"Return request {instance.return_number or instance.pk} status changed to {instance.status}.",
                reference_model="orders.ReturnRequest",
                reference_id=str(instance.pk),
                is_visible_to_customer=True,
            )
    except Exception as exc:
        logger.exception("ReturnRequest post_save signal failed: %s", exc)

@receiver(post_save, sender=OrderNote, dispatch_uid="orders_post_save_order_note")
def orders_post_save_order_note(sender: type, instance: OrderNote, created: bool, **kwargs: Any) -> None:
    if _is_signal_disabled(instance) or not created:
        return
    try:
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.NOTE_ADDED,
            title="Note Added",
            description=f"Note added by {instance.author.username if instance.author else 'System'}.",
            reference_model="orders.OrderNote",
            reference_id=str(instance.pk),
            is_visible_to_customer=bool(instance.is_visible_to_customer),
        )
    except Exception as exc:
        logger.exception("OrderNote post_save signal failed: %s", exc)

@receiver(post_save, sender=OrderAttachment, dispatch_uid="orders_post_save_order_attachment")
def orders_post_save_order_attachment(sender: type, instance: OrderAttachment, created: bool, **kwargs: Any) -> None:
    if _is_signal_disabled(instance) or not created:
        return
    try:
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.ATTACHMENT_ADDED,
            title="Attachment Uploaded",
            description=f"Attached file: {instance.original_filename}.",
            reference_model="orders.OrderAttachment",
            reference_id=str(instance.pk),
            is_visible_to_customer=bool(instance.is_visible_to_customer),
        )
    except Exception as exc:
        logger.exception("OrderAttachment post_save signal failed: %s", exc)

@receiver(post_save, sender=CouponUsage, dispatch_uid="orders_post_save_coupon_usage")
def orders_post_save_coupon_usage(sender: type, instance: CouponUsage, created: bool, **kwargs: Any) -> None:
    if _is_signal_disabled(instance) or not created:
        return
    try:
        _build_timeline_event(
            order=instance.order,
            event_type=c.TimelineEventType.DISCOUNT_APPLIED,
            title="Coupon Applied",
            description=f"Redeemed coupon code '{instance.coupon_code}'.",
            reference_model="orders.CouponUsage",
            reference_id=str(instance.pk),
            is_visible_to_customer=True,
        )
    except Exception as exc:
        logger.exception("CouponUsage post_save signal failed: %s", exc)

__all__ = [
    "orders_pre_save_order", "orders_post_save_order", "orders_pre_save_order_item",
    "orders_post_save_order_item", "orders_post_delete_order_item", "orders_pre_save_shipment",
    "orders_post_save_shipment", "orders_pre_save_payment", "orders_post_save_payment",
    "orders_pre_save_refund", "orders_post_save_refund", "orders_pre_save_return_request",
    "orders_post_save_return_request", "orders_post_save_order_note",
    "orders_post_save_order_attachment", "orders_post_save_coupon_usage",
]