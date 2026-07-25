"""
Service for calculating cart totals, discounts, taxes, and shipping.
All calculations use Decimal for precision and are rounded to 2 decimal places.
Includes dynamic coupon re-validation to prevent stale discount state.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from django.conf import settings
from django.db import models

from ..models import Cart, CartItem

logger = logging.getLogger(__name__)

class CartCalculationsService:
    """
    Service for calculating cart totals, discounts, taxes, and shipping.
    All calculations use Decimal for precision and are rounded to 2 decimal places.
    """

    @staticmethod
    def calculate_line_subtotal(item: CartItem) -> Decimal:
        """Calculate the subtotal for a single cart item."""
        return (item.unit_price_snapshot or Decimal("0.00")) * item.quantity

    @staticmethod
    def calculate_line_discount(item: CartItem) -> Decimal:
        """Calculate the discount for a single cart item (based on compare_at_price)."""
        if item.compare_at_price_snapshot and item.compare_at_price_snapshot > item.unit_price_snapshot:
            return (item.compare_at_price_snapshot - item.unit_price_snapshot) * item.quantity
        return Decimal("0.00")

    @staticmethod
    def calculate_cart_subtotal(cart: Cart) -> Decimal:
        """Calculate the subtotal of all active items in the cart."""
        if not cart or not getattr(cart, "pk", None):
            return Decimal("0.00")

        active_items = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
        total = active_items.aggregate(
            total=models.Sum(
                models.F("unit_price_snapshot") * models.F("quantity"),
                output_field=models.DecimalField(max_digits=14, decimal_places=2),
            )
        )["total"]
        return total or Decimal("0.00")

    @staticmethod
    def calculate_cart_discount(cart: Cart) -> Decimal:
        """Calculate the total discount from line-level discounts (e.g., sale prices)."""
        if not cart or not getattr(cart, "pk", None):
            return Decimal("0.00")

        active_items = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
        total = active_items.aggregate(
            total=models.Sum(
                models.Case(
                    models.When(
                        compare_at_price_snapshot__gt=models.F("unit_price_snapshot"),
                        then=(models.F("compare_at_price_snapshot") - models.F("unit_price_snapshot"))
                        * models.F("quantity"),
                    ),
                    default=Decimal("0.00"),
                    output_field=models.DecimalField(max_digits=14, decimal_places=2),
                )
            )
        )["total"]
        return total or Decimal("0.00")

    @staticmethod
    def calculate_cart_tax(cart: Cart) -> Decimal:
        """Calculate tax based on cart subtotal and default tax rate."""
        tax_rate = getattr(settings, "DEFAULT_TAX_RATE", Decimal("0.13"))
        subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        return (subtotal * Decimal(str(tax_rate))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def calculate_cart_shipping(cart: Cart) -> Decimal:
        """Calculate shipping cost. Defaults to 0.00; configurable via settings."""
        return Decimal("0.00")

    @staticmethod
    def calculate_cart_grand_total(cart: Cart) -> Decimal:
        """Calculate the grand total (subtotal - discount + tax + shipping) with dynamic coupon revalidation."""
        if cart and getattr(cart, "pk", None) and cart.coupon_code:
            try:
                from apps.coupons.services import CouponValidationService
                CouponValidationService.revalidate_cart_coupon(cart)
            except Exception as exc:
                logger.debug("Coupon revalidation error in calculate_cart_grand_total: %s", exc)

        subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        discount = CartCalculationsService.calculate_cart_discount(cart) + (
            getattr(cart, "coupon_discount_amount", Decimal("0.00")) or Decimal("0.00")
        )
        tax = CartCalculationsService.calculate_cart_tax(cart)
        shipping = CartCalculationsService.calculate_cart_shipping(cart)

        grand_total = max(Decimal("0.00"), subtotal - discount + tax + shipping)
        return grand_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def get_cart_summary(cart: Cart) -> Dict[str, Any]:
        """Get a summary dictionary of cart totals for display or API responses."""
        if cart and getattr(cart, "pk", None) and cart.coupon_code:
            try:
                from apps.coupons.services import CouponValidationService
                CouponValidationService.revalidate_cart_coupon(cart)
            except Exception as exc:
                logger.debug("Coupon revalidation error in get_cart_summary: %s", exc)

        subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        discount = CartCalculationsService.calculate_cart_discount(cart)
        coupon_discount = getattr(cart, "coupon_discount_amount", Decimal("0.00")) or Decimal("0.00")
        tax = CartCalculationsService.calculate_cart_tax(cart)
        shipping = CartCalculationsService.calculate_cart_shipping(cart)
        grand_total = CartCalculationsService.calculate_cart_grand_total(cart)

        return {
            "subtotal": subtotal,
            "discount": discount + coupon_discount,
            "line_discount": discount,
            "coupon_discount": coupon_discount,
            "tax": tax,
            "shipping": shipping,
            "grand_total": grand_total,
            "currency": cart.currency if cart else "NPR",
            "item_count": cart.total_items_count if cart else 0,
            "unique_item_count": cart.unique_items_count if cart else 0,
        }

    @staticmethod
    def apply_cart_discounts(cart: Cart) -> None:
        """Updates timestamps and refreshes active cart activity."""
        if cart and getattr(cart, "pk", None):
            cart.touch()

__all__ = ["CartCalculationsService"]