"""
Global context processor exposing promotional coupons, badges, and CMS settings to templates.
"""
from __future__ import annotations

from typing import Any, Dict
from django.http import HttpRequest

from .selectors import get_coupon_by_code, get_coupon_cms_settings, get_public_active_coupons
from .serializers import CouponCMSSettingSerializer, CouponSerializer

def coupons_context(request: HttpRequest) -> Dict[str, Any]:
    """
    Injects public available coupons, CMS promotion bar settings, and current applied coupon into every template.
    """
    try:
        cms_settings = get_coupon_cms_settings()
        if not cms_settings.enable_coupon_system:
            return {
                "coupon_cms_settings": None,
                "public_coupons": [],
                "applied_coupon": None,
            }

        public_coupons = get_public_active_coupons()
        
        # Resolve applied coupon details if active in request cart
        applied_coupon = None
        cart = getattr(request, "_cached_cart", None)
        if not cart and hasattr(request, "session"):
            from apps.cart.services.cart_core import CartService
            cart = CartService.get_active_cart(request)
            request._cached_cart = cart

        if cart and cart.coupon_code:
            applied_coupon = get_coupon_by_code(cart.coupon_code)

        return {
            "coupon_cms_settings": cms_settings,
            "public_coupons": public_coupons,
            "applied_coupon": applied_coupon,
        }
    except Exception:
        return {
            "coupon_cms_settings": None,
            "public_coupons": [],
            "applied_coupon": None,
        }