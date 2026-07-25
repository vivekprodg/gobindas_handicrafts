"""
Enterprise-grade Orchestration Facade Layer for the Cart application.
Single entry point re-exporting core cart service classes and backward-compatible functions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, Optional, Tuple, Union

from .models import Cart, CartItem
from .services.cart_calculations import CartCalculationsService
from .services.cart_core import CartService
from .services.cart_coupons import CartCouponService
from .services.cart_inventory import CartInventoryService
from .services.cart_items import CartItemService
from .services.cart_merger import CartMergerService
from .services.cart_reorder import CartReorderService

# Configuration getters
def get_default_tax_rate() -> Decimal:
    from django.conf import settings
    return Decimal(str(getattr(settings, "DEFAULT_TAX_RATE", "0.13")))

def get_default_shipping_flat() -> Decimal:
    from django.conf import settings
    return Decimal(str(getattr(settings, "DEFAULT_SHIPPING_FEE", "0.00")))

def get_default_reservation_minutes() -> int:
    from .services.cart_inventory import get_default_reservation_minutes as g_drm
    return g_add_drm if (g_add_drm := g_drm()) else 30

def get_max_items_per_cart() -> int:
    from .services.cart_items import get_max_items_per_cart as g_mipc
    return g_mipc()

def get_max_quantity_per_item() -> int:
    from .services.cart_items import get_max_quantity_per_item as g_mqpi
    return g_mqpi()

def get_coupon_min_subtotal() -> Decimal:
    return Decimal("0.00")

# Domain Exceptions
class CartError(Exception): pass
class CartNotFoundError(CartError): pass
class CartItemNotFoundError(CartError): pass
class CartLimitExceededError(CartError): pass
class CartQuantityLimitExceededError(CartError): pass
class CartCouponError(CartError): pass
class CartCheckoutError(CartError): pass

# Legacy Function Aliases
def get_or_create_cart(request: Any) -> Tuple[Optional[Cart], bool]:
    return CartService.get_or_create_for_request(request)

def add_item_to_cart(
    cart: Cart,
    *,
    product: Any = None,
    variant: Any = None,
    quantity: int = 1,
    unit_price_snapshot: Optional[Decimal] = None,
    currency: str = "",
) -> Dict[str, Any]:
    return CartItemService.add_item(
        cart=cart,
        product=product,
        variant=variant,
        quantity=quantity,
        unit_price_snapshot=unit_price_snapshot,
        currency=currency,
    )

def remove_item_from_cart(cart: Cart, *, item_id: Optional[int] = None) -> Dict[str, Any]:
    return CartItemService.remove_item(cart=cart, item_id=item_id)

def update_cart_item(cart: Cart, *, item_id: Optional[int] = None, quantity: int = 1) -> Dict[str, Any]:
    return CartItemService.update_quantity(cart=cart, item_id=item_id, quantity=quantity)

def clear_cart(cart: Cart) -> Dict[str, Any]:
    return CartItemService.clear_cart(cart=cart)

def save_item_for_later(cart: Cart, *, item_id: Optional[int] = None, reason: str = "") -> Dict[str, Any]:
    return CartItemService.save_for_later(cart=cart, item_id=item_id, reason=reason)

def move_item_to_cart(cart: Cart, *, item_id: Optional[int] = None) -> Dict[str, Any]:
    return CartItemService.move_to_cart(cart=cart, item_id=item_id)

def merge_guest_cart_into_customer(guest_cart: Cart, customer: Any) -> Optional[Cart]:
    return CartService.merge_guest_cart_into_customer(guest_cart=guest_cart, customer=customer)

def apply_coupon(cart: Cart, code: str, customer: Any = None) -> Dict[str, Any]:
    res = CartCouponService.apply_coupon(cart=cart, code=code, customer=customer)
    if isinstance(res, dict):
        return res
    return {"success": True, "coupon_code": code}

def remove_coupon(cart: Cart) -> Dict[str, Any]:
    return CartCouponService.remove_coupon(cart=cart)

def validate_cart_for_checkout(cart: Cart) -> Dict[str, Any]:
    return CartInventoryService.validate_for_checkout(cart=cart)

def reorder_items_into_cart(
    cart: Optional[Cart] = None,
    items: Optional[Iterable[Dict[str, Any]]] = None,
    order: Optional[Any] = None,
    order_reference: str = "",
    user: Optional[Any] = None,
) -> Dict[str, Any]:
    res = CartReorderService.reorder_items_into_cart(
        cart=cart, order=order, items=items, user=user, order_reference=order_reference
    )
    if isinstance(res, dict):
        return res
    return {"success": True, "message": "Reordered"}

def process_return_request(
    cart: Optional[Cart] = None,
    items: Optional[Iterable[Dict[str, Any]]] = None,
    order_reference: str = "",
) -> Dict[str, Any]:
    return reorder_items_into_cart(cart=cart, items=items, order_reference=order_reference)

__all__ = [
    "CartService",
    "CartItemService",
    "CartCalculationsService",
    "CartInventoryService",
    "CartCouponService",
    "CartMergerService",
    "CartReorderService",
    "get_default_tax_rate",
    "get_default_shipping_flat",
    "get_default_reservation_minutes",
    "get_max_items_per_cart",
    "get_max_quantity_per_item",
    "get_coupon_min_subtotal",
    "CartError",
    "CartNotFoundError",
    "CartItemNotFoundError",
    "CartLimitExceededError",
    "CartQuantityLimitExceededError",
    "CartCouponError",
    "CartCheckoutError",
    "get_or_create_cart",
    "add_item_to_cart",
    "remove_item_from_cart",
    "update_cart_item",
    "clear_cart",
    "save_item_for_later",
    "move_item_to_cart",
    "merge_guest_cart_into_customer",
    "apply_coupon",
    "remove_coupon",
    "validate_cart_for_checkout",
    "reorder_items_into_cart",
    "process_return_request",
]