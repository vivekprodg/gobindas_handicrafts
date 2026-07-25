"""
Views for applying, removing, validating, and listing promotional coupons.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.cart.services.cart_core import CartService
from . import constants as c
from .forms import ApplyCouponForm
from .selectors import get_coupon_by_code, get_coupon_cms_settings, get_public_active_coupons
from .serializers import CouponCMSSettingSerializer, CouponSerializer
from .services import CouponApplicationService, CouponValidationService

logger = logging.getLogger(c.LOGGER_NAME)

def _is_ajax_request(request: HttpRequest) -> bool:
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )

class ApplyCouponView(View):
    """
    Applies a coupon code to the user's active shopping cart.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        cart, _ = CartService.get_or_create_for_request(request)
        if not cart:
            if _is_ajax_request(request):
                return JsonResponse({"success": False, "message": str(_("Cart not found."))}, status=400)
            messages.error(request, _("Cart not found."))
            return redirect("cart:cart_detail")

        # Extract code from POST body or JSON payload
        code = request.POST.get("coupon_code", "").strip()
        if not code and request.body:
            try:
                payload = json.loads(request.body.decode("utf-8"))
                code = str(payload.get("coupon_code", "")).strip()
            except Exception:
                pass

        if not code:
            if _is_ajax_request(request):
                return JsonResponse({"success": False, "message": str(_("A coupon code is required."))}, status=400)
            messages.error(request, _("A coupon code is required."))
            return redirect("cart:cart_detail")

        result = CouponApplicationService.apply_coupon_to_cart(
            cart=cart,
            code=code,
            customer=request.user if request.user.is_authenticated else None,
        )

        if _is_ajax_request(request):
            status_code = 200 if result.get("success") else 400
            return JsonResponse(result, status=status_code)

        if result.get("success"):
            messages.success(request, result.get("message"))
        else:
            messages.error(request, result.get("message"))

        return redirect("cart:cart_detail")

class RemoveCouponView(View):
    """
    Clears the currently applied coupon code from the active shopping cart.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        cart, _ = CartService.get_or_create_for_request(request)
        if not cart:
            if _is_ajax_request(request):
                return JsonResponse({"success": False, "message": str(_("Cart not found."))}, status=400)
            messages.error(request, _("Cart not found."))
            return redirect("cart:cart_detail")

        result = CouponApplicationService.remove_coupon_from_cart(cart)

        if _is_ajax_request(request):
            return JsonResponse(result, status=200)

        messages.success(request, result.get("message"))
        return redirect("cart:cart_detail")

class PublicCouponsListView(View):
    """
    Returns available public coupons and promotional CMS settings in JSON format.
    """

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        cms_settings = get_coupon_cms_settings()
        if not cms_settings.enable_coupon_system:
            return JsonResponse({
                "success": False,
                "message": str(_("Coupon system is disabled.")),
                "coupons": [],
            })

        coupons = get_public_active_coupons()
        serialized_coupons = CouponSerializer.serialize_many(coupons)
        serialized_settings = CouponCMSSettingSerializer.serialize(cms_settings)

        return JsonResponse({
            "success": True,
            "cms_settings": serialized_settings,
            "coupons": serialized_coupons,
            "count": len(serialized_coupons),
        })

@method_decorator(csrf_exempt, name="dispatch")
class ValidateCouponAPIView(View):
    """
    REST API endpoint for real-time frontend validation of coupon codes without mutating cart state.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        cart, _ = CartService.get_or_create_for_request(request)
        if not cart:
            return JsonResponse({"valid": False, "message": str(_("Cart not found."))}, status=400)

        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        except Exception:
            payload = {}

        code = str(payload.get("coupon_code", "") or request.POST.get("coupon_code", "")).strip()
        if not code:
            return JsonResponse({"valid": False, "message": str(_("Coupon code required."))}, status=400)

        coupon = get_coupon_by_code(code)
        if not coupon:
            return JsonResponse({"valid": False, "message": str(_("Invalid coupon code."))}, status=404)

        try:
            discount, message = CouponValidationService.validate_and_calculate_discount(
                coupon=coupon,
                cart=cart,
                customer=request.user if request.user.is_authenticated else None,
            )
            return JsonResponse({
                "valid": True,
                "message": message,
                "code": coupon.code,
                "discount_amount": str(discount),
                "discount_type": coupon.discount_type,
            })
        except Exception as exc:
            return JsonResponse({
                "valid": False,
                "message": getattr(exc, "message", str(exc)),
            }, status=400)