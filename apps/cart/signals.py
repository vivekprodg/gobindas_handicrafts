"""
Event-driven orchestration and cache invalidation signals for the Cart application.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import Receiver, Signal, receiver

from .models import Cart, CartItem

logger = logging.getLogger(__name__)

CART_GLOBAL_SUMMARY_KEY = "cart:global:summary:v1"
CART_CUSTOMER_KEY_PREFIX = "cart:customer:"
CART_SESSION_KEY_PREFIX = "cart:session:"
CART_TOKEN_KEY_PREFIX = "cart:token:"

_state_lock = threading.local()

def _safe_log(scope: str, exc: Exception, **extra: Any) -> None:
    try:
        logger.error("Cart signal failure [%s]: %s | extra=%s", scope, exc, extra, exc_info=True)
    except Exception:
        pass

def _safe_cache_delete(key: str) -> None:
    if key:
        try:
            cache.delete(key)
        except Exception as exc:
            _safe_log("cache.delete", exc, key=key)

def _safe_touch_cart(cart_id: Any) -> None:
    if cart_id is None:
        return
    try:
        from django.utils import timezone
        Cart.objects.filter(pk=cart_id).update(last_activity_at=timezone.now())
    except Exception as exc:
        _safe_log("cart.touch", exc, cart_id=cart_id)

# Domain Signals
cart_merged = Signal()
cart_reservation_changed = Signal()

@receiver([post_save, post_delete], sender=Cart)
def invalidate_cart_cache_on_cart_change(sender: Any, instance: Cart, **kwargs: Any) -> None:
    try:
        if kwargs.get("raw", False):
            return

        _safe_cache_delete(CART_GLOBAL_SUMMARY_KEY)
        if instance.customer_id:
            _safe_cache_delete(f"{CART_CUSTOMER_KEY_PREFIX}{instance.customer_id}")
        if instance.session_key:
            _safe_cache_delete(f"{CART_SESSION_KEY_PREFIX}{instance.session_key}")
        if instance.anonymous_token:
            _safe_cache_delete(f"{CART_TOKEN_KEY_PREFIX}{instance.anonymous_token}")
    except Exception as exc:
        _safe_log("invalidate_cart_cache_on_cart_change", exc, cart_id=getattr(instance, "id", None))

@receiver([post_save, post_delete], sender=CartItem)
def touch_cart_on_item_change(sender: Any, instance: CartItem, **kwargs: Any) -> None:
    try:
        cart_id = getattr(instance, "cart_id", None)
        if cart_id is None:
            return

        _safe_cache_delete(CART_GLOBAL_SUMMARY_KEY)
        cart = getattr(instance, "cart", None)
        if cart is not None:
            if cart.customer_id:
                _safe_cache_delete(f"{CART_CUSTOMER_KEY_PREFIX}{cart.customer_id}")
            if cart.session_key:
                _safe_cache_delete(f"{CART_SESSION_KEY_PREFIX}{cart.session_key}")
            if cart.anonymous_token:
                _safe_cache_delete(f"{CART_TOKEN_KEY_PREFIX}{cart.anonymous_token}")
    except Exception as exc:
        _safe_log("touch_cart_on_item_change", exc, cart_id=getattr(instance, "cart_id", None))

@receiver(post_save, sender=CartItem)
def touch_cart_on_item_create(sender: Any, instance: CartItem, created: bool, **kwargs: Any) -> None:
    if created:
        _safe_touch_cart(getattr(instance, "cart_id", None))

@receiver(post_save, sender=Cart)
def handle_cart_state_transition(sender: Any, instance: Cart, created: bool, update_fields: Any = None, **kwargs: Any) -> None:
    if created:
        return
    if update_fields is not None and "status" not in update_fields:
        return
    _safe_touch_cart(instance.id)

@receiver(post_save, sender=Cart)
def handle_cart_converted_event(sender: Any, instance: Cart, created: bool, update_fields: Any = None, **kwargs: Any) -> None:
    if created or getattr(instance, "status", None) != Cart.CartStatus.CONVERTED:
        return
    _safe_touch_cart(instance.id)

@receiver(post_delete, sender=Cart)
def handle_cart_deletion_cleanup(sender: Any, instance: Cart, **kwargs: Any) -> None:
    _safe_cache_delete(CART_GLOBAL_SUMMARY_KEY)
    if instance.customer_id:
        _safe_cache_delete(f"{CART_CUSTOMER_KEY_PREFIX}{instance.customer_id}")

@receiver(post_delete, sender=CartItem)
def handle_cart_item_deletion(sender: Any, instance: CartItem, **kwargs: Any) -> None:
    _safe_touch_cart(getattr(instance, "cart_id", None))

@receiver([post_save, post_delete], sender=Cart)
@receiver([post_save, post_delete], sender=CartItem)
def handle_cart_audit_log(sender: Any, instance: Any, created: bool = False, **kwargs: Any) -> None:
    pass

__all__ = [
    "invalidate_cart_cache_on_cart_change",
    "touch_cart_on_item_change",
    "touch_cart_on_item_create",
    "handle_cart_state_transition",
    "handle_cart_converted_event",
    "handle_cart_deletion_cleanup",
    "handle_cart_item_deletion",
    "handle_cart_audit_log",
    "cart_merged",
    "cart_reservation_changed",
]