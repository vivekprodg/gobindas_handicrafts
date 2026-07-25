"""
Enterprise-grade REST JSON API for the Cart application.
Direct sub-module service imports eliminate package __init__ collision issues.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from ..models import Cart, CartItem
from ..services.cart_calculations import CartCalculationsService
from ..services.cart_core import CartService
from ..services.cart_coupons import CartCouponService
from ..services.cart_inventory import CartInventoryService
from ..services.cart_items import CartItemService
from ..services.cart_merger import CartMergerService
from ..services.cart_reorder import CartReorderService

logger = logging.getLogger(__name__)
User = get_user_model()

def _api_response(
    success: bool,
    *,
    code: str = "",
    message: str = "",
    data: Any = None,
    errors: Optional[List[Dict[str, Any]]] = None,
    status: int = 200,
) -> JsonResponse:
    return JsonResponse(
        {
            "success": bool(success),
            "code": str(code),
            "message": str(message),
            "data": data,
            "errors": errors or [],
            "metadata": {
                "timestamp": timezone.now().isoformat(),
                "version": "1.0",
                "request_id": uuid.uuid4().hex[:16],
            },
        },
        status=status,
    )

def _parse_json(request: HttpRequest) -> Tuple[Optional[Dict[str, Any]], Optional[JsonResponse]]:
    try:
        raw = request.body.decode("utf-8") if request.body else "{}"
        return json.loads(raw), None
    except Exception as exc:
        return None, _api_response(False, code="invalid_json", message="Malformed JSON body.", status=400)

class CartAPIBaseView(View):
    allowed_methods: List[str] = ["GET"]

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.method.upper() not in [m.upper() for m in self.allowed_methods]:
            return _api_response(False, code="method_not_allowed", message="Method not allowed.", status=405)

        cart, _ = CartService.get_or_create_for_request(request)
        if not cart:
            return _api_response(False, code="cart_not_found", message="Could not resolve cart.", status=500)
        self.cart = cart
        return super().dispatch(request, *args, **kwargs)

@method_decorator(csrf_exempt, name="dispatch")
class CartSyncView(CartAPIBaseView):
    allowed_methods = ["GET", "POST"]

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        totals = CartService.compute_totals(self.cart)
        return _api_response(
            True,
            code="cart_synced",
            data={"cart_id": self.cart.pk, "totals": totals, "count": self.cart.total_items_count},
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        data, err = _parse_json(request)
        if err:
            return err

        p_id = data.get("product_id")
        v_id = data.get("variant_id")
        qty = int(data.get("quantity", 1) or 1)

        product = None
        variant = None
        try:
            from apps.catalog.models import Product, ProductVariant
            if v_id:
                variant = ProductVariant.objects.filter(pk=v_id).first()
                if variant:
                    product = variant.product
            elif p_id:
                product = Product.objects.filter(pk=p_id).first()
        except Exception:
            pass

        res = CartItemService.add_item(cart=self.cart, product=product, variant=variant, quantity=qty)
        return _api_response(bool(res.get("success")), code=res.get("code", ""), message=res.get("message", ""), data=res)

@method_decorator(csrf_exempt, name="dispatch")
class CartEstimateView(CartAPIBaseView):
    allowed_methods = ["GET"]

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        totals = CartService.compute_totals(self.cart)
        return _api_response(True, code="estimate_computed", data={"totals": totals})

@method_decorator(csrf_exempt, name="dispatch")
class CartValidateView(CartAPIBaseView):
    allowed_methods = ["GET", "POST"]

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        res = CartInventoryService.validate_for_checkout(cart=self.cart)
        return _api_response(bool(res.get("ready_for_checkout")), code="validation_complete", data=res)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        return self.get(request, *args, **kwargs)

@method_decorator(csrf_exempt, name="dispatch")
class CartApplyCouponView(CartAPIBaseView):
    allowed_methods = ["POST"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        data, err = _parse_json(request)
        if err:
            return err
        code = str(data.get("coupon_code", "")).strip()
        res = CartCouponService.apply_coupon(cart=self.cart, code=code)
        return _api_response(bool(res.get("success")), code=res.get("code", ""), message=res.get("message", ""), data=res)

@method_decorator(csrf_exempt, name="dispatch")
class CartRemoveCouponView(CartAPIBaseView):
    allowed_methods = ["POST", "DELETE"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        res = CartCouponService.remove_coupon(cart=self.cart)
        return _api_response(bool(res.get("success")), code=res.get("code", ""), message=res.get("message", ""))

    def delete(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        return self.post(request, *args, **kwargs)

@method_decorator(csrf_exempt, name="dispatch")
class CartMergeView(CartAPIBaseView):
    allowed_methods = ["POST"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        if not request.user or not request.user.is_authenticated:
            return _api_response(False, code="authentication_required", message="Auth required.", status=401)

        merged = CartMergerService.merge_guest_cart_into_customer(guest_cart=self.cart, customer=request.user)
        return _api_response(bool(merged is not None), code="merge_completed", data={"cart_id": merged.pk if merged else None})

@method_decorator(csrf_exempt, name="dispatch")
class CartReorderView(CartAPIBaseView):
    allowed_methods = ["POST"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        if not request.user or not request.user.is_authenticated:
            return _api_response(False, code="authentication_required", message="Auth required.", status=401)

        data, err = _parse_json(request)
        if err:
            return err

        res = CartReorderService.reorder_items_into_cart(cart=self.cart, items=data.get("items"), user=request.user)
        return _api_response(bool(res.get("success")), code=res.get("code", ""), message=res.get("message", ""), data=res)

# Legacy function views
@require_GET
def cart_estimate_legacy(request: HttpRequest) -> JsonResponse:
    cart, _ = CartService.get_or_create_for_request(request)
    totals = CartService.compute_totals(cart)
    return JsonResponse({"status": "success", "subtotal": str(totals["subtotal"]), "total": str(totals["grand_total"])})

@require_GET
def cart_validate_legacy(request: HttpRequest) -> JsonResponse:
    cart, _ = CartService.get_or_create_for_request(request)
    res = CartInventoryService.validate_for_checkout(cart=cart)
    return JsonResponse({"status": "ok" if res.get("ready_for_checkout") else "error", "issues": res.get("issues", [])})

@csrf_exempt
@require_POST
def cart_sync_legacy(request: HttpRequest) -> JsonResponse:
    cart, _ = CartService.get_or_create_for_request(request)
    return JsonResponse({"status": "success", "cart_id": cart.pk if cart else None})

def cart_sync_snapshot_legacy(request: HttpRequest) -> JsonResponse:
    return cart_estimate_legacy(request)

def cart_apply_coupon_legacy(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "success"})

def cart_remove_coupon_legacy(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "success"})

def cart_merge_legacy(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "success"})

def cart_reorder_legacy(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "success"})

__all__ = [
    "CartAPIBaseView",
    "CartSyncView",
    "CartEstimateView",
    "CartValidateView",
    "CartApplyCouponView",
    "CartRemoveCouponView",
    "CartMergeView",
    "CartReorderView",
    "cart_estimate_legacy",
    "cart_validate_legacy",
    "cart_sync_legacy",
    "cart_sync_snapshot_legacy",
    "cart_apply_coupon_legacy",
    "cart_remove_coupon_legacy",
    "cart_merge_legacy",
    "cart_reorder_legacy",
]