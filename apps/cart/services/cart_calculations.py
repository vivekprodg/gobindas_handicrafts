from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional

from django.conf import settings
from django.db import models

from ..models import Cart, CartItem

class CartCalculationsService:
    """
    Service for calculating cart totals, discounts, taxes, and shipping.
    All calculations use Decimal for precision and are rounded to 2 decimal places.
    """

    @staticmethod
    def calculate_line_subtotal(item: CartItem) -> Decimal:
        """Calculate the subtotal for a single cart item."""
        return (item.unit_price_snapshot or Decimal("0")) * item.quantity

    @staticmethod
    def calculate_line_discount(item: CartItem) -> Decimal:
        """Calculate the discount for a single cart item (based on compare_at_price)."""
        if item.compare_at_price_snapshot and item.compare_at_price_snapshot > item.unit_price_snapshot:
            return (item.compare_at_price_snapshot - item.unit_price_snapshot) * item.quantity
        return Decimal("0")

    @staticmethod
    def calculate_cart_subtotal(cart: Cart) -> Decimal:
        """Calculate the subtotal of all active items in the cart."""
        active_items = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
        total = active_items.aggregate(
            total=models.Sum(
                models.F("unit_price_snapshot") * models.F("quantity"),
                output_field=models.DecimalField(max_digits=14, decimal_places=2),
            )
        )["total"]
        return total or Decimal("0")

    @staticmethod
    def calculate_cart_discount(cart: Cart) -> Decimal:
        """Calculate the total discount from line-level discounts (e.g., sale prices)."""
        active_items = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
        total = active_items.aggregate(
            total=models.Sum(
                models.Case(
                    models.When(
                        compare_at_price_snapshot__gt=models.F("unit_price_snapshot"),
                        then=(models.F("compare_at_price_snapshot") - models.F("unit_price_snapshot"))
                        * models.F("quantity"),
                    ),
                    default=Decimal("0"),
                    output_field=models.DecimalField(max_digits=14, decimal_places=2),
                )
            )
        )["total"]
        return total or Decimal("0")

    @staticmethod
    def calculate_cart_tax(cart: Cart) -> Decimal:
        """Calculate tax based on cart subtotal and default tax rate."""
        tax_rate = getattr(settings, "DEFAULT_TAX_RATE", Decimal("0.13"))  # 13% default (Nepal VAT)
        subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        return (subtotal * tax_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def calculate_cart_shipping(cart: Cart) -> Decimal:
        """
        Calculate shipping cost. Defaults to free shipping; override in settings or checkout.
        This is a stub; actual shipping logic should be implemented in the checkout flow.
        """
        # Example: Free shipping over $50, otherwise $5
        # free_shipping_threshold = getattr(settings, "FREE_SHIPPING_THRESHOLD", Decimal("50"))
        # shipping_cost = getattr(settings, "STANDARD_SHIPPING_COST", Decimal("5"))
        # subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        # return Decimal("0") if subtotal >= free_shipping_threshold else shipping_cost
        return Decimal("0.00")

    @staticmethod
    def calculate_cart_grand_total(cart: Cart) -> Decimal:
        """Calculate the grand total (subtotal - discount + tax + shipping)."""
        subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        discount = CartCalculationsService.calculate_cart_discount(cart)
        tax = CartCalculationsService.calculate_cart_tax(cart)
        shipping = CartCalculationsService.calculate_cart_shipping(cart)
        return max(
            Decimal("0"),
            subtotal - discount + tax + shipping
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def get_cart_summary(cart: Cart) -> Dict[str, Any]:
        """
        Get a summary of cart totals for display or API responses.
        """
        subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        discount = CartCalculationsService.calculate_cart_discount(cart)
        tax = CartCalculationsService.calculate_cart_tax(cart)
        shipping = CartCalculationsService.calculate_cart_shipping(cart)
        grand_total = CartCalculationsService.calculate_cart_grand_total(cart)
        return {
            "subtotal": subtotal,
            "discount": discount,
            "tax": tax,
            "shipping": shipping,
            "grand_total": grand_total,
            "currency": cart.currency,
            "item_count": cart.total_items_count,
            "unique_item_count": cart.unique_items_count,
        }

    @staticmethod
    def apply_cart_discounts(cart: Cart) -> None:
        """
        Apply calculated discounts to the cart model fields.
        This should be called after cart modifications to update stored totals.
        """
        cart.subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        cart.total_discount = (
            CartCalculationsService.calculate_cart_discount(cart) + cart.coupon_discount_amount
        )
        cart.save(update_fields=["subtotal", "total_discount", "updated_at"])