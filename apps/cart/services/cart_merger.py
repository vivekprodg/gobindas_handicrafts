"""
Enterprise Cart Merger Orchestration Layer.
Safely combines guest and session carts into authenticated customer carts.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Cart, CartItem

logger = logging.getLogger(__name__)

_DEFAULT_MAX_QUANTITY_PER_ITEM = 99
_DEFAULT_MAX_ITEMS_PER_CART = 200

def get_max_quantity_per_item() -> int:
    try:
        return max(1, int(getattr(settings, "CART_MERGER_MAX_QUANTITY_PER_ITEM", _DEFAULT_MAX_QUANTITY_PER_ITEM)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUANTITY_PER_ITEM

def get_default_reservation_minutes() -> int:
    try:
        return max(1, int(getattr(settings, "CART_MERGER_RESERVATION_MINUTES", 30)))
    except (TypeError, ValueError):
        return 30

def get_max_items_per_cart() -> int:
    try:
        return max(1, int(getattr(settings, "CART_MERGER_MAX_ITEMS", _DEFAULT_MAX_ITEMS_PER_CART)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ITEMS_PER_CART

def should_release_guest_reservations() -> bool:
    return bool(getattr(settings, "CART_MERGER_RELEASE_RESERVATIONS", True))

def should_validate_inventory() -> bool:
    return bool(getattr(settings, "CART_MERGER_VALIDATE_INVENTORY", True))

def should_recreate_reservation() -> bool:
    return bool(getattr(settings, "CART_MERGER_RECREATE_RESERVATION", True))

def _structured_response(
    success: bool,
    *,
    code: str = "",
    message: str = "",
    payload: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    res: Dict[str, Any] = {"success": bool(success), "code": str(code), "message": str(message)}
    if payload and isinstance(payload, dict):
        res.update(payload)
    if error is not None:
        res["error"] = str(error)
    return res

class CartError(Exception): pass
class CartNotFoundError(CartError): pass
class CartInvalidStateError(CartError): pass
class CartMergeError(CartError): pass
class CartMergeConflictError(CartMergeError): pass

class CartMergerService:
    @classmethod
    @transaction.atomic
    def merge_guest_cart_into_customer(cls, guest_cart: Optional[Cart], customer: Any) -> Optional[Cart]:
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        if not guest_cart or not getattr(guest_cart, "pk", None):
            return cls._get_or_create_customer_cart(customer)
        if guest_cart.customer_id == customer.id:
            return guest_cart

        customer_cart = (
            Cart.objects.select_for_update()
            .filter(customer_id=customer.id, is_active=True)
            .order_by("-last_activity_at")
            .first()
        )
        if customer_cart is None:
            customer_cart = Cart.objects.create(customer=customer, status=Cart.CartStatus.ACTIVE, is_active=True)

        guest_items = list(guest_cart.items.filter(status=CartItem.ItemStatus.ACTIVE).select_related("product", "variant"))

        for guest_item in guest_items:
            existing = customer_cart.items.filter(
                product_id=guest_item.product_id,
                variant_id=guest_item.variant_id,
                status=CartItem.ItemStatus.ACTIVE,
            ).first()

            if existing:
                new_qty = min(existing.quantity + guest_item.quantity, get_max_quantity_per_item())
                CartItem.objects.filter(pk=existing.pk).update(quantity=new_qty, updated_at=timezone.now())
                guest_item.delete()
            else:
                CartItem.objects.filter(pk=guest_item.pk).update(cart=customer_cart, updated_at=timezone.now())

        Cart.objects.filter(pk=guest_cart.pk).update(
            status=Cart.CartStatus.MERGED,
            is_active=False,
            last_merged_at=timezone.now(),
            updated_at=timezone.now(),
        )

        customer_cart.touch()
        return customer_cart

    @classmethod
    def merge_multiple_guest_carts(cls, *, guest_carts: List[Cart], customer: Any) -> Optional[Cart]:
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        cart = None
        for g_cart in guest_carts or []:
            if g_cart and getattr(g_cart, "pk", None) and not g_cart.customer_id:
                cart = cls.merge_guest_cart_into_customer(g_cart, customer)
        return cart or cls._get_or_create_customer_cart(customer)

    @classmethod
    def merge_session_into_customer(cls, *, session_key: str, customer: Any) -> Optional[Cart]:
        if not customer or not getattr(customer, "is_authenticated", False) or not session_key:
            return None
        guest_cart = Cart.objects.filter(session_key=session_key, customer__isnull=True, is_active=True).first()
        return cls.merge_guest_cart_into_customer(guest_cart, customer)

    @classmethod
    def merge_persistent_into_active(cls, *, persistent_cart: Cart, customer: Any) -> Optional[Cart]:
        return cls.merge_guest_cart_into_customer(persistent_cart, customer)

    @classmethod
    def merge_structured(cls, *, source_cart: Optional[Cart], customer: Any) -> Dict[str, Any]:
        if not customer or not getattr(customer, "is_authenticated", False):
            return _structured_response(False, code="invalid_customer", message="Customer not authenticated.")
        if not source_cart or not getattr(source_cart, "pk", None):
            c_cart = cls._get_or_create_customer_cart(customer)
            return _structured_response(True, code="no_source_cart", payload={"cart_id": c_cart.pk if c_cart else None})

        merged = cls.merge_guest_cart_into_customer(source_cart, customer)
        if merged:
            return _structured_response(True, code="merge_ok", message="Cart merge completed.", payload={"cart_id": merged.pk})
        return _structured_response(False, code="merge_failed", message="Merge failed.")

    @classmethod
    def get_merge_analytics(cls, cart: Cart) -> Dict[str, Any]:
        if not cart or not isinstance(cart, Cart):
            return {"last_merged_at": None, "merge_count": 0, "was_guest_cart": False}
        return {
            "last_merged_at": cart.last_merged_at,
            "merge_count": 1 if cart.last_merged_at else 0,
            "was_guest_cart": cart.is_guest,
            "status": cart.status,
            "is_active": cart.is_active,
        }

    @staticmethod
    def _get_or_create_customer_cart(customer: Any) -> Optional[Cart]:
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        cart = Cart.objects.filter(customer_id=customer.id, is_active=True).order_by("-last_activity_at").first()
        if cart:
            return cart
        return Cart.objects.create(customer=customer, status=Cart.CartStatus.ACTIVE, is_active=True)

# Module aliases
def merge_guest_cart_into_customer(guest_cart: Optional[Cart], customer: Any) -> Optional[Cart]:
    return CartMergerService.merge_guest_cart_into_customer(guest_cart, customer)

def get_merge_analytics(cart: Cart) -> Dict[str, Any]:
    return CartMergerService.get_merge_analytics(cart)

def merge_session_into_customer(*, session_key: str, customer: Any) -> Optional[Cart]:
    return CartMergerService.merge_session_into_customer(session_key=session_key, customer=customer)

def merge_multiple_guest_carts(*, guest_carts: List[Cart], customer: Any) -> Optional[Cart]:
    return CartMergerService.merge_multiple_guest_carts(guest_carts=guest_carts, customer=customer)

def merge_persistent_into_active(*, persistent_cart: Cart, customer: Any) -> Optional[Cart]:
    return CartMergerService.merge_persistent_into_active(persistent_cart=persistent_cart, customer=customer)

def merge_structured(*, source_cart: Optional[Cart], customer: Any) -> Dict[str, Any]:
    return CartMergerService.merge_structured(source_cart=source_cart, customer=customer)

__all__ = [
    "CartMergerService",
    "get_max_quantity_per_item",
    "get_default_reservation_minutes",
    "get_max_items_per_cart",
    "should_release_guest_reservations",
    "should_validate_inventory",
    "should_recreate_reservation",
    "CartError",
    "CartNotFoundError",
    "CartInvalidStateError",
    "CartMergeError",
    "CartMergeConflictError",
    "merge_guest_cart_into_customer",
    "get_merge_analytics",
    "merge_session_into_customer",
    "merge_multiple_guest_carts",
    "merge_persistent_into_active",
    "merge_structured",
]