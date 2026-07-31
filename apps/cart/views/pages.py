"""
Enterprise-grade Page Views for the Cart application.
Handles full HTML page rendering and AJAX component fragments.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError, OperationalError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView

from ..models import Cart, CartItem
from ..services import (
    CartCouponService,
    CartInventoryService,
    CartItemService,
    CartReorderService,
    CartService,
)

logger = logging.getLogger(__name__)
User = get_user_model()

_DEFAULT_MINI_CART_LIMIT: int = 5

def _safe_str(val: Any) -> str:
    return str(val).strip() if val is not None else ""

def _safe_decimal(val: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    if val is None or val == "":
        return default
    try:
        d = Decimal(str(val))
        return default if d.is_nan() or d.is_infinite() else d
    except (InvalidOperation, TypeError, ValueError):
        return default

def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return max(0, int(val))
    except (TypeError, ValueError):
        return default

def _is_ajax(request: HttpRequest) -> bool:
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("Accept", "")
    )

def _get_client_ip(request: HttpRequest) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return _safe_str(forwarded.split(",")[0])
    return _safe_str(request.META.get("REMOTE_ADDR", ""))

def _resolve_cart_or_redirect(request: HttpRequest) -> Tuple[Optional[Cart], Optional[HttpResponse]]:
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if not cart:
            return None, _error_response(request, code="cart_not_found", message=_("Cart could not be resolved."), status=500)
        return cart, None
    except Exception as exc:
        logger.exception("Cart resolution failed: %s", exc)
        return None, _error_response(request, code="cart_resolution_failed", message=_("Resolution error."), status=500)

def _error_response(
    request: HttpRequest,
    *,
    code: str = "error",
    message: str = "An error occurred.",
    status: int = 400,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> HttpResponse:
    if _is_ajax(request):
        return _api_response(request, success=False, code=code, message=message, status=status, errors=errors or [])
    try:
        messages.error(request, message)
    except Exception:
        pass
    return redirect("cart:cart_detail")

def _api_response(
    request: HttpRequest,
    success: bool,
    *,
    code: str = "",
    message: str = "",
    data: Any = None,
    errors: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
    status: int = 200,
) -> JsonResponse:
    payload: Dict[str, Any] = {
        "success": bool(success),
        "code": _safe_str(code),
        "message": _safe_str(message),
        "data": data,
        "errors": errors or [],
        "warnings": warnings or [],
        "metadata": {
            "timestamp": timezone.now().isoformat(),
            "version": "1.0",
            "request_id": uuid.uuid4().hex[:16],
        },
    }
    return JsonResponse(payload, status=status)

def _serialize_cart_item(item: CartItem, warehouse: Optional[Any] = None) -> Dict[str, Any]:
    try:
        inv = CartInventoryService.get_inventory_context(
            product=getattr(item, "product", None),
            product_variant=getattr(item, "variant", None),
            warehouse=warehouse,
        )
    except Exception:
        inv = {"exists": False, "is_out_of_stock": True, "stock_message": "Unavailable"}

    img_url = ""
    try:
        if item.product_image_snapshot:
            img_url = item.product_image_snapshot.url
        elif getattr(item.product, "primary_image", None):
            img_url = item.product.primary_image.url
    except Exception:
        img_url = ""

    return {
        "id": item.pk,
        "product_id": getattr(item, "product_id", None),
        "variant_id": getattr(item, "variant_id", None),
        "quantity": int(item.quantity or 0),
        "status": _safe_str(item.status),
        "unit_price": str(item.unit_price_snapshot or Decimal("0.00")),
        "currency": _safe_str(item.currency_snapshot) or "USD",
        "line_subtotal": str(item.line_subtotal),
        "product_name": _safe_str(item.product_name_snapshot) or getattr(item.product, "title", "Product"),
        "product_sku": _safe_str(item.product_sku_snapshot) or getattr(item.product, "sku", ""),
        "variant_name": _safe_str(item.variant_name_snapshot),
        "image_url": img_url,
        "inventory": inv,
    }

def _serialize_cart(cart: Optional[Cart]) -> Dict[str, Any]:
    if not cart or not getattr(cart, "pk", None):
        return {"id": None, "subtotal": "0.00", "grand_total": "0.00", "items": []}

    items = list(cart.items.filter(status=CartItem.ItemStatus.ACTIVE).select_related("product", "variant"))
    serialized = [_serialize_cart_item(item, warehouse=cart.preferred_warehouse) for item in items]
    totals = CartService.compute_totals(cart)

    return {
        "id": cart.pk,
        "status": _safe_str(cart.status),
        "currency": _safe_str(cart.currency) or "USD",
        "subtotal": str(totals.get("subtotal", "0.00")),
        "tax": str(totals.get("tax", "0.00")),
        "shipping": str(totals.get("shipping", "0.00")),
        "discount": str(totals.get("discount", "0.00")),
        "grand_total": str(totals.get("grand_total", "0.00")),
        "total_items": totals.get("total_items", 0),
        "unique_items": totals.get("unique_items", 0),
        "coupon_code": _safe_str(cart.coupon_code),
        "items": serialized,
    }

class BaseCartView(View):
    require_authentication: bool = False
    needs_cart: bool = True

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.request = request
        if self.require_authentication and not (request.user and request.user.is_authenticated):
            if _is_ajax(request):
                return _api_response(request, success=False, code="authentication_required", message=_("Auth required."), status=401)
            messages.error(request, _("Please sign in."))
            return redirect("/accounts/login/")

        if self.needs_cart:
            cart, error = _resolve_cart_or_redirect(request)
            if error is not None:
                return error
            self.cart = cart

        return super().dispatch(request, *args, **kwargs)

class CartDetailView(BaseCartView, TemplateView):
    template_name = "cart/cart.html"

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart_payload = _serialize_cart(self.cart)

        saved_items = list(
            self.cart.items.filter(status=CartItem.ItemStatus.SAVED).select_related("product", "variant")
        )
        saved_serialized = [_serialize_cart_item(item) for item in saved_items]

        context.update({
            "cart": self.cart,
            "cart_payload": cart_payload,
            "cart_items": cart_payload.get("items", []),
            "saved_items": saved_serialized,
            "saved_items_count": len(saved_serialized),
            "page_title": _("Shopping Cart"),
        })
        return context

class CartSummaryView(BaseCartView, TemplateView):
    template_name = "cart/partials/cart_summary.html"

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["cart"] = self.cart
        return context

class MiniCartView(BaseCartView, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        items = list(self.cart.items.filter(status=CartItem.ItemStatus.ACTIVE).select_related("product", "variant"))
        mini_items = [_serialize_cart_item(i) for i in items[:_DEFAULT_MINI_CART_LIMIT]]

        html = render_to_string(
            "cart/partials/mini_cart_content.html",
            {"cart": self.cart, "mini_cart_items": mini_items},
            request=request,
        )
        return HttpResponse(html)

class CartAddItemView(BaseCartView, View):
    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        p_id = kwargs.get("product_id") or request.POST.get("product_id")
        v_id = request.POST.get("variant_id")
        qty = _safe_int(request.POST.get("quantity", 1), default=1)

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

        if not product and not variant:
            return _error_response(request, code="product_not_found", message=_("Product not found."), status=404)

        result = CartItemService.add_item(cart=self.cart, product=product, variant=variant, quantity=qty)

        if _is_ajax(request):
            return _api_response(request, success=bool(result.get("success")), message=result.get("message", ""))

        if result.get("success"):
            messages.success(request, _("Item added to bag."))
        else:
            messages.error(request, result.get("message") or _("Could not add item."))

        return redirect("cart:cart_detail")

class CartUpdateItemView(BaseCartView, View):
    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any) -> HttpResponse:
        qty = _safe_int(request.POST.get("quantity", 1), default=1)
        result = CartItemService.update_quantity(cart=self.cart, item_id=item_id, quantity=qty)

        if _is_ajax(request):
            return _api_response(request, success=bool(result.get("success")), message=result.get("message", ""))

        return redirect("cart:cart_detail")

class CartRemoveItemView(BaseCartView, View):
    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any) -> HttpResponse:
        result = CartItemService.remove_item(cart=self.cart, item_id=item_id)
        if _is_ajax(request):
            return _api_response(request, success=bool(result.get("success")), message=result.get("message", ""))
        return redirect("cart:cart_detail")

class CartClearView(BaseCartView, View):
    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        result = CartItemService.clear_cart(cart=self.cart)
        if _is_ajax(request):
            return _api_response(request, success=bool(result.get("success")), message=result.get("message", ""))
        return redirect("cart:cart_detail")

class CartSaveForLaterView(BaseCartView, View):
    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any) -> HttpResponse:
        result = CartItemService.save_for_later(cart=self.cart, item_id=item_id)
        if _is_ajax(request):
            return _api_response(request, success=bool(result.get("success")), message=result.get("message", ""))
        return redirect("cart:cart_detail")

class CartMoveToCartView(BaseCartView, View):
    http_method_names = ["post", "options"]

    def post(self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any) -> HttpResponse:
        result = CartItemService.move_to_cart(cart=self.cart, item_id=item_id)
        if _is_ajax(request):
            return _api_response(request, success=bool(result.get("success")), message=result.get("message", ""))
        return redirect("cart:cart_detail")

__all__ = [
    "BaseCartView",
    "CartDetailView",
    "CartSummaryView",
    "MiniCartView",
    "CartAddItemView",
    "CartUpdateItemView",
    "CartRemoveItemView",
    "CartClearView",
    "CartSaveForLaterView",
    "CartMoveToCartView",
]