"""
Coupon code application, validation, and removal for the Cart application.
Delegates domain validation and calculations to the Coupons application service layer.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional, Union

from django.utils.translation import gettext_lazy as _

from ..models import Cart

logger = logging.getLogger(__name__)

class CartCouponService:
    """
    Applies and removes coupon codes on active cart instances.
    Delegates validation and ledger processing to apps.coupons.services.
    """

    @staticmethod
    def apply_coupon(
        cart: Cart,
        code: str,
        discount_amount: Decimal = Decimal("0.00"),
        customer: Any = None,
    ) -> Dict[str, Any]:
        """
        Validates and applies a promotional coupon code to the specified cart.
        """
        if not cart or not getattr(cart, "pk", None):
            return {
                "success": False,
                "code": "cart_not_found",
                "message": str(_("Cart not found.")),
                "error": "Cart not found.",
            }

        clean_code = str(code or "").strip().upper()
        if not clean_code:
            return {
                "success": False,
                "code": "missing_coupon_code",
                "message": str(_("A coupon code is required.")),
                "error": "A coupon code is required.",
            }

        if cart.subtotal <= Decimal("0.00"):
            return {
                "success": False,
                "code": "cart_empty",
                "message": str(_("Cart is empty; cannot apply a coupon.")),
                "error": "Cart is empty.",
            }

        try:
            from apps.coupons.services import CouponApplicationService
            user = customer if (customer and getattr(customer, "is_authenticated", False)) else cart.customer
            return CouponApplicationService.apply_coupon_to_cart(
                cart=cart,
                code=clean_code,
                customer=user,
            )
        except Exception as exc:
            logger.exception("Failed to apply coupon '%s' to cart #%s: %s", clean_code, cart.pk, exc)
            return {
                "success": False,
                "code": "coupon_application_failed",
                "message": str(_("Unable to process coupon at this time.")),
                "error": str(exc),
            }

    @staticmethod
    def remove_coupon(cart: Cart) -> Dict[str, Any]:
        """
        Clears the currently applied coupon code and resets discount to zero on the cart.
        """
        if not cart or not getattr(cart, "pk", None):
            return {
                "success": False,
                "code": "cart_not_found",
                "message": str(_("Cart not found.")),
            }

        try:
            from apps.coupons.services import CouponApplicationService
            return CouponApplicationService.remove_coupon_from_cart(cart)
        except Exception as exc:
            logger.exception("Failed to remove coupon from cart #%s: %s", cart.pk, exc)
            cart.clear_coupon()
            return {
                "success": True,
                "code": "coupon_removed",
                "message": str(_("Coupon removed from cart.")),
                "cart_id": cart.pk,
            }

__all__ = ["CartCouponService"]