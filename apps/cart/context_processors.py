"""
Enterprise-grade context processor for the Cart application.
Exposes global cart parameters, counts, and URL endpoints safely to every Django template.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse

from .models import Cart, CartItem
from .services import CartInventoryService, CartService

logger = logging.getLogger(__name__)

_DEFAULT_CURRENCY = "NPR"
_MINI_CART_LIMIT = 5

def _safe_reverse(url_name: str, **kwargs: Any) -> str:
    try:
        return reverse(url_name, kwargs=kwargs or None)
    except (NoReverseMatch, Exception):
        return "#"

def _safe_str(val: Any) -> str:
    return str(val).strip() if val is not None else ""

def _safe_int(val: Any) -> int:
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return 0

def _serialize_item(item: CartItem) -> Dict[str, Any]:
    if not item or not getattr(item, "pk", None):
        return {}

    image_url = ""
    try:
        if item.product_image_snapshot:
            image_url = item.product_image_snapshot.url
        elif getattr(item.product, "primary_image", None):
            image_url = item.product.primary_image.url
    except Exception:
        image_url = ""

    product_slug = getattr(item.product, "slug", "")
    item_url = f"/catalog/products/{product_slug}/" if product_slug else "#"

    return {
        "id": item.pk,
        "product_id": item.product_id,
        "variant_id": item.variant_id,
        "quantity": item.quantity,
        "product_name": item.product_name_snapshot or getattr(item.product, "title", "Product"),
        "variant_name": item.variant_name_snapshot or "",
        "unit_price": str(item.unit_price_snapshot or Decimal("0.00")),
        "line_subtotal": str(item.line_subtotal),
        "currency": item.currency_snapshot or "NPR",
        "image_url": image_url,
        "product_url": item_url,
    }

def cart(request: HttpRequest) -> Dict[str, Any]:
    """
    Exposes complete cart context variables to every rendered Django template safely.
    """
    cart_obj, _ = CartService.get_or_create_for_request(request)

    if not cart_obj:
        return {
            "cart": None,
            "cart_id": None,
            "cart_count": 0,
            "cart_unique_items": 0,
            "cart_subtotal": "0.00",
            "cart_tax": "0.00",
            "cart_shipping": "0.00",
            "cart_discount": "0.00",
            "cart_grand_total": "0.00",
            "cart_currency": _DEFAULT_CURRENCY,
            "cart_is_guest": True,
            "mini_cart_items": [],
            "cart_ready_for_checkout": True,
            "cart_checkout_blocked": False,
            "is_empty": True,
            "user_authenticated": bool(request.user and request.user.is_authenticated),
            "cart_url": _safe_reverse("cart:cart_detail"),
            "mini_cart_url": _safe_reverse("cart:mini_cart"),
            "cart_clear_url": _safe_reverse("cart:cart_clear"),
            "cart_apply_coupon_url": _safe_reverse("cart:cart_apply_coupon"),
            "cart_remove_coupon_url": _safe_reverse("cart:cart_remove_coupon"),
        }

    active_items = list(
        cart_obj.items.filter(status=CartItem.ItemStatus.ACTIVE)
        .select_related("product", "variant", "reservation")
        .order_by("added_at")
    )

    mini_cart_items = [_serialize_item(item) for item in active_items[:_MINI_CART_LIMIT]]

    totals = CartService.compute_totals(cart_obj)
    count = totals.get("total_items", 0)

    try:
        check = CartInventoryService.validate_for_checkout(cart=cart_obj)
        ready = bool(check.get("ready_for_checkout", True))
    except Exception:
        ready = True

    return {
        "cart": cart_obj,
        "cart_id": cart_obj.pk,
        "cart_count": count,
        "cart_total_items": count,
        "cart_unique_items": totals.get("unique_items", 0),
        "cart_subtotal": str(totals.get("subtotal", "0.00")),
        "cart_tax": str(totals.get("tax", "0.00")),
        "cart_shipping": str(totals.get("shipping", "0.00")),
        "cart_discount": str(totals.get("discount", "0.00")),
        "cart_grand_total": str(totals.get("grand_total", "0.00")),
        "cart_currency": cart_obj.currency or _DEFAULT_CURRENCY,
        "cart_is_guest": cart_obj.is_guest,
        "cart_status": cart_obj.status,
        "cart_coupon_code": cart_obj.coupon_code,
        "cart_coupon_discount": str(cart_obj.coupon_discount_amount or "0.00"),
        "mini_cart_items": mini_cart_items,
        "cart_ready_for_checkout": ready,
        "cart_checkout_blocked": not ready,
        "cart_checkout_allowed": ready,
        "is_empty": count == 0,
        "user_authenticated": bool(request.user and request.user.is_authenticated),
        "cart_url": _safe_reverse("cart:cart_detail"),
        "mini_cart_url": _safe_reverse("cart:mini_cart"),
        "cart_clear_url": _safe_reverse("cart:cart_clear"),
        "cart_apply_coupon_url": _safe_reverse("cart:cart_apply_coupon"),
        "cart_remove_coupon_url": _safe_reverse("cart:cart_remove_coupon"),
        "cart_sync_url": _safe_reverse("cart:cart_sync"),
        "cart_estimate_url": _safe_reverse("cart:cart_estimate"),
        "cart_validate_url": _safe_reverse("cart:cart_validate"),
        "cart_merge_url": _safe_reverse("cart:cart_merge"),
        "cart_reorder_url": _safe_reverse("cart:cart_reorder"),
    }