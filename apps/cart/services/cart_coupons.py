"""
apps/cart/services/cart_coupons.py

Coupon code application, validation, and removal.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from ..models import Cart

class CartCouponService:
    """Applies coupon codes to carts with validation."""

    @staticmethod
    def apply_coupon(cart: Cart, code: str, discount_amount: Decimal) -> Cart:
        """
        Applies a validated coupon code. Validates the discount is not
        greater than the cart subtotal.
        """
        if not code:
            raise ValidationError(_("A coupon code is required."))
        if discount_amount < Decimal("0.00"):
            raise ValidationError(_("Coupon discount must be non-negative."))
        if cart.subtotal <= Decimal("0.00"):
            raise ValidationError(_("Cart is empty; cannot apply a coupon."))
        if discount_amount > cart.subtotal:
            raise ValidationError(_("Coupon discount cannot exceed cart subtotal."))
        cart.apply_coupon(code=code.upper().strip(), discount_amount=discount_amount)
        return cart

    @staticmethod
    def remove_coupon(cart: Cart) -> Cart:
        cart.clear_coupon()
        return cart