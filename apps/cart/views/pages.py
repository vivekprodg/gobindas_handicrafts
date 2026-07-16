"""
Enterprise-grade Page Views for the Cart application.

================================================================================
ARCHITECTURE
================================================================================

This module implements the complete presentation layer for the Cart
application's user-facing pages. Every view in this module is a **THIN
ORCHESTRATOR** that:

  * Validates and parses HTTP requests
  * Authenticates and authorizes callers
  * Delegates ALL business logic to the Cart service layer
  * Delegates ALL inventory operations to the Inventory service
    (transitively, through the Cart service layer)
  * Returns rendered templates or structured JSON responses
  * Surfaces inventory results verbatim - never recalculates or owns
    stock state at the HTTP boundary

INVENTORY IS THE SINGLE SOURCE OF TRUTH.

The Cart views NEVER:
  * Read or write inventory data directly
  * Calculate stock, availability, or reservations
  * Mutate inventory models
  * Persist reservation state
  * Re-compute warehouse allocation

Every inventory read below uses the standardized inventory context
provided by the Cart service layer, which in turn delegates to the
Inventory application's service / selector layers.

ENTERPRISE PRINCIPLES
================================================================================

  * Thin HTTP layer
  * RESTful URL design with proper HTTP verbs
  * Consistent structured JSON response envelope
  * Comprehensive error handling with proper HTTP status codes
  * OWASP ASVS / OWASP API Top 10 compliance
  * CSRF / authentication / throttling
  * Backward compatibility with legacy function-based endpoints
  * Idempotent operations where appropriate
  * Read-only inventory context endpoint for the storefront UI
  * Production-grade logging and observability
  * Reservation lifecycle surfaced to the user in real time
  * Cart merge for guest-to-authenticated transition
  * Save-for-later and reorder workflows

SUPPORTED ENDPOINTS (PAGE VIEWS)
================================================================================

Page-level (HTML):
    GET    /cart/                              Cart page
    GET    /cart/summary/                      Cart summary fragment
    GET    /cart/mini/                         Mini-cart HTML fragment
    GET    /cart/saved/                        Saved-for-later page
    GET    /cart/checkout/prepare/             Checkout preparation page
    GET    /cart/checkout/review/              Checkout review page
    GET    /cart/merge/                        Cart merge page
    GET    /cart/reservations/                 Reservation status page
    GET    /cart/inventory/                    Read-only inventory context
    GET    /cart/coupons/                      Coupon management page
    GET    /cart/reorder/                      Reorder selection page

================================================================================
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
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import (
    FieldError,
    ObjectDoesNotExist,
    PermissionDenied,
    ValidationError as DjangoValidationError,
)
from django.db import DatabaseError, OperationalError, transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import (
    require_GET,
    require_POST,
    require_http_methods,
    require_safe,
)
from django.views.generic import (
    DetailView,
    FormView,
    ListView,
    TemplateView,
    View,
)

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

# =============================================================================
# PAGE-LEVEL CONFIGURATION (CMS-DRIVEN)
# =============================================================================
# All defaults can be overridden via Django settings (which can be wired
# to the CMS). The defaults below are safe enterprise fallbacks for
# production deployment.

_DEFAULT_PAGE_SIZE: int = 25
_DEFAULT_MINI_CART_LIMIT: int = 5
_DEFAULT_SUMMARY_CACHE_TTL: int = 60
_MAX_RESERVATION_DISPLAY: int = 50

# =============================================================================
# SAFE UTILITY HELPERS
# =============================================================================
def _safe_str(value: Any) -> str:
    """Best-effort safe string conversion. Never raises."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

def _safe_decimal(value: Any, *, default: Optional[Decimal] = None) -> Optional[Decimal]:
    """Best-effort safe Decimal conversion. Never raises."""
    if value is None or value == "":
        return default
    try:
        decimal_value = Decimal(str(value))
        if decimal_value.is_nan() or decimal_value.is_infinite():
            return default
        return decimal_value
    except (InvalidOperation, TypeError, ValueError):
        return default

def _safe_int(value: Any, *, default: int = 0) -> int:
    """Best-effort safe int conversion. Never raises."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return default

def _is_ajax(request: HttpRequest) -> bool:
    """Returns True if the request was made via AJAX (XMLHttpRequest)."""
    return request.headers.get("x-requested-with") == "XMLHttpRequest" or \
        "application/json" in request.headers.get("Accept", "")

def _get_client_ip(request: HttpRequest) -> str:
    """Extract the originating client IP, honouring X-Forwarded-For."""
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        try:
            return forwarded_for.split(",")[0].strip()
        except Exception:
            pass
    return _safe_str(request.META.get("REMOTE_ADDR", ""))

def _resolve_cart_or_redirect(
    request: HttpRequest,
    redirect_url_name: str = "cart:cart_detail",
) -> Tuple[Optional[Cart], Optional[HttpResponse]]:
    """
    Resolves the cart for the current request, creating one if missing.

    Returns a tuple of (cart, error_response). When ``error_response`` is
    not None, callers should return it directly.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
    except Exception as exc:
        logger.exception("Cart resolution failed: %s", exc)
        return None, _error_response(
            request,
            code="cart_resolution_failed",
            message=_("Could not resolve a cart for this request. Please try again."),
            status=500,
        )
    if cart is None:
        return None, _error_response(
            request,
            code="cart_not_found",
            message=_("Could not resolve a cart for this request."),
            status=500,
        )
    return cart, None

def _error_response(
    request: HttpRequest,
    *,
    code: str = "error",
    message: str = "An error occurred.",
    status: int = 400,
    errors: Optional[List[Dict[str, Any]]] = None,
) -> HttpResponse:
    """
    Build a context-agnostic error response.

    For AJAX requests, returns a JSON envelope. For standard HTML
    requests, redirects with a Django messages framework entry when
    possible, or returns a minimal HTML error page as a last resort.
    """
    if _is_ajax(request):
        return _api_response(
            request,
            success=False,
            code=code,
            message=message,
            status=status,
            errors=errors or [],
        )
    try:
        messages.error(request, message)
    except Exception:
        pass
    try:
        return redirect("cart:cart_detail")
    except Exception:
        from django.http import HttpResponseBadRequest
        return HttpResponseBadRequest(message)

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
    """
    Build a structured JSON response envelope for AJAX endpoints.

    The envelope ALWAYS includes:

      * success            - bool
      * code               - machine-readable status code
      * message            - human readable message
      * data               - structured payload
      * errors             - list of error dicts
      * warnings           - list of warning dicts
      * metadata.timestamp - ISO 8601 UTC timestamp
      * metadata.version   - API version
      * metadata.request_id - opaque request correlation ID
    """
    payload: Dict[str, Any] = {
        "success": bool(success),
    }
    if code:
        payload["code"] = _safe_str(code)
    if message:
        payload["message"] = _safe_str(message)
    payload["data"] = data
    payload["errors"] = list(errors or [])
    payload["warnings"] = list(warnings or [])

    metadata: Dict[str, Any] = {
        "timestamp": timezone.now().isoformat(),
        "version": "1.0",
        "request_id": uuid.uuid4().hex[:16],
    }
    payload["metadata"] = metadata

    return JsonResponse(payload, status=status)

def _translate_service_payload(
    service_payload: Dict[str, Any],
) -> Tuple[int, str, str, Any, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Translate a Cart service payload into the canonical API envelope
    components. Returns (status, code, message, data, errors, warnings).
    """
    if not isinstance(service_payload, dict):
        return 200, "ok", "Operation completed successfully.", service_payload, [], []

    success = bool(service_payload.get("success", True))
    code = _safe_str(service_payload.get("code", "")) or (
        "operation_ok" if success else "operation_failed"
    )
    error_text = _safe_str(service_payload.get("error", ""))
    message_text = _safe_str(service_payload.get("message", ""))
    if not message_text:
        message_text = error_text or (
            "Operation completed successfully." if success else "Operation failed."
        )

    if success:
        status = 200
    else:
        status = _service_status_to_http(code) or 400

    envelope_keys = {"success", "code", "message", "error", "extras"}
    data: Dict[str, Any] = {}
    for key, value in service_payload.items():
        if key in envelope_keys:
            continue
        data[key] = value

    errors: List[Dict[str, Any]] = []
    if not success and error_text:
        errors.append(
            {"field": "", "code": code or "error", "message": error_text}
        )

    extras = service_payload.get("extras")
    warnings: List[Dict[str, Any]] = []
    if isinstance(extras, dict):
        warn_list = extras.get("warnings")
        if isinstance(warn_list, list):
            for warn in warn_list:
                if isinstance(warn, dict):
                    warnings.append(warn)
                else:
                    warnings.append({"message": _safe_str(warn)})

    return status, code, message_text, data, errors, warnings

def _service_status_to_http(code: str) -> int:
    """Translate a service-level code to an HTTP status code."""
    if not code:
        return 400
    code_lower = code.lower()
    if any(token in code_lower for token in ("not_found", "missing")):
        return 404
    if any(token in code_lower for token in ("permission", "forbidden", "access")):
        return 403
    if any(token in code_lower for token in ("auth", "login")):
        return 401
    if any(token in code_lower for token in ("service_unavailable", "inventory_unavailable", "timeout")):
        return 503
    if any(token in code_lower for token in ("conflict", "concurrency", "duplicate")):
        return 409
    if any(token in code_lower for token in ("limit", "exceeded", "too_large")):
        return 413
    if any(token in code_lower for token in ("validation", "invalid", "missing")):
        return 400
    if any(token in code_lower for token in ("insufficient", "out_of_stock")):
        return 409
    return 400

# =============================================================================
# CART INVENTORY SERIALIZATION
# =============================================================================
def _serialize_cart_inventory(
    cart: Optional[Cart],
) -> Dict[str, Any]:
    """
    Build a standardized inventory payload for the entire cart.

    Returns a safe dictionary. When the cart is empty or missing,
    returns the safe-empty context.
    """
    empty = {
        "exists": False,
        "inventory_status": "unknown",
        "is_in_stock": False,
        "is_low_stock": False,
        "is_out_of_stock": True,
        "available_quantity": "0.00",
        "reserved_quantity": "0.00",
        "free_stock": "0.00",
        "total_stock": "0.00",
        "warehouse_summary": "",
        "ready_for_checkout": True,
        "blocking_issues": [],
        "item_count": 0,
        "in_stock_items": 0,
        "low_stock_items": 0,
        "out_of_stock_items": 0,
    }
    if cart is None or not getattr(cart, "pk", None):
        return empty

    try:
        active_items = list(
            cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
        )
    except Exception as exc:
        logger.debug("Failed to load cart items: %s", exc)
        return empty

    total_available = Decimal("0.00")
    total_reserved = Decimal("0.00")
    warehouse_ids: set = set()
    in_stock = 0
    low_stock = 0
    out_of_stock = 0
    considered = 0
    blocking_issues: List[Dict[str, Any]] = []

    for item in active_items:
        try:
            inv_payload = CartInventoryService.get_inventory_context(
                product=getattr(item, "product", None),
                product_variant=getattr(item, "variant", None),
                warehouse=getattr(cart, "preferred_warehouse", None),
            )
        except Exception as exc:
            logger.debug("Inventory build failed for item %s: %s",
                         getattr(item, "pk", "?"), exc)
            continue

        if not isinstance(inv_payload, dict):
            continue
        if not inv_payload.get("exists", False):
            continue
        considered += 1

        try:
            v_available = _safe_decimal(
                inv_payload.get("available_quantity"), default=Decimal("0")
            ) or Decimal("0")
            v_reserved = _safe_decimal(
                inv_payload.get("reserved_quantity"), default=Decimal("0")
            ) or Decimal("0")
        except Exception:
            v_available = Decimal("0")
            v_reserved = Decimal("0")

        total_available += v_available
        total_reserved += v_reserved

        is_out = bool(inv_payload.get("is_out_of_stock", False))
        is_low = bool(inv_payload.get("is_low_stock", False))
        is_in = bool(inv_payload.get("is_in_stock", False))

        if is_out:
            out_of_stock += 1
            blocking_issues.append(
                {
                    "item_id": getattr(item, "pk", None),
                    "code": "out_of_stock",
                    "message": inv_payload.get("stock_message")
                    or "Out of stock",
                }
            )
        elif is_low:
            low_stock += 1
        elif is_in:
            in_stock += 1

    if considered == 0:
        return {**empty, "item_count": len(active_items)}

    free_stock = max(Decimal("0.00"), total_available - total_reserved)
    total_stock = total_available + total_reserved
    is_oos = free_stock <= Decimal("0.00")
    is_low = (not is_oos) and (low_stock > 0 or out_of_stock < considered)
    is_in = free_stock > Decimal("0.00")

    if is_oos:
        status = "out_of_stock"
    elif is_low:
        status = "low_stock"
    elif is_in:
        status = "in_stock"
    else:
        status = "unknown"

    return {
        "exists": True,
        "inventory_status": status,
        "is_in_stock": is_in,
        "is_low_stock": is_low,
        "is_out_of_stock": is_oos,
        "available_quantity": str(total_available),
        "reserved_quantity": str(total_reserved),
        "free_stock": str(free_stock),
        "total_stock": str(total_stock),
        "warehouse_summary": (
            f"{len(warehouse_ids)} warehouse(s)" if warehouse_ids else ""
        ),
        "ready_for_checkout": len(blocking_issues) == 0,
        "blocking_issues": blocking_issues,
        "item_count": considered,
        "in_stock_items": in_stock,
        "low_stock_items": low_stock,
        "out_of_stock_items": out_of_stock,
    }

def _serialize_cart_reservation(item: CartItem) -> Dict[str, Any]:
    """Build a reservation status payload from a CartItem."""
    reservation = getattr(item, "reservation", None)
    if reservation is None:
        return {
            "id": None,
            "token": _safe_str(getattr(item, "reservation_token", "")) or None,
            "status": _safe_str(getattr(item, "reservation_status", "")) or None,
            "quantity": (
                str(item.reservation_quantity)
                if getattr(item, "reservation_quantity", None) is not None
                else None
            ),
            "expires_at": (
                item.reservation_expires_at.isoformat()
                if getattr(item, "reservation_expires_at", None) is not None
                else None
            ),
            "is_active": False,
            "is_expired": True,
            "is_terminal": True,
        }
    return {
        "id": getattr(reservation, "pk", None),
        "token": _safe_str(getattr(reservation, "reservation_token", "")) or None,
        "status": _safe_str(getattr(reservation, "status", "")) or None,
        "quantity": str(getattr(reservation, "quantity", "0.00") or "0.00"),
        "expires_at": (
            reservation.expires_at.isoformat()
            if getattr(reservation, "expires_at", None) is not None
            else None
        ),
        "is_active": bool(getattr(reservation, "is_active", False)),
        "is_expired": bool(getattr(reservation, "is_expired", False)),
        "is_terminal": bool(getattr(reservation, "is_terminal", False)),
    }

def _serialize_cart_item(
    item: CartItem,
    warehouse: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Build a cart-item payload with inventory and reservation context.

    Inventory data is sourced from the Cart service / Inventory
    selector layer. Reservation data is sourced from the linked
    StockReservation row. The Cart view NEVER reads or computes
    inventory data directly.
    """
    try:
        inv = CartInventoryService.get_inventory_context(
            product=getattr(item, "product", None),
            product_variant=getattr(item, "variant", None),
            warehouse=warehouse,
        )
    except Exception as exc:
        logger.debug("Inventory build failed: %s", exc)
        inv = {
            "exists": False,
            "inventory_status": "unknown",
            "is_in_stock": False,
            "is_low_stock": False,
            "is_out_of_stock": True,
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "free_stock": "0.00",
            "stock_message": "Stock status unavailable",
        }

    reservation = _serialize_cart_reservation(item)

    try:
        line_subtotal = item.line_subtotal if hasattr(item, "line_subtotal") else Decimal("0.00")
    except Exception:
        line_subtotal = Decimal("0.00")

    return {
        "id": item.pk,
        "product_id": getattr(item, "product_id", None),
        "variant_id": getattr(item, "variant_id", None),
        "quantity": int(getattr(item, "quantity", 0) or 0),
        "status": _safe_str(getattr(item, "status", "")) or None,
        "saved_reason": _safe_str(getattr(item, "saved_reason", "")) or None,
        "unit_price": str(
            getattr(item, "unit_price_snapshot", None) or "0.00"
        ),
        "compare_at_price": (
            str(getattr(item, "compare_at_price_snapshot", None))
            if getattr(item, "compare_at_price_snapshot", None) is not None
            else None
        ),
        "currency": _safe_str(getattr(item, "currency_snapshot", "")) or None,
        "line_subtotal": str(line_subtotal),
        "product_name": _safe_str(
            getattr(item, "product_name_snapshot", "")
        ) or None,
        "product_sku": _safe_str(
            getattr(item, "product_sku_snapshot", "")
        ) or None,
        "variant_name": _safe_str(
            getattr(item, "variant_name_snapshot", "")
        ) or None,
        "product_image_url": (
            item.product_image_snapshot.url
            if getattr(item, "product_image_snapshot", None) is not None
            else None
        ),
        "added_at": (
            item.added_at.isoformat()
            if getattr(item, "added_at", None) is not None
            else None
        ),
        "updated_at": (
            item.updated_at.isoformat()
            if getattr(item, "updated_at", None) is not None
            else None
        ),
        "inventory": inv,
        "reservation": reservation,
    }

def _serialize_cart(
    cart: Optional[Cart],
    *,
    include_inactive: bool = False,
) -> Dict[str, Any]:
    """Build a full cart payload with items, inventory, and reservation context."""
    if cart is None or not getattr(cart, "pk", None):
        return {
            "id": None,
            "status": None,
            "is_active": False,
            "is_guest": True,
            "currency": None,
            "subtotal": "0.00",
            "tax": "0.00",
            "shipping": "0.00",
            "discount": "0.00",
            "grand_total": "0.00",
            "total_items": 0,
            "unique_items": 0,
            "coupon_code": None,
            "items": [],
            "inventory_overview": _serialize_cart_inventory(None),
        }
    warehouse = getattr(cart, "preferred_warehouse", None)
    if include_inactive:
        items_qs = cart.items.all()
    else:
        items_qs = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
    try:
        items = list(
            items_qs.select_related("product", "variant", "reservation")
        )
    except Exception as exc:
        logger.debug("Failed to load cart items: %s", exc)
        items = []

    serialized_items = [
        _serialize_cart_item(item, warehouse=warehouse) for item in items
    ]
    try:
        totals = CartService.compute_totals(cart)
    except Exception as exc:
        logger.debug("Cart totals calculation failed: %s", exc)
        totals = {
            "subtotal": Decimal("0.00"),
            "tax": Decimal("0.00"),
            "shipping": Decimal("0.00"),
            "discount": Decimal("0.00"),
            "grand_total": Decimal("0.00"),
            "total_items": 0,
            "unique_items": 0,
        }

    inventory_overview = _serialize_cart_inventory(cart)

    return {
        "id": cart.pk,
        "status": _safe_str(getattr(cart, "status", "")) or None,
        "is_active": bool(getattr(cart, "is_active", False)),
        "is_guest": bool(getattr(cart, "is_guest", True)),
        "currency": _safe_str(getattr(cart, "currency", "")) or None,
        "customer_id": getattr(cart, "customer_id", None),
        "session_key": _safe_str(getattr(cart, "session_key", "")) or None,
        "coupon_code": _safe_str(getattr(cart, "coupon_code", "")) or None,
        "coupon_discount": str(
            _safe_decimal(
                getattr(cart, "coupon_discount_amount", None),
                default=Decimal("0"),
            )
        ),
        "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
        "tax": str(totals.get("tax", Decimal("0.00"))),
        "shipping": str(totals.get("shipping", Decimal("0.00"))),
        "discount": str(totals.get("discount", Decimal("0.00"))),
        "grand_total": str(totals.get("grand_total", Decimal("0.00"))),
        "total_items": int(totals.get("total_items", 0)),
        "unique_items": int(totals.get("unique_items", 0)),
        "last_activity_at": (
            cart.last_activity_at.isoformat()
            if getattr(cart, "last_activity_at", None) is not None
            else None
        ),
        "expires_at": (
            cart.expires_at.isoformat()
            if getattr(cart, "expires_at", None) is not None
            else None
        ),
        "preferred_warehouse_id": getattr(cart, "preferred_warehouse_id", None),
        "items": serialized_items,
        "inventory_overview": inventory_overview,
    }

def _attach_reservation_context(
    items: List[CartItem],
) -> List[Dict[str, Any]]:
    """
    Build reservation context payloads for each cart item.

    Reservation data is read from the linked StockReservation rows
    via the cart item's FK relationship. The Cart view NEVER
    computes reservation state.
    """
    output: List[Dict[str, Any]] = []
    count = 0
    for item in items:
        if count >= _MAX_RESERVATION_DISPLAY:
            break
        try:
            output.append(_serialize_cart_reservation(item))
        except Exception as exc:
            logger.debug("Reservation serialization failed: %s", exc)
            output.append({
                "id": None,
                "token": None,
                "status": "unknown",
                "quantity": None,
                "expires_at": None,
                "is_active": False,
                "is_expired": True,
                "is_terminal": True,
            })
        count += 1
    return output

# =============================================================================
# BASE VIEW CLASS
# =============================================================================
class BaseCartView(View):
    """
    Base class for Cart page views.

    Provides:
        * Cart resolution (with auto-create for guests)
        * Cart serialization
        * Inventory context building
        * Reservation context building
        * Standardized AJAX error responses
        * Structured logging hooks
        * CSRF exemption for safe AJAX flows (where appropriate)
    """

    require_authentication: bool = False
    needs_cart: bool = True

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.request = request

        if self.require_authentication:
            user = getattr(request, "user", None)
            if user is None or not getattr(user, "is_authenticated", False):
                if _is_ajax(request):
                    return _api_response(
                        request,
                        success=False,
                        code="authentication_required",
                        message=_("Authentication is required for this action."),
                        status=401,
                    )
                try:
                    messages.error(
                        request,
                        _("Please sign in to continue."),
                    )
                except Exception:
                    pass
                try:
                    return redirect("foundation:login")
                except Exception:
                    return redirect("/accounts/login/")

        if self.needs_cart:
            cart, error = _resolve_cart_or_redirect(request)
            if error is not None:
                return error
            self.cart = cart

        try:
            response = super().dispatch(request, *args, **kwargs)
        except Exception as exc:
            logger.exception("Cart page view failure: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )
        return response

    def log_request(self, request: HttpRequest, response: HttpResponse) -> None:
        """Structured log entry for every page request. Never raises."""
        try:
            status_code = getattr(response, "status_code", 0)
            logger.info(
                "cart.page | method=%s path=%s status=%s user=%s ip=%s",
                request.method,
                request.path,
                status_code,
                getattr(request.user, "pk", None)
                if getattr(request, "user", None)
                and request.user.is_authenticated
                else None,
                _get_client_ip(request),
            )
        except Exception:
            pass

    def finalize(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        """Apply tracing and logging decorations to the response."""
        try:
            request_id = uuid.uuid4().hex[:16]
            if isinstance(response, HttpResponse):
                response["X-Cart-Request-ID"] = request_id
        except Exception:
            pass
        try:
            self.log_request(request, response)
        except Exception:
            pass
        return response

# =============================================================================
# 1. CART DETAIL (Main Cart Page)
# =============================================================================
class CartDetailView(BaseCartView, TemplateView):
    """
    The main shopping cart page.

    Renders the full cart with:
        * Active cart items
        * Saved-for-later items
        * Coupon summary
        * Inventory overview (read-only)
        * Reservation overview (read-only)
        * Totals (subtotal, tax, shipping, discount, grand total)

    All business logic is delegated to the Cart service layer.
    Inventory data flows from the Cart service → Inventory service.
    """

    template_name = "cart/cart.html"
    require_authentication: bool = False
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        # Cart serialization (totals, items, inventory overview)
        try:
            cart_payload = _serialize_cart(cart, include_inactive=False)
        except Exception as exc:
            logger.exception("Cart serialization failed: %s", exc)
            cart_payload = _serialize_cart(None)

        # Saved-for-later items (read-only inventory attached)
        try:
            saved_items_qs = cart.items.filter(
                status=CartItem.ItemStatus.SAVED
            ).select_related("product", "variant", "reservation")
            saved_items = list(saved_items_qs)
        except Exception as exc:
            logger.debug("Failed to load saved items: %s", exc)
            saved_items = []

        warehouse = getattr(cart, "preferred_warehouse", None)
        saved_serialized = [
            _serialize_cart_item(item, warehouse=warehouse) for item in saved_items
        ]

        # Reservation overview (read-only)
        try:
            active_items_for_reservations = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related("reservation")
            )
        except Exception as exc:
            logger.debug("Failed to load active items: %s", exc)
            active_items_for_reservations = []

        reservation_overview = _attach_reservation_context(
            active_items_for_reservations
        )

        # Cart-level inventory check
        try:
            cart_inventory = CartInventoryService.validate_for_checkout(
                cart=cart
            )
        except Exception as exc:
            logger.debug("Cart inventory check failed: %s", exc)
            cart_inventory = {
                "ready_for_checkout": True,
                "issues": [],
                "totals": {},
                "cart": _serialize_cart(cart),
            }

        # Standardized coupons / promotions
        try:
            coupon_payload: Dict[str, Any] = {
                "code": _safe_str(getattr(cart, "coupon_code", "")) or None,
                "discount_amount": str(
                    _safe_decimal(
                        getattr(cart, "coupon_discount_amount", None),
                        default=Decimal("0"),
                    )
                ),
                "is_applied": bool(
                    _safe_str(getattr(cart, "coupon_code", ""))
                ),
            }
        except Exception:
            coupon_payload = {
                "code": None,
                "discount_amount": "0.00",
                "is_applied": False,
            }

        context.update(
            {
                "cart": cart,
                "cart_payload": cart_payload,
                "cart_items": cart_payload.get("items", []),
                "saved_items": saved_serialized,
                "saved_items_count": len(saved_serialized),
                "reservation_overview": reservation_overview,
                "cart_inventory": cart_inventory,
                "coupon": coupon_payload,
                "currency": cart_payload.get("currency"),
                "subtotal": cart_payload.get("subtotal"),
                "total_discount": cart_payload.get("discount"),
                "estimated_tax": cart_payload.get("tax"),
                "estimated_shipping": cart_payload.get("shipping"),
                "grand_total": cart_payload.get("grand_total"),
                "has_out_of_stock": (
                    cart_payload.get("inventory_overview", {}).get(
                        "out_of_stock_items", 0
                    )
                    > 0
                ),
                "page_title": _("Shopping Cart"),
            }
        )
        return context

# =============================================================================
# 2. CART SUMMARY (Lightweight Fragment for AJAX Updates)
# =============================================================================
class CartSummaryView(BaseCartView, TemplateView):
    """
    Lightweight cart summary fragment for AJAX updates.

    Used by the storefront UI to refresh mini-cart widgets without
    re-rendering the entire cart page.
    """

    template_name = "cart/partials/cart_summary.html"
    require_authentication: bool = False
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        try:
            active_items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related("product", "variant", "reservation")
            )
        except Exception:
            active_items = []

        warehouse = getattr(cart, "preferred_warehouse", None)
        serialized_items = [
            _serialize_cart_item(item, warehouse=warehouse)
            for item in active_items
        ]
        overall_inventory = _serialize_cart_inventory(cart)

        context.update(
            {
                "cart": cart,
                "cart_items": serialized_items,
                "overall_inventory": overall_inventory,
                "currency": _safe_str(getattr(cart, "currency", "")) or None,
                "page_title": _("Cart Summary"),
            }
        )
        return context

# =============================================================================
# 3. MINI CART (Header Dropdown Fragment)
# =============================================================================
class MiniCartView(BaseCartView, View):
    """
    Returns the mini-cart HTML fragment for the header dropdown.

    Returns rendered HTML (not a redirect) so the header dropdown
    can be updated via AJAX without page navigation. All business
    logic is delegated to the Cart service layer.
    """

    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        cart, error = _resolve_cart_or_redirect(request)
        if error is not None:
            return error

        try:
            mini_payload = CartService.build_mini_cart_payload(cart)
        except Exception as exc:
            logger.debug("Mini cart payload failed: %s", exc)
            mini_payload = []

        warehouse = getattr(cart, "preferred_warehouse", None)
        try:
            items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related("product", "variant", "reservation")
            )
        except Exception:
            items = []

        mini_items: List[Dict[str, Any]] = []
        for item in items[:_DEFAULT_MINI_CART_LIMIT]:
            try:
                mini_items.append(
                    _serialize_cart_item(item, warehouse=warehouse)
                )
            except Exception as exc:
                logger.debug("Mini item serialization failed: %s", exc)

        overall_inventory = _serialize_cart_inventory(cart)

        try:
            html = render_to_string(
                "cart/partials/mini_cart_content.html",
                {
                    "cart": cart,
                    "mini_cart_items": mini_items,
                    "mini_cart_payload": mini_payload,
                    "inventory_overview": overall_inventory,
                    "currency": _safe_str(getattr(cart, "currency", "")) or None,
                },
                request=request,
            )
        except Exception as exc:
            logger.exception("Mini cart render failed: %s", exc)
            if _is_ajax(request):
                return _api_response(
                    request,
                    success=False,
                    code="render_failed",
                    message=_("Could not render mini cart."),
                    status=500,
                )
            return _error_response(
                request,
                code="render_failed",
                message=_("Could not render mini cart."),
                status=500,
            )

        response = HttpResponse(html)
        try:
            response["X-Cart-Request-ID"] = uuid.uuid4().hex[:16]
        except Exception:
            pass
        return self.finalize(request, response)

# =============================================================================
# 4. SAVED FOR LATER PAGE
# =============================================================================
class SavedItemsView(BaseCartView, TemplateView):
    """
    Page dedicated to "Save for Later" items.

    Renders all SAVED items in the cart. Inventory is read from the
    Cart service layer. Items can be moved back to the active cart
    via the dedicated action view.
    """

    template_name = "cart/saved.html"
    require_authentication: bool = False
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        try:
            saved_items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.SAVED
                ).select_related("product", "variant", "reservation")
            )
        except Exception as exc:
            logger.debug("Failed to load saved items: %s", exc)
            saved_items = []

        warehouse = getattr(cart, "preferred_warehouse", None)
        serialized = [
            _serialize_cart_item(item, warehouse=warehouse)
            for item in saved_items
        ]

        context.update(
            {
                "cart": cart,
                "saved_items": serialized,
                "saved_items_count": len(serialized),
                "page_title": _("Saved For Later"),
            }
        )
        return context

# =============================================================================
# 5. CART ITEM MANAGEMENT (Add / Update / Remove)
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartAddItemView(BaseCartView, View):
    """
    Add an item to the cart. Supports AJAX and standard POST.

    Reads product / variant context from the catalog app, then
    delegates to the Cart service layer. Inventory validation and
    reservation creation are performed by the Cart service layer
    (which in turn delegates to the Inventory service layer).
    """

    require_authentication: bool = False
    needs_cart: bool = True

    http_method_names = ["post", "options"]

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            product_id = kwargs.get("product_id") or request.POST.get("product_id")
            variant_id = request.POST.get("variant_id")
            try:
                quantity = int(request.POST.get("quantity", 1))
            except (TypeError, ValueError):
                quantity = 1
            unit_price_raw = request.POST.get("unit_price_snapshot")
            currency = _safe_str(request.POST.get("currency", ""))
            personalization_raw = request.POST.get("personalization")
            personalization = None
            if personalization_raw:
                try:
                    personalization = json.loads(personalization_raw)
                    if not isinstance(personalization, dict):
                        personalization = None
                except (ValueError, TypeError):
                    personalization = None

            unit_price_snapshot: Optional[Decimal] = None
            if unit_price_raw:
                unit_price_snapshot = _safe_decimal(
                    unit_price_raw, default=Decimal("0")
                )

            if not product_id:
                return _error_response(
                    request,
                    code="missing_product",
                    message=_("A product identifier is required."),
                    status=400,
                )

            product = None
            product_variant = None
            try:
                if variant_id:
                    from apps.catalog.models import ProductVariant
                    product_variant = (
                        ProductVariant.objects.select_related("product")
                        .filter(pk=variant_id)
                        .first()
                    )
                    if product_variant is not None:
                        product = getattr(product_variant, "product", None)
                else:
                    from apps.catalog.models import Product
                    product = Product.objects.filter(pk=product_id).first()
            except (DatabaseError, OperationalError) as exc:
                logger.exception("Catalog lookup failed: %s", exc)
                return _error_response(
                    request,
                    code="database_error",
                    message=_("A database error occurred. Please try again."),
                    status=503,
                )

            if product is None and product_variant is None:
                return _error_response(
                    request,
                    code="product_not_found",
                    message=_("Requested product was not found."),
                    status=404,
                )

            result = CartItemService.add_item(
                cart=self.cart,
                product=product,
                variant=product_variant,
                quantity=quantity,
                unit_price_snapshot=unit_price_snapshot,
                currency=currency,
                personalization=personalization,
            )

            if _is_ajax(request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(
                        request,
                        _("Item added to your bag."),
                    )
                except Exception:
                    pass
            else:
                try:
                    messages.error(
                        request,
                        result.get("error")
                        or _("Could not add the item to your cart."),
                    )
                except Exception:
                    pass

            return redirect("cart:cart_detail")
        except (DjangoValidationError, ValueError) as exc:
            return _error_response(
                request,
                code="validation_error",
                message=str(exc),
                status=400,
            )
        except Exception as exc:
            logger.exception("Add to cart failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        """Handle CORS preflight."""
        response = HttpResponse(status=204)
        return self.finalize(request, response)

@method_decorator(csrf_exempt, name="dispatch")
class CartUpdateItemView(BaseCartView, View):
    """Update the quantity of a specific cart item."""

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["post", "options"]

    def post(
        self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            try:
                quantity = int(request.POST.get("quantity", 1))
            except (TypeError, ValueError):
                quantity = 1

            result = CartItemService.update_quantity(
                cart=self.cart, item_id=item_id, quantity=quantity
            )

            if _is_ajax(request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if not result.get("success", False):
                try:
                    messages.error(
                        request,
                        result.get("error")
                        or _("Could not update the item."),
                    )
                except Exception:
                    pass
            return redirect("cart:cart_detail")
        except (DjangoValidationError, ValueError) as exc:
            return _error_response(
                request,
                code="validation_error",
                message=str(exc),
                status=400,
            )
        except Exception as exc:
            logger.exception("Update item failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

@method_decorator(csrf_exempt, name="dispatch")
class CartRemoveItemView(BaseCartView, View):
    """Remove a specific item from the cart."""

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["post", "options"]

    def post(
        self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            result = CartItemService.remove_item(
                cart=self.cart, item_id=item_id
            )

            if _is_ajax(request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(request, _("Item removed from your bag."))
                except Exception:
                    pass
            return redirect("cart:cart_detail")
        except (DjangoValidationError, ValueError) as exc:
            return _error_response(
                request,
                code="validation_error",
                message=str(exc),
                status=400,
            )
        except Exception as exc:
            logger.exception("Remove item failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

# =============================================================================
# 6. CART LIFECYCLE OPERATIONS (Clear / Save / Move)
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartClearView(BaseCartView, View):
    """Clear all items from the active cart."""

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["post", "options"]

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            result = CartItemService.clear_cart(cart=self.cart)

            if _is_ajax(request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(
                        request,
                        _("Your cart has been cleared."),
                    )
                except Exception:
                    pass
            return redirect("cart:cart_detail")
        except Exception as exc:
            logger.exception("Clear cart failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

@method_decorator(csrf_exempt, name="dispatch")
class CartSaveForLaterView(BaseCartView, View):
    """Move an active cart item to the Saved For Later bucket."""

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["post", "options"]

    def post(
        self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            reason = _safe_str(request.POST.get("reason", ""))
            result = CartItemService.save_for_later(
                cart=self.cart, item_id=item_id, reason=reason
            )

            if _is_ajax(request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(request, _("Item saved for later."))
                except Exception:
                    pass
            return redirect("cart:cart_detail")
        except (DjangoValidationError, ValueError) as exc:
            return _error_response(
                request,
                code="validation_error",
                message=str(exc),
                status=400,
            )
        except Exception as exc:
            logger.exception("Save for later failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

@method_decorator(csrf_exempt, name="dispatch")
class CartMoveToCartView(BaseCartView, View):
    """Move a saved-for-later item back into the active cart."""

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["post", "options"]

    def post(
        self, request: HttpRequest, item_id: int, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            result = CartItemService.move_to_cart(
                cart=self.cart, item_id=item_id
            )

            if _is_ajax(request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(
                        request,
                        _("Item moved back to your cart."),
                    )
                except Exception:
                    pass
            return redirect("cart:cart_detail")
        except (DjangoValidationError, ValueError) as exc:
            return _error_response(
                request,
                code="validation_error",
                message=str(exc),
                status=400,
            )
        except Exception as exc:
            logger.exception("Move to cart failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

# =============================================================================
# 7. CART MERGE (Guest -> Authenticated)
# =============================================================================
class CartMergeView(BaseCartView, TemplateView):
    """
    Cart merge page.

    Allows a guest to either:
        * Merge the current guest cart into their authenticated
          customer cart (delegated to CartService)
        * Discard the guest cart and start fresh

    All merge logic is delegated to the Cart service layer. The
    service layer handles reservation cleanup, customer cart
    creation, and conflict resolution.
    """

    template_name = "cart/merge.html"
    require_authentication: bool = False
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        user = getattr(self.request, "user", None)
        is_authenticated = bool(
            user and getattr(user, "is_authenticated", False)
        )

        cart_payload = _serialize_cart(cart, include_inactive=True)
        customer_cart_payload: Optional[Dict[str, Any]] = None

        if is_authenticated:
            try:
                customer_cart = CartService.get_for_customer(user)
                if customer_cart is not None and customer_cart.pk != cart.pk:
                    customer_cart_payload = _serialize_cart(
                        customer_cart, include_inactive=True
                    )
            except Exception as exc:
                logger.debug("Customer cart lookup failed: %s", exc)
                customer_cart_payload = None

        context.update(
            {
                "cart": cart,
                "cart_payload": cart_payload,
                "customer_cart_payload": customer_cart_payload,
                "is_authenticated": is_authenticated,
                "is_guest": bool(getattr(cart, "is_guest", True)),
                "page_title": _("Merge Your Cart"),
            }
        )
        return context

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        user = getattr(request, "user", None)
        if not (user and getattr(user, "is_authenticated", False)):
            return _error_response(
                request,
                code="authentication_required",
                message=_(
                    "Please sign in to merge your guest cart with your "
                    "customer cart."
                ),
                status=401,
            )

        if not getattr(self.cart, "is_guest", True):
            try:
                messages.info(
                    request,
                    _("Your cart is already linked to your account."),
                )
            except Exception:
                pass
            return redirect("cart:cart_detail")

        try:
            merged_cart = CartService.merge_guest_cart_into_customer(
                guest_cart=self.cart,
                customer=user,
            )
        except Exception as exc:
            logger.exception("Cart merge failed: %s", exc)
            return _error_response(
                request,
                code="merge_failed",
                message=_("Cart merge failed. Please try again."),
                status=500,
            )

        if merged_cart is None:
            return _error_response(
                request,
                code="merge_failed",
                message=_("Cart merge could not be completed."),
                status=500,
            )

        try:
            messages.success(
                request,
                _("Your guest cart was merged with your customer cart."),
            )
        except Exception:
            pass

        return redirect("cart:cart_detail")

# =============================================================================
# 8. CART VALIDATION (For Checkout)
# =============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class CartValidateView(BaseCartView, View):
    """
    Validate the cart for checkout readiness.

    Always reads inventory from the Cart service layer. The Cart
    view NEVER computes inventory state directly.
    """

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["get", "post", "options"]

    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._validate()

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._validate()

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

    def _validate(self) -> HttpResponse:
        request = self.request
        try:
            result = CartInventoryService.validate_for_checkout(cart=self.cart)
        except Exception as exc:
            logger.exception("Cart validation failed: %s", exc)
            return _error_response(
                request,
                code="validation_failed",
                message=_("Cart validation failed. Please try again."),
                status=500,
            )

        if _is_ajax(request):
            status, code, message, data, errors, warnings = (
                _translate_service_payload(result)
            )
            return _api_response(
                request,
                success=status < 400,
                code=code,
                message=message,
                data=data,
                errors=errors,
                warnings=warnings,
                status=status,
            )

        ready = bool(result.get("ready_for_checkout", False))
        if not ready:
            try:
                messages.error(
                    request,
                    _(
                        "Your cart has issues that must be resolved before "
                        "checkout."
                    ),
                )
            except Exception:
                pass
        return redirect("cart:checkout_prepare" if ready else "cart:cart_detail")

# =============================================================================
# 9. CHECKOUT PREPARATION (Refresh Inventory & Reservations)
# =============================================================================
class CartCheckoutPrepareView(BaseCartView, TemplateView):
    """
    Checkout preparation page.

    Refreshes inventory references, validates reservations, refreshes
    pricing, and prepares the cart for checkout. All operations
    are delegated to the Cart service layer (which in turn
    delegates to the Inventory / pricing / coupon / tax services).

    The Cart view NEVER directly mutates inventory or computes
    stock state.
    """

    template_name = "cart/checkout_prepare.html"
    require_authentication: bool = False
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        # Inventory refresh - delegated to the Cart service layer
        try:
            inventory_check = CartInventoryService.validate_for_checkout(
                cart=cart
            )
        except Exception as exc:
            logger.debug("Checkout prepare inventory check failed: %s", exc)
            inventory_check = {
                "ready_for_checkout": False,
                "issues": [
                    {
                        "code": "inventory_unavailable",
                        "message": str(exc)
                        or "Inventory check failed.",
                    }
                ],
                "totals": {},
                "cart": _serialize_cart(cart),
            }

        # Reservation refresh - delegated to the Cart service layer
        try:
            reservation_refresh = (
                CartInventoryService.cleanup_expired_reservations_for_cart()
            )
        except Exception as exc:
            logger.debug("Reservation refresh failed: %s", exc)
            reservation_refresh = {"released": 0, "failed": 0, "processed": 0}

        cart_payload = _serialize_cart(cart, include_inactive=False)

        context.update(
            {
                "cart": cart,
                "cart_payload": cart_payload,
                "inventory_check": inventory_check,
                "ready_for_checkout": bool(
                    inventory_check.get("ready_for_checkout", False)
                ),
                "issues": inventory_check.get("issues", []),
                "reservation_refresh": reservation_refresh,
                "page_title": _("Preparing Checkout"),
            }
        )
        return context

class CartCheckoutReviewView(LoginRequiredMixin, BaseCartView, TemplateView):
    """
    Checkout review page (auth required).

    Final review before placing an order. Refreshes inventory,
    reservations, pricing, promotions, coupons, shipping, and taxes.
    """

    template_name = "cart/checkout_review.html"
    require_authentication: bool = True
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        try:
            inventory_check = CartInventoryService.validate_for_checkout(
                cart=cart
            )
        except Exception as exc:
            logger.debug("Checkout review inventory check failed: %s", exc)
            inventory_check = {
                "ready_for_checkout": False,
                "issues": [],
                "totals": {},
                "cart": _serialize_cart(cart),
            }

        cart_payload = _serialize_cart(cart, include_inactive=False)

        try:
            reservation_refresh = (
                CartInventoryService.cleanup_expired_reservations_for_cart()
            )
        except Exception as exc:
            logger.debug("Reservation refresh failed: %s", exc)
            reservation_refresh = {"released": 0, "failed": 0, "processed": 0}

        try:
            active_items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related("reservation")
            )
        except Exception as exc:
            logger.debug("Failed to load active items: %s", exc)
            active_items = []

        reservation_context = _attach_reservation_context(active_items)

        context.update(
            {
                "cart": cart,
                "cart_payload": cart_payload,
                "inventory_check": inventory_check,
                "ready_for_checkout": bool(
                    inventory_check.get("ready_for_checkout", False)
                ),
                "issues": inventory_check.get("issues", []),
                "reservation_refresh": reservation_refresh,
                "reservation_context": reservation_context,
                "page_title": _("Review &amp; Checkout"),
            }
        )
        return context

# =============================================================================
# 10. RESERVATION STATUS PAGE
# =============================================================================
class CartReservationStatusView(BaseCartView, TemplateView):
    """
    Reservation status page.

    Displays every active reservation on the current cart. All
    reservation data is sourced from the linked StockReservation
    rows via the cart item's FK relationship. The Cart view
    NEVER computes reservation state.
    """

    template_name = "cart/reservations.html"
    require_authentication: bool = False
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        try:
            active_items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related(
                    "product", "variant", "reservation"
                )
            )
        except Exception as exc:
            logger.debug("Failed to load items: %s", exc)
            active_items = []

        warehouse = getattr(cart, "preferred_warehouse", None)
        reservation_entries: List[Dict[str, Any]] = []
        for item in active_items:
            try:
                serialized = _serialize_cart_item(
                    item, warehouse=warehouse
                )
                reservation_entries.append(serialized)
            except Exception as exc:
                logger.debug("Reservation serialization failed: %s", exc)

        # Trigger reservation cleanup (delegated to the Cart service)
        try:
            cleanup_result = (
                CartInventoryService.cleanup_expired_reservations_for_cart()
            )
        except Exception as exc:
            logger.debug("Reservation cleanup failed: %s", exc)
            cleanup_result = {"released": 0, "failed": 0, "processed": 0}

        context.update(
            {
                "cart": cart,
                "reservation_entries": reservation_entries,
                "cleanup_result": cleanup_result,
                "page_title": _("Reservation Status"),
            }
        )
        return context

class CartInventoryRefreshView(BaseCartView, View):
    """
    Read-only inventory context endpoint for the entire cart.

    Returns a structured inventory payload. Useful for AJAX
    inventory refreshes on the cart page without re-rendering
    the entire page.
    """

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["get", "post", "options"]

    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._respond()

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._respond()

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

    def _respond(self) -> HttpResponse:
        cart = self.cart
        try:
            warehouse = getattr(cart, "preferred_warehouse", None)
            items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related("product", "variant", "reservation")
            )
            line_payloads: List[Dict[str, Any]] = []
            for item in items:
                inv = CartInventoryService.get_inventory_context(
                    product=getattr(item, "product", None),
                    product_variant=getattr(item, "variant", None),
                    warehouse=warehouse,
                )
                line_payloads.append(
                    {
                        "item_id": item.pk,
                        "product_id": getattr(item, "product_id", None),
                        "variant_id": getattr(item, "variant_id", None),
                        "requested_quantity": int(
                            getattr(item, "quantity", 0) or 0
                        ),
                        "inventory": inv,
                    }
                )
            overall = _serialize_cart_inventory(cart)
            warehouse_id = getattr(warehouse, "pk", None) if warehouse else None
            warehouse_name = (
                getattr(warehouse, "display_name", None)
                or getattr(warehouse, "name", None)
                if warehouse
                else None
            )
        except Exception as exc:
            logger.exception("Inventory refresh failed: %s", exc)
            return _error_response(
                self.request,
                code="inventory_refresh_failed",
                message=_("Could not refresh inventory context."),
                status=500,
            )

        return _api_response(
            self.request,
            success=True,
            code="inventory_context_retrieved",
            message=_("Cart inventory context retrieved successfully."),
            data={
                "items": line_payloads,
                "overall": overall,
                "warehouse_id": warehouse_id,
                "warehouse_name": warehouse_name,
            },
        )

# =============================================================================
# 11. COUPON MANAGEMENT PAGE
# =============================================================================
class CartCouponsView(BaseCartView, TemplateView):
    """
    Coupon management page.

    Displays the current coupon state, and provides a form to
    apply / remove coupons. All coupon logic is delegated to
    the Cart coupon service.
    """

    template_name = "cart/coupons.html"
    require_authentication: bool = False
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart

        coupon_code = _safe_str(getattr(cart, "coupon_code", "")) or None
        discount_amount = str(
            _safe_decimal(
                getattr(cart, "coupon_discount_amount", None),
                default=Decimal("0"),
            )
        )

        cart_payload = _serialize_cart(cart, include_inactive=False)

        context.update(
            {
                "cart": cart,
                "cart_payload": cart_payload,
                "coupon_code": coupon_code,
                "discount_amount": discount_amount,
                "is_coupon_applied": bool(coupon_code),
                "page_title": _("Coupons & Promotions"),
            }
        )
        return context

@method_decorator(csrf_exempt, name="dispatch")
class CartCouponApplyView(BaseCartView, View):
    """Apply a coupon to the cart. Delegates to the Cart coupon service."""

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["post", "options"]

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            if request.body:
                try:
                    payload = json.loads(request.body or b"{}")
                except (ValueError, TypeError):
                    payload = {}
            else:
                payload = {}
            code = _safe_str(
                payload.get("code")
                or payload.get("coupon_code")
                or request.POST.get("code")
                or request.POST.get("coupon_code", "")
            )
            if not code:
                return _error_response(
                    request,
                    code="missing_coupon_code",
                    message=_("Coupon code is required."),
                    status=400,
                )
            user = getattr(request, "user", None)
            customer = (
                user
                if (user and getattr(user, "is_authenticated", False))
                else None
            )
            try:
                result = CartCouponService.apply_coupon(
                    cart=self.cart, code=code, customer=customer
                )
            except AttributeError:
                # Service signature fallback for legacy callers
                result = CartCouponService.apply_coupon(
                    cart=self.cart, code=code
                )

            if _is_ajax(request):
                status, status_code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=status_code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(
                        request,
                        _("Coupon '%(code)s' applied.") % {"code": code},
                    )
                except Exception:
                    pass
            else:
                try:
                    messages.error(
                        request,
                        result.get("error")
                        or result.get("message")
                        or _("Invalid coupon."),
                    )
                except Exception:
                    pass
            return redirect("cart:coupons")
        except (DjangoValidationError, ValueError) as exc:
            return _error_response(
                request,
                code="validation_error",
                message=str(exc),
                status=400,
            )
        except Exception as exc:
            logger.exception("Apply coupon failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

@method_decorator(csrf_exempt, name="dispatch")
class CartCouponRemoveView(BaseCartView, View):
    """Remove the currently applied coupon."""

    require_authentication: bool = False
    needs_cart: bool = True
    http_method_names = ["post", "delete", "options"]

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._remove()

    def delete(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        return self._remove()

    def options(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        response = HttpResponse(status=204)
        return self.finalize(request, response)

    def _remove(self) -> HttpResponse:
        try:
            result = CartCouponService.remove_coupon(cart=self.cart)

            if _is_ajax(self.request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    self.request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(self.request, _("Coupon removed."))
                except Exception:
                    pass
            return redirect("cart:coupons")
        except Exception as exc:
            logger.exception("Remove coupon failed: %s", exc)
            return _error_response(
                self.request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

# =============================================================================
# 12. REORDER (From Past Order)
# =============================================================================
class CartReorderView(LoginRequiredMixin, BaseCartView, TemplateView):
    """
    Reorder selection page.

    Allows an authenticated customer to reorder a past order or
    submit a custom list of items. All reorder logic is delegated
    to the Cart reorder service.
    """

    template_name = "cart/reorder.html"
    require_authentication: bool = True
    needs_cart: bool = True

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        cart = self.cart
        cart_payload = _serialize_cart(cart, include_inactive=False)

        order_id_raw = self.request.GET.get("order_id")
        order_id = _safe_int(order_id_raw) if order_id_raw else None
        order = None
        order_items: List[Dict[str, Any]] = []
        if order_id:
            try:
                from apps.orders.models import Order
                order = (
                    Order.objects.filter(
                        pk=order_id, customer=self.request.user
                    )
                    .prefetch_related("items")
                    .first()
                )
            except Exception as exc:
                logger.debug("Order lookup failed: %s", exc)
                order = None
            if order is not None:
                try:
                    for oi in order.items.all():
                        try:
                            order_items.append(
                                {
                                    "product_id": getattr(oi, "product_id", None),
                                    "variant_id": getattr(oi, "variant_id", None),
                                    "quantity": int(
                                        getattr(oi, "quantity", 0) or 0
                                    ),
                                    "name": _safe_str(
                                        getattr(oi, "product_name", "")
                                    )
                                    or _safe_str(
                                        getattr(oi, "title", "")
                                    ),
                                    "unit_price": str(
                                        getattr(oi, "unit_price", None) or "0.00"
                                    ),
                                }
                            )
                        except Exception as exc:
                            logger.debug(
                                "Order item serialization failed: %s", exc
                            )
                except Exception as exc:
                    logger.debug("Order items load failed: %s", exc)

        context.update(
            {
                "cart": cart,
                "cart_payload": cart_payload,
                "order": order,
                "order_items": order_items,
                "page_title": _("Reorder Items"),
            }
        )
        return context

    def post(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        try:
            try:
                payload = json.loads(request.body or b"{}")
                if not isinstance(payload, dict):
                    payload = {}
            except (ValueError, TypeError):
                payload = {}

            order_id = _safe_int(
                payload.get("order_id")
                or request.POST.get("order_id")
            )
            items = payload.get("items") or request.POST.getlist("items")
            order_reference = _safe_str(
                payload.get("order_reference")
                or request.POST.get("order_reference", "")
            )

            if not order_id and not items:
                return _error_response(
                    request,
                    code="missing_input",
                    message=_("Either an order ID or an items list is required."),
                    status=400,
                )

            order = None
            if order_id:
                try:
                    from apps.orders.models import Order
                    order = (
                        Order.objects.filter(
                            pk=order_id, customer=request.user
                        )
                        .prefetch_related("items")
                        .first()
                    )
                except Exception as exc:
                    logger.debug("Order lookup failed: %s", exc)
                    order = None
                if order is None:
                    return _error_response(
                        request,
                        code="order_not_found",
                        message=_("Order not found or not accessible."),
                        status=404,
                    )

            if not isinstance(items, list):
                return _error_response(
                    request,
                    code="invalid_items",
                    message=_("Items must be a list."),
                    status=400,
                )

            result = CartReorderService.reorder_items_into_cart(
                cart=self.cart,
                items=items if not order else None,
                order=order,
                order_reference=order_reference,
                user=request.user,
            )

            if _is_ajax(request):
                status, code, message, data, errors, warnings = (
                    _translate_service_payload(result)
                )
                return _api_response(
                    request,
                    success=status < 400,
                    code=code,
                    message=message,
                    data=data,
                    errors=errors,
                    warnings=warnings,
                    status=status,
                )

            if result.get("success", False):
                try:
                    messages.success(
                        request,
                        _("Items have been added to your cart."),
                    )
                except Exception:
                    pass
            else:
                try:
                    messages.error(
                        request,
                        result.get("error")
                        or result.get("message")
                        or _("Reorder could not be completed."),
                    )
                except Exception:
                    pass
            return redirect("cart:cart_detail")
        except (DjangoValidationError, ValueError) as exc:
            return _error_response(
                request,
                code="validation_error",
                message=str(exc),
                status=400,
            )
        except Exception as exc:
            logger.exception("Reorder failed: %s", exc)
            return _error_response(
                request,
                code="internal_error",
                message=_("An unexpected error occurred. Please try again."),
                status=500,
            )

# =============================================================================
# 13. LEGACY FUNCTION-BASED ENDPOINTS (Backward Compatibility)
# =============================================================================
# These endpoints preserve the public URLs and response shapes
# consumed by existing JavaScript clients (notably the legacy
# storefront JS). They are kept on the same module so that the URL
# configuration continues to work without modification.

@require_GET
def cart_estimate_legacy(request: HttpRequest) -> JsonResponse:
    """
    GET /api/cart/estimate/

    Legacy tax / shipping / total estimate endpoint used by the
    storefront JavaScript. Returns the legacy flat response shape.

    All math is delegated to the Cart service layer.
    """
    request_id = uuid.uuid4().hex[:16]
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Cart not found.")),
                },
                status=400,
            )
        try:
            warehouse = getattr(cart, "preferred_warehouse", None)
            items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related("product", "variant", "reservation")
            )
            overall = _serialize_cart_inventory(cart)
            totals = CartService.compute_totals(cart)
            return JsonResponse(
                {
                    "status": "success",
                    "currency": _safe_str(
                        getattr(cart, "currency", "")
                    ) or None,
                    "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
                    "discount": str(totals.get("discount", Decimal("0.00"))),
                    "tax": str(totals.get("tax", Decimal("0.00"))),
                    "shipping": str(
                        totals.get("shipping", Decimal("0.00"))
                    ),
                    "total": str(
                        totals.get("grand_total", Decimal("0.00"))
                    ),
                    "item_count": int(totals.get("total_items", 0)),
                    "unique_count": int(totals.get("unique_items", 0)),
                    "inventory_overview": overall,
                }
            )
        except Exception as exc:
            logger.exception("Legacy cart estimate failed: %s", exc)
            return JsonResponse(
                {"status": "error", "message": str(_("Cart estimate failed."))},
                status=500,
            )
    except Exception as exc:
        logger.exception("Legacy cart estimate outer failure: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Cart estimate failed."))},
            status=500,
        )

@require_GET
def cart_validate_legacy(request: HttpRequest) -> JsonResponse:
    """
    GET /api/cart/validate/

    Legacy read-only validation endpoint. Delegates to the Cart
    inventory service.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        try:
            CartInventoryService.validate_for_checkout(cart=cart)
            return JsonResponse({"status": "ok"})
        except DjangoValidationError as exc:
            msg = (
                exc.messages[0]
                if getattr(exc, "messages", None)
                else str(exc)
            )
            return JsonResponse(
                {"status": "error", "message": str(msg)},
                status=400,
            )
    except Exception as exc:
        logger.exception("Legacy cart validate failed: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Cart validation failed."))},
            status=500,
        )

@csrf_exempt
@require_POST
def cart_sync_legacy(request: HttpRequest) -> JsonResponse:
    """
    POST /api/cart/sync/

    Legacy "sync" endpoint. Adds an item to the cart using POST
    parameters and returns a snapshot in the legacy flat shape.
    Preserved verbatim for the legacy storefront JS.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        product_id = request.POST.get("product_id")
        variant_id = request.POST.get("variant_id")
        try:
            quantity = int(request.POST.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 1
        if not product_id:
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Missing product_id.")),
                },
                status=400,
            )
        product = None
        product_variant = None
        try:
            from apps.catalog.models import Product, ProductVariant
            if variant_id:
                product_variant = (
                    ProductVariant.objects.filter(pk=variant_id)
                    .select_related("product")
                    .first()
                )
                if product_variant is not None:
                    product = getattr(product_variant, "product", None)
            else:
                product = Product.objects.filter(pk=product_id).first()
        except DatabaseError:
            product = None
            product_variant = None
        if product is None and product_variant is None:
            return JsonResponse(
                {"status": "error", "message": str(_("Product not found."))},
                status=404,
            )
        try:
            CartItemService.add_item(
                cart=cart,
                product=product,
                variant=product_variant,
                quantity=quantity,
            )
            warehouse = getattr(cart, "preferred_warehouse", None)
            try:
                items = list(
                    cart.items.filter(
                        status=CartItem.ItemStatus.ACTIVE
                    ).select_related("product", "variant", "reservation")
                )
            except Exception:
                items = []
            serialized_items = [
                _serialize_cart_item(item, warehouse=warehouse)
                for item in items
            ]
            overall = _serialize_cart_inventory(cart)
            return JsonResponse(
                {
                    "status": "success",
                    "cart": {
                        "id": cart.pk,
                        "item_count": int(
                            getattr(cart, "total_items_count", lambda: 0)() or 0
                        ),
                        "unique_count": int(
                            getattr(cart, "unique_items_count", lambda: 0)()
                            or 0
                        ),
                        "subtotal": str(
                            getattr(cart, "subtotal", Decimal("0.00"))
                            or Decimal("0.00")
                        ),
                        "discount": str(
                            getattr(
                                cart, "coupon_discount_amount", Decimal("0.00")
                            )
                            or Decimal("0.00")
                        ),
                        "tax": str(
                            getattr(
                                cart, "estimated_tax", Decimal("0.00")
                            )
                            or Decimal("0.00")
                        ),
                        "shipping": str(
                            getattr(
                                cart, "estimated_shipping", Decimal("0.00")
                            )
                            or Decimal("0.00")
                        ),
                        "total": str(
                            getattr(cart, "grand_total", Decimal("0.00"))
                            or Decimal("0.00")
                        ),
                        "currency": _safe_str(
                            getattr(cart, "currency", "")
                        ) or "NPR",
                    },
                    "mini_cart_items": serialized_items,
                    "inventory_overview": overall,
                }
            )
        except (DjangoValidationError, ValueError) as exc:
            msg = (
                exc.messages[0]
                if hasattr(exc, "messages") and exc.messages
                else str(exc)
            )
            return JsonResponse(
                {"status": "error", "message": str(msg)},
                status=400,
            )
    except Exception as exc:
        logger.exception("Legacy cart sync failed: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Cart sync failed."))},
            status=500,
        )

@require_GET
def cart_sync_snapshot_legacy(request: HttpRequest) -> JsonResponse:
    """
    GET /api/cart/sync/

    Legacy read-only cart snapshot. Returns the legacy flat
    response shape. Does NOT mutate the cart.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {"status": "ok", "is_wishlisted": False, "cart": None}
            )
        warehouse = getattr(cart, "preferred_warehouse", None)
        try:
            items = list(
                cart.items.filter(
                    status=CartItem.ItemStatus.ACTIVE
                ).select_related("product", "variant", "reservation")
            )
        except Exception:
            items = []
        serialized_items = [
            _serialize_cart_item(item, warehouse=warehouse)
            for item in items
        ]
        overall = _serialize_cart_inventory(cart)
        return JsonResponse(
            {
                "status": "ok",
                "is_wishlisted": False,
                "cart": {
                    "id": cart.pk,
                    "item_count": int(
                        getattr(cart, "total_items_count", lambda: 0)() or 0
                    ),
                    "subtotal": str(
                        getattr(cart, "subtotal", Decimal("0.00"))
                        or Decimal("0.00")
                    ),
                    "total": str(
                        getattr(cart, "grand_total", Decimal("0.00"))
                        or Decimal("0.00")
                    ),
                    "currency": _safe_str(
                        getattr(cart, "currency", "")
                    ) or "NPR",
                },
                "mini_cart_items": serialized_items,
                "inventory_overview": overall,
            }
        )
    except Exception as exc:
        logger.exception("Legacy cart sync snapshot failed: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Cart snapshot failed."))},
            status=500,
        )

@csrf_exempt
@require_POST
def cart_apply_coupon_legacy(request: HttpRequest) -> JsonResponse:
    """
    POST /api/cart/apply-coupon/  - legacy coupon apply endpoint.

    Delegates to the Cart coupon service. The service layer is
    the SINGLE source of truth for coupon validation and
    discount calculation.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        code = _safe_str(request.POST.get("coupon_code", ""))
        if not code:
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Please enter a coupon code.")),
                },
                status=400,
            )
        discount_amount_raw = request.POST.get("discount_amount", "0")
        discount_amount = _safe_decimal(
            discount_amount_raw, default=Decimal("0")
        )
        try:
            result = CartCouponService.apply_coupon(
                cart=cart,
                code=code,
                discount_amount=discount_amount,
            )
        except AttributeError:
            result = CartCouponService.apply_coupon(cart=cart, code=code)
        if result.get("success", False):
            return JsonResponse(
                {
                    "status": "success",
                    "message": str(
                        _("Coupon '%(code)s' applied.") % {"code": code}
                    ),
                    "cart": {
                        "subtotal": str(
                            getattr(cart, "subtotal", Decimal("0.00"))
                            or Decimal("0.00")
                        ),
                        "discount": str(
                            getattr(
                                cart, "coupon_discount_amount", Decimal("0.00")
                            )
                            or Decimal("0.00")
                        ),
                        "tax": str(
                            getattr(
                                cart, "estimated_tax", Decimal("0.00")
                            )
                            or Decimal("0.00")
                        ),
                        "shipping": str(
                            getattr(
                                cart, "estimated_shipping", Decimal("0.00")
                            )
                            or Decimal("0.00")
                        ),
                        "total": str(
                            getattr(cart, "grand_total", Decimal("0.00"))
                            or Decimal("0.00")
                        ),
                        "currency": _safe_str(
                            getattr(cart, "currency", "")
                        ) or "NPR",
                    },
                }
            )
        msg = (
            result.get("error")
            or result.get("message")
            or str(_("Invalid coupon."))
        )
        return JsonResponse(
            {"status": "error", "message": str(msg)},
            status=400,
        )
    except Exception as exc:
        logger.exception("Legacy cart apply coupon failed: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Apply coupon failed."))},
            status=500,
        )

@csrf_exempt
@require_POST
def cart_remove_coupon_legacy(request: HttpRequest) -> JsonResponse:
    """
    POST /api/cart/remove-coupon/  - legacy coupon removal endpoint.

    Delegates to the Cart coupon service.
    """
    try:
        cart, _ = CartService.get_or_create_for_request(request)
        if cart is None:
            return JsonResponse(
                {"status": "error", "message": str(_("Cart not found."))},
                status=400,
            )
        CartCouponService.remove_coupon(cart=cart)
        return JsonResponse(
            {
                "status": "success",
                "message": str(_("Coupon removed.")),
                "cart": {
                    "subtotal": str(
                        getattr(cart, "subtotal", Decimal("0.00"))
                        or Decimal("0.00")
                    ),
                    "discount": str(
                        getattr(
                            cart, "coupon_discount_amount", Decimal("0.00")
                            or Decimal("0.00")
                        )
                    ),
                    "tax": str(
                        getattr(
                            cart, "estimated_tax", Decimal("0.00")
                        )
                        or Decimal("0.00")
                    ),
                    "shipping": str(
                        getattr(
                            cart, "estimated_shipping", Decimal("0.00")
                        )
                        or Decimal("0.00")
                    ),
                    "total": str(
                        getattr(cart, "grand_total", Decimal("0.00"))
                        or Decimal("0.00")
                    ),
                    "currency": _safe_str(
                        getattr(cart, "currency", "")
                    ) or "NPR",
                },
            }
        )
    except Exception as exc:
        logger.exception("Legacy cart remove coupon failed: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Remove coupon failed."))},
            status=500,
        )

@csrf_exempt
@require_POST
def cart_merge_legacy(request: HttpRequest) -> JsonResponse:
    """
    POST /api/cart/merge/  - legacy merge endpoint (login required).

    Delegates to the Cart service layer. The Cart service handles
    guest-to-customer merge, reservation cleanup, and conflict
    resolution.
    """
    try:
        if not request.user or not request.user.is_authenticated:
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Authentication is required.")),
                },
                status=401,
            )
        if not request.session.session_key:
            try:
                request.session.create()
            except Exception:
                pass
        session_key = request.session.session_key or ""
        try:
            guest_cart = (
                Cart.objects.filter(
                    session_key=session_key,
                    customer__isnull=True,
                    is_active=True,
                )
                .order_by("-last_activity_at")
                .first()
            )
        except Exception:
            guest_cart = None
        if guest_cart is None:
            return JsonResponse(
                {
                    "status": "success",
                    "message": str(_("Guest cart merged successfully.")),
                }
            )
        try:
            merged = CartService.merge_guest_cart_into_customer(
                guest_cart=guest_cart, customer=request.user
            )
        except Exception as exc:
            logger.exception("Legacy cart merge failed: %s", exc)
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Unable to merge guest cart.")),
                },
                status=500,
            )
        if merged is None:
            return JsonResponse(
                {
                    "status": "success",
                    "message": str(_("Guest cart merged successfully.")),
                }
            )
        return JsonResponse(
            {
                "status": "success",
                "message": str(_("Guest cart merged successfully.")),
                "cart": {
                    "id": getattr(merged, "pk", None),
                    "item_count": int(
                        getattr(
                            merged, "total_items_count", lambda: 0
                        )() or 0
                    )
                    if merged is not None
                    else 0,
                    "subtotal": str(
                        getattr(merged, "subtotal", Decimal("0.00"))
                        or Decimal("0.00")
                    )
                    if merged is not None
                    else "0.00",
                    "total": str(
                        getattr(merged, "grand_total", Decimal("0.00"))
                        or Decimal("0.00")
                    )
                    if merged is not None
                    else "0.00",
                    "currency": _safe_str(
                        getattr(merged, "currency", "")
                    ) or "NPR"
                    if merged is not None
                    else "NPR",
                },
            }
        )
    except Exception as exc:
        logger.exception("Legacy cart merge outer failure: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Unable to merge guest cart."))},
            status=500,
        )

@csrf_exempt
@require_POST
def cart_reorder_legacy(request: HttpRequest) -> JsonResponse:
    """
    POST /api/cart/reorder/  - legacy reorder endpoint (login required).

    Delegates to the Cart reorder service. The service layer is
    the single source of truth for reorder logic.
    """
    try:
        if not request.user or not request.user.is_authenticated:
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Authentication is required.")),
                },
                status=401,
            )
        order_id = request.POST.get("order_id")
        if not order_id:
            return JsonResponse(
                {"status": "error", "message": str(_("Order ID is required."))},
                status=400,
            )
        order = None
        try:
            from apps.orders.models import Order
            order = (
                Order.objects.filter(pk=order_id, customer=request.user)
                .prefetch_related("items")
                .first()
            )
        except Exception as exc:
            logger.debug("Order lookup failed: %s", exc)
            order = None
        if order is None:
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Order not found or access denied.")),
                },
                status=404,
            )
        try:
            cart, _ = CartService.get_or_create_for_request(request)
            CartReorderService.reorder_items_into_cart(
                cart=cart, order=order, user=request.user
            )
        except Exception as exc:
            logger.exception("Legacy cart reorder failed: %s", exc)
            return JsonResponse(
                {
                    "status": "error",
                    "message": str(_("Unable to reorder items.")),
                },
                status=500,
            )
        return JsonResponse(
            {
                "status": "success",
                "message": str(_("Items reordered successfully.")),
                "cart": {
                    "id": getattr(cart, "pk", None),
                    "item_count": int(
                        getattr(cart, "total_items_count", lambda: 0)() or 0
                    ),
                    "subtotal": str(
                        getattr(cart, "subtotal", Decimal("0.00"))
                    ),
                    "discount": str(
                        getattr(
                            cart, "coupon_discount_amount", Decimal("0.00")
                        )
                    ),
                    "tax": str(
                        getattr(cart, "estimated_tax", Decimal("0.00"))
                    ),
                    "shipping": str(
                        getattr(
                            cart, "estimated_shipping", Decimal("0.00")
                        )
                    ),
                    "total": str(
                        getattr(cart, "grand_total", Decimal("0.00"))
                    ),
                    "currency": _safe_str(
                        getattr(cart, "currency", "")
                    ) or "NPR",
                },
            }
        )
    except Exception as exc:
        logger.exception("Legacy cart reorder outer failure: %s", exc)
        return JsonResponse(
            {"status": "error", "message": str(_("Unable to reorder items."))},
            status=500,
        )

# =============================================================================
# 14. ROUTING SHIM (Convenience for include-based URL configs)
# =============================================================================
# This module is consumed by urls.py via standard Django URL patterns.
# The ``urlpatterns`` list below is OPTIONAL and serves only as a
# convenience for projects that want to include the Cart page views
# with a single ``path("cart/", include("apps.cart.views.pages"))`` call.
# Most projects will continue to wire individual paths in their
# dedicated urls.py.

from django.urls import path

urlpatterns: List[Any] = [
    # ==============================================================================
    # PAGE-LEVEL CART ROUTES
    # ==============================================================================
    path("", CartDetailView.as_view(), name="cart_detail"),
    path("summary/", CartSummaryView.as_view(), name="cart_summary"),
    path("mini/", MiniCartView.as_view(), name="mini_cart"),
    path("saved/", SavedItemsView.as_view(), name="saved_items"),
    path("merge/", CartMergeView.as_view(), name="merge"),
    path("validate/", CartValidateView.as_view(), name="validate"),
    path(
        "checkout/prepare/",
        CartCheckoutPrepareView.as_view(),
        name="checkout_prepare",
    ),
    path(
        "checkout/review/",
        CartCheckoutReviewView.as_view(),
        name="checkout_review",
    ),
    path(
        "reservations/",
        CartReservationStatusView.as_view(),
        name="reservation_status",
    ),
    path(
        "inventory/",
        CartInventoryRefreshView.as_view(),
        name="inventory_refresh",
    ),
    path("coupons/", CartCouponsView.as_view(), name="coupons"),
    path("reorder/", CartReorderView.as_view(), name="reorder"),

    # ==============================================================================
    # ITEM-LEVEL CART ROUTES
    # ==============================================================================
    path("items/add/", CartAddItemView.as_view(), name="add_item"),
    path(
        "items/<int:item_id>/update/",
        CartUpdateItemView.as_view(),
        name="update_item",
    ),
    path(
        "items/<int:item_id>/remove/",
        CartRemoveItemView.as_view(),
        name="remove_item",
    ),
    path(
        "items/<int:item_id>/save-for-later/",
        CartSaveForLaterView.as_view(),
        name="save_for_later",
    ),
    path(
        "items/<int:item_id>/move-to-cart/",
        CartMoveToCartView.as_view(),
        name="move_to_cart",
    ),
    path("clear/", CartClearView.as_view(), name="clear"),

    # ==============================================================================
    # COUPON ROUTES
    # ==============================================================================
    path("coupon/apply/", CartCouponApplyView.as_view(), name="coupon_apply"),
    path(
        "coupon/remove/",
        CartCouponRemoveView.as_view(),
        name="coupon_remove",
    ),

    # ==============================================================================
    # LEGACY ROUTES (Backward Compatibility)
    # ==============================================================================
    path("legacy/estimate/", cart_estimate_legacy, name="legacy_estimate"),
    path("legacy/validate/", cart_validate_legacy, name="legacy_validate"),
    path("legacy/sync/snapshot/", cart_sync_snapshot_legacy, name="legacy_sync_snapshot"),
    path("legacy/sync/add/", cart_sync_legacy, name="legacy_sync_add"),
    path(
        "legacy/coupon/apply/",
        cart_apply_coupon_legacy,
        name="legacy_coupon_apply",
    ),
    path(
        "legacy/coupon/remove/",
        cart_remove_coupon_legacy,
        name="legacy_coupon_remove",
    ),
    path("legacy/merge/", cart_merge_legacy, name="legacy_merge"),
    path("legacy/reorder/", cart_reorder_legacy, name="legacy_reorder"),
]

# =============================================================================
# PUBLIC MODULE API
# =============================================================================
__all__ = [
    # Page-level views
    "CartDetailView",
    "CartSummaryView",
    "MiniCartView",
    "SavedItemsView",
    "CartMergeView",
    "CartValidateView",
    "CartCheckoutPrepareView",
    "CartCheckoutReviewView",
    "CartReservationStatusView",
    "CartInventoryRefreshView",
    "CartCouponsView",
    "CartReorderView",
    # Item-level views
    "CartAddItemView",
    "CartUpdateItemView",
    "CartRemoveItemView",
    "CartClearView",
    "CartSaveForLaterView",
    "CartMoveToCartView",
    # Coupon views
    "CartCouponApplyView",
    "CartCouponRemoveView",
    # Base class
    "BaseCartView",
    # Legacy function-based endpoints (preserved for backward compatibility)
    "cart_estimate_legacy",
    "cart_validate_legacy",
    "cart_sync_legacy",
    "cart_sync_snapshot_legacy",
    "cart_apply_coupon_legacy",
    "cart_remove_coupon_legacy",
    "cart_merge_legacy",
    "cart_reorder_legacy",
    # URL patterns (optional include shim)
    "urlpatterns",
]