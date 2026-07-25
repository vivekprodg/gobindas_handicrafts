"""
Django signal handlers for cache invalidation and order lifecycle integration.
"""
from __future__ import annotations

import logging
from typing import Any
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.orders.models import Order
from . import constants as c
from .events import handle_coupon_deleted, handle_coupon_saved
from .models import Coupon, CouponCMSSetting
from .services import CouponApplicationService

logger = logging.getLogger(c.LOGGER_NAME)

@receiver(post_save, sender=Coupon)
def on_coupon_saved(sender: Any, instance: Coupon, created: bool, **kwargs: Any) -> None:
    handle_coupon_saved(instance)

@receiver(post_delete, sender=Coupon)
def on_coupon_deleted(sender: Any, instance: Coupon, **kwargs: Any) -> None:
    handle_coupon_deleted(instance)

@receiver(post_save, sender=CouponCMSSetting)
def on_coupon_cms_setting_saved(sender: Any, instance: CouponCMSSetting, **kwargs: Any) -> None:
    handle_coupon_saved(None)

@receiver(post_save, sender=Order)
def on_order_status_updated_for_coupons(sender: Any, instance: Order, created: bool, **kwargs: Any) -> None:
    """
    Listens to Order status changes to finalize coupon redemptions or process reversals.
    """
    if not instance or not getattr(instance, "pk", None):
        return

    try:
        # 1. Reverse coupon on cancellation or refund
        if instance.status in {Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED, Order.OrderStatus.FAILED}:
            CouponApplicationService.reverse_coupon_redemption_for_order(
                order=instance,
                reason=f"Order status changed to '{instance.status}'."
            )

        # 2. Record redemption if order placed and not yet recorded
        elif instance.coupon_code and not instance.coupon_redemption_records.filter(is_reversed=False).exists():
            if instance.status not in {Order.OrderStatus.DRAFT, Order.OrderStatus.CANCELLED, Order.OrderStatus.FAILED}:
                CouponApplicationService.record_coupon_redemption_for_order(
                    order=instance,
                    user=instance.customer
                )
    except Exception as exc:
        logger.exception("Failed to process coupon signal for Order #%s: %s", instance.pk, exc)