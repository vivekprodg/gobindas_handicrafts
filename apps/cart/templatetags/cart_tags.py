"""
Presentation template tags and filters for the Cart application.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from django import template
from django.conf import settings

from ..models import Cart, CartItem
from ..services import CartCalculationsService, CartService

register = template.Library()

def _format_decimal(val: Any) -> str:
    try:
        d = Decimal(str(val))
        return f"{d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"
    except Exception:
        return "0.00"

@register.filter(name="cart_decimal")
def cart_decimal_filter(value: Any) -> str:
    return _format_decimal(value)

@register.filter(name="cart_currency")
def cart_currency_filter(value: Any) -> str:
    curr = getattr(settings, "CART_DEFAULT_CURRENCY", "USD")
    return f"{curr} {_format_decimal(value)}"

@register.filter(name="multiply")
def multiply_filter(value: Any, arg: Any) -> str:
    try:
        res = Decimal(str(value)) * Decimal(str(arg))
        return _format_decimal(res)
    except Exception:
        return "0.00"

@register.simple_tag(takes_context=True)
def cart_count(context: Dict[str, Any]) -> int:
    request = context.get("request")
    if not request:
        return 0
    cart, _ = CartService.get_or_create_for_request(request)
    return cart.total_items_count if cart else 0

@register.simple_tag(takes_context=True)
def cart_subtotal(context: Dict[str, Any]) -> str:
    request = context.get("request")
    if not request:
        return "0.00"
    cart, _ = CartService.get_or_create_for_request(request)
    return _format_decimal(cart.subtotal) if cart else "0.00"

@register.simple_tag(takes_context=True)
def cart_total(context: Dict[str, Any]) -> str:
    request = context.get("request")
    if not request:
        return "0.00"
    cart, _ = CartService.get_or_create_for_request(request)
    return _format_decimal(cart.grand_total) if cart else "0.00"

@register.simple_tag
def line_subtotal(item: Any) -> str:
    if isinstance(item, CartItem):
        return _format_decimal(item.line_subtotal)
    return "0.00"

@register.inclusion_tag("cart/partials/cart_summary.html", takes_context=True, name="cart_summary")
def cart_summary(context: Dict[str, Any]) -> Dict[str, Any]:
    request = context.get("request")
    cart, _ = CartService.get_or_create_for_request(request) if request else (None, False)

    if not cart:
        return {
            "cart": None,
            "cart_items": [],
            "subtotal": Decimal("0.00"),
            "discount": Decimal("0.00"),
            "tax": Decimal("0.00"),
            "shipping": Decimal("0.00"),
            "grand_total": Decimal("0.00"),
            "currency": "USD",
            "item_count": 0,
            "is_empty": True,
        }

    summary = CartCalculationsService.get_cart_summary(cart)
    active_items = cart.items.filter(status=CartItem.ItemStatus.ACTIVE).select_related("product", "variant")

    return {
        "cart": cart,
        "cart_items": active_items,
        "subtotal": summary["subtotal"],
        "discount": summary["discount"],
        "tax": summary["tax"],
        "shipping": summary["shipping"],
        "grand_total": summary["grand_total"],
        "currency": summary["currency"],
        "item_count": summary["item_count"],
        "coupon_code": cart.coupon_code,
        "is_empty": summary["item_count"] == 0,
    }

__all__ = [
    "cart_decimal_filter",
    "cart_currency_filter",
    "multiply_filter",
    "cart_count",
    "cart_subtotal",
    "cart_total",
    "line_subtotal",
    "cart_summary",
]