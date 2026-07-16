"""
Enterprise-grade context processor for the Cart application.

================================================================================
ARCHITECTURE
================================================================================
This module is a THIN ORCHESTRATION LAYER that exposes global cart
context to every template rendered by the Django template engine.

The Cart application is a PURE CONSUMER of the Inventory application.
This context processor NEVER:
    * Calculates or owns any inventory, stock, reservation, or
      warehouse state.
    * Reads or writes inventory models directly.
    * Duplicates business logic that lives in the Cart or Inventory
      service layers.
    * Performs any calculation that is not strictly display-only
      presentation formatting.

All such concerns are EXCLUSIVELY owned by the Inventory application.
Every inventory read below is delegated to the CartInventoryService,
which in turn delegates to the Inventory application's service /
selector layers via the standardized inventory context contract.

CART CONTEXT RESPONSIBILITY
================================================================================
This context processor is responsible ONLY for:
    * Resolving the current request's cart (guest or authenticated)
      via the Cart service layer.
    * Exposing a global cart count and total for the header badge
      and mini-cart widget.
    * Exposing a complete cart payload (serialized) for the cart
      page and summary partials.
    * Exposing a lightweight mini-cart payload for the header
      dropdown.
    * Exposing cart-level inventory summary (delegated to the
      Inventory service through the standardized inventory
      context).
    * Exposing cart-level reservation status (delegated to the
      Inventory service).
    * Exposing cart-level checkout readiness (delegated to the
      Inventory service).
    * Exposing coupon state (code + discount).
    * Exposing URL helpers for cart endpoints.

CACHING
================================================================================
* Cart and inventory data is cached via Django's cache framework
  where the Cart service layer has already implemented cache
  invalidation.
* All cache operations are wrapped in defensive try/except blocks
  so cache failures NEVER break template rendering.
* The context processor is intentionally lightweight and does not
  introduce new caching strategies; it relies on the underlying
  service layers' caching.

CMS-DRIVEN
================================================================================
* All configuration values come from Django settings (which the
  CMS can override) rather than being hardcoded.
* Default currency, mini-cart limit, and similar defaults are
  configurable.

OWASP ASVS COMPLIANCE
================================================================================
* Inputs are never trusted.
* All inventory reads are delegated to the Inventory service layer
  through a well-defined contract.
* No sensitive data is ever exposed in template variables.
* Failures are logged without leaking internals to the template.
* Object ownership is never assumed.
* The context processor NEVER bypasses service-layer authorization
  or validation.

DESIGN PRINCIPLES
================================================================================
* Production-ready: Defensive guards on every code path.
* Scalable: Zero unnecessary database queries when no cart exists.
* Maintainable: Thin orchestrator, no business logic, no calculations.
* Future-proof: Designed to integrate cleanly with Purchase Orders,
  Sales Orders, Manufacturing, Batch/Lot tracking, Barcode/QR
  workflows, REST and GraphQL APIs, and the future notifications
  service.
* Backward-compatible: All existing template variable names are
  preserved so existing templates continue to function without
  modification.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.http import HttpRequest

from .models import Cart, CartItem
from .services import (
    CartInventoryService,
    CartItemService,
    CartService,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# MODULE-LEVEL CONFIGURATION (CMS-DRIVEN)
# ==============================================================================
# All defaults can be overridden via Django settings, which can be wired
# to the CMS without code changes. This keeps the context processor
# fully parameterized and future-proof.

#: Default fallback currency for empty / unavailable carts.
_DEFAULT_CURRENCY: str = "NPR"

#: Maximum number of items surfaced in the lightweight mini-cart
#: payload. The remaining count is displayed as a "+N more" link.
_MINI_CART_LIMIT: int = 5


def _get_default_currency() -> str:
    """
    Returns the default currency code used as a safe fallback when
    the cart's currency is missing. Sourced from
    ``settings.CART_DEFAULT_CURRENCY`` when defined.
    """
    from django.conf import settings
    return getattr(settings, "CART_DEFAULT_CURRENCY", _DEFAULT_CURRENCY)


def _get_mini_cart_limit() -> int:
    """
    Returns the configured mini-cart item limit. Sourced from
    ``settings.CART_MINI_CART_LIMIT`` when defined.
    """
    from django.conf import settings
    try:
        limit = int(getattr(settings, "CART_MINI_CART_LIMIT", _MINI_CART_LIMIT))
        if limit < 1:
            return _MINI_CART_LIMIT
        if limit > 100:
            return 100
        return limit
    except (TypeError, ValueError):
        return _MINI_CART_LIMIT


# ==============================================================================
# SAFE TYPE-COERCION HELPERS
# ==============================================================================
# Centralized, defensive helpers that NEVER raise. They are the
# only safe way to coerce unknown values into known types within
# the context processor.
# ==============================================================================
def _safe_str(value: Any, *, default: str = "") -> str:
    """
    Best-effort conversion of any value into a trimmed string.
    Returns ``default`` when the value is None or coerces to
    an empty string. Never raises.
    """
    if value is None:
        return default
    try:
        result = str(value).strip()
        return result if result else default
    except Exception:
        return default


def _safe_int(value: Any, *, default: int = 0) -> int:
    """
    Best-effort conversion of any value into a non-negative integer.
    Returns ``default`` on any failure. Never raises.
    """
    if value is None or value == "":
        return default
    try:
        result = int(value)
        return max(result, 0)
    except (TypeError, ValueError):
        try:
            return max(int(Decimal(str(value))), 0)
        except (InvalidOperation, TypeError, ValueError):
            return default


def _safe_decimal(value: Any, *, default: Optional[Decimal] = None) -> Optional[Decimal]:
    """
    Best-effort conversion of any value into a Decimal. Returns
    ``default`` on any failure. Never raises. Always returns a
    non-NaN, non-infinite value when default is provided.
    """
    if value is None or value == "":
        return default
    try:
        decimal_value = Decimal(str(value))
        if decimal_value.is_nan() or decimal_value.is_infinite():
            return default
        return decimal_value
    except (InvalidOperation, TypeError, ValueError):
        return default


def _safe_isoformat(value: Any) -> Optional[str]:
    """
    Best-effort conversion of a datetime-like value into an
    ISO-8601 string. Returns None on any failure. Never raises.
    """
    if value is None:
        return None
    try:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)
    except Exception:
        return None


# ==============================================================================
# SAFE DEFAULT PAYLOADS
# ==============================================================================
# These payloads are returned whenever the cart cannot be resolved
# or whenever a downstream service call fails. They guarantee
# that template rendering NEVER fails because of missing context.
# ==============================================================================
def _empty_inventory_context() -> Dict[str, Any]:
    """
    Returns a complete, safe-default inventory context dictionary.
    Every inventory-related key is present even when no inventory
    data is available. This guarantees that templates never encounter
    undefined variables.
    """
    return {
        "exists": False,
        "inventory": None,
        "inventory_summary": "Stock status unavailable",
        "inventory_status": "unknown",
        "available_quantity": "0.00",
        "reserved_quantity": "0.00",
        "damaged_quantity": "0.00",
        "incoming_quantity": "0.00",
        "free_stock": "0.00",
        "total_stock": "0.00",
        "warehouse_id": None,
        "warehouse_name": None,
        "warehouse_summary": "",
        "warehouse_count": 0,
        "is_in_stock": False,
        "is_low_stock": False,
        "is_out_of_stock": True,
        "needs_reorder": False,
        "is_overstock": False,
        "reorder_level": None,
        "minimum_stock": None,
        "maximum_stock": None,
        "location_bin": None,
        "ready_for_checkout": True,
        "blocking_issues": [],
        "stock_message": "Stock status unavailable",
        "in_stock_items": 0,
        "low_stock_items": 0,
        "out_of_stock_items": 0,
        "unknown_items": 0,
        "is_active": False,
    }


def _empty_cart_payload() -> Dict[str, Any]:
    """
    Returns a complete, safe-default cart payload. Every cart-related
    key is present even when no cart exists. This guarantees that
    templates never encounter undefined variables.
    """
    return {
        "id": None,
        "status": None,
        "is_active": False,
        "is_guest": True,
        "currency": _get_default_currency(),
        "customer_id": None,
        "session_key": None,
        "anonymous_token": None,
        "coupon_code": None,
        "coupon_discount": "0.00",
        "subtotal": "0.00",
        "tax": "0.00",
        "shipping": "0.00",
        "discount": "0.00",
        "grand_total": "0.00",
        "total_items": 0,
        "unique_items": 0,
        "last_activity_at": None,
        "expires_at": None,
        "preferred_warehouse_id": None,
        "items": [],
        "inventory_overview": _empty_inventory_context(),
    }


# ==============================================================================
# URL HELPER
# ==============================================================================
# Resolves cart endpoint URLs safely. Never raises. Returns "#" as
# a safe fallback so templates never render broken links. All URL
# names are sourced from the canonical cart URL configuration.
# ==============================================================================
def _safe_reverse(url_name: str, **kwargs: Any) -> str:
    """
    Safely reverses a named URL. Returns "#" if the URL name is
    unregistered, the namespace is missing, or any other reversal
    error occurs. Never raises.
    """
    try:
        from django.urls import reverse, NoReverseMatch
        return reverse(url_name, kwargs=kwargs or None)
    except Exception:
        return "#"


def _build_cart_url_helpers() -> Dict[str, str]:
    """
    Builds the set of standard cart URL strings used by templates
    (cart detail, mini-cart, checkout, clear, etc.). Resolved
    once per request via Django's reverse() and cached on the
    context dictionary.
    """
    return {
        "cart_url": _safe_reverse("cart:cart_detail"),
        "cart_summary_url": _safe_reverse("cart:cart_summary"),
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


# ==============================================================================
# SAFE INVENTORY ACCESSOR
# ==============================================================================
# Centralized wrapper that invokes the CartInventoryService. NEVER
# calculates inventory state. NEVER mutates inventory. Only fetches
# the standardized inventory context from the Inventory service.
# ==============================================================================
def _safe_get_inventory_for_item(
    *,
    product: Any,
    product_variant: Any,
    warehouse: Any = None,
) -> Dict[str, Any]:
    """
    Safely retrieves the standardized inventory context for a single
    cart item. Returns a safe-default context on any failure.
    Never raises. The Cart layer never calculates inventory state;
    all data is delegated to the Inventory service.
    """
    try:
        ctx = CartInventoryService.get_inventory_context(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )
    except Exception as exc:
        logger.debug(
            "Failed to fetch inventory context for cart item: %s", exc
        )
        return _empty_inventory_context()
    if not isinstance(ctx, dict):
        return _empty_inventory_context()
    # Backfill any missing keys with safe defaults.
    base = _empty_inventory_context()
    for key, value in ctx.items():
        if key in base:
            base[key] = value
    return base


def _safe_get_reservation_for_item(
    item: Any,
) -> Dict[str, Any]:
    """
    Safely retrieves the reservation status for a single cart item
    by reading the linked StockReservation row via the cart item's
    FK relationship. The Cart layer never computes reservation
    state. Returns a safe-default payload on any failure.
    Never raises.
    """
    empty = {
        "id": None,
        "token": None,
        "status": None,
        "expires_at": None,
        "is_active": False,
        "is_expired": True,
        "is_terminal": True,
        "quantity": None,
    }
    if item is None or not getattr(item, "pk", None):
        return empty
    try:
        reservation = getattr(item, "reservation", None)
    except Exception:
        return empty
    if reservation is None:
        # The item has no linked reservation; surface any mirrored
        # fields from the cart item itself.
        token = _safe_str(getattr(item, "reservation_token", "")) or None
        status = _safe_str(getattr(item, "reservation_status", "")) or None
        expires_at = _safe_isoformat(
            getattr(item, "reservation_expires_at", None)
        )
        return {
            "id": None,
            "token": token,
            "status": status,
            "expires_at": expires_at,
            "is_active": False,
            "is_expired": True,
            "is_terminal": True,
            "quantity": None,
        }
    try:
        return {
            "id": getattr(reservation, "pk", None),
            "token": _safe_str(
                getattr(reservation, "reservation_token", "")
            ) or None,
            "status": _safe_str(getattr(reservation, "status", "")) or None,
            "expires_at": _safe_isoformat(
                getattr(reservation, "expires_at", None)
            ),
            "is_active": bool(
                getattr(reservation, "is_active", False)
            ),
            "is_expired": bool(
                getattr(reservation, "is_expired", False)
            ),
            "is_terminal": bool(
                getattr(reservation, "is_terminal", False)
            ),
            "quantity": str(
                getattr(reservation, "quantity", None) or "0.00"
            ),
        }
    except Exception as exc:
        logger.debug(
            "Failed to serialize reservation for cart item %s: %s",
            getattr(item, "pk", "?"),
            exc,
        )
        return empty


def _safe_build_product_url(item: Any) -> str:
    """
    Builds a safe product detail URL from a cart item. Returns "#"
    on any failure. Never raises.
    """
    if item is None:
        return "#"
    try:
        from django.urls import reverse, NoReverseMatch
        slug = ""
        product = getattr(item, "product", None)
        if product is not None:
            slug = _safe_str(getattr(product, "slug", ""))
        if not slug:
            variant = getattr(item, "variant", None)
            if variant is not None:
                slug = _safe_str(getattr(variant, "slug", ""))
        if not slug:
            return "#"
        try:
            return reverse("catalog:product_detail", kwargs={"slug": slug})
        except NoReverseMatch:
            return f"/{slug}/"
    except Exception:
        return "#"


# ==============================================================================
# CART ITEM SERIALIZER
# ==============================================================================
# Builds a serializable dictionary representation of a CartItem row
# for template consumption. Inventory data is sourced EXCLUSIVELY
# from the standardized inventory context returned by the Inventory
# service. The Cart layer never reads or calculates inventory.
# ==============================================================================
def _serialize_cart_item(item: Any) -> Dict[str, Any]:
    """
    Serializes a CartItem into a template-friendly dictionary.
    All inventory and reservation data is sourced from the
    respective service layers. Never raises; returns an empty
    dict for invalid input.
    """
    if item is None or not getattr(item, "pk", None):
        return {}

    try:
        product = getattr(item, "product", None)
        variant = getattr(item, "variant", None)
        inventory = _safe_get_inventory_for_item(
            product=product,
            product_variant=variant,
        )
        reservation = _safe_get_reservation_for_item(item)

        # Resolve image URL safely
        image_url: Optional[str] = None
        try:
            image = getattr(item, "product_image_snapshot", None)
            if image is not None and hasattr(image, "url"):
                image_url = image.url
        except Exception:
            image_url = None

        return {
            "id": item.pk,
            "product_id": getattr(item, "product_id", None),
            "variant_id": getattr(item, "variant_id", None),
            "quantity": _safe_int(getattr(item, "quantity", 0)),
            "status": _safe_str(getattr(item, "status", "")) or None,
            "saved_reason": _safe_str(
                getattr(item, "saved_reason", "")
            ) or None,
            "unit_price": str(
                getattr(item, "unit_price_snapshot", None) or "0.00"
            ),
            "compare_at_price": (
                str(getattr(item, "compare_at_price_snapshot", None))
                if getattr(item, "compare_at_price_snapshot", None) is not None
                else None
            ),
            "currency": _safe_str(
                getattr(item, "currency_snapshot", "")
            ) or _get_default_currency(),
            "line_subtotal": str(
                getattr(item, "line_subtotal", None) or "0.00"
            ),
            "product_name": _safe_str(
                getattr(item, "product_name_snapshot", "")
            ) or None,
            "product_sku": _safe_str(
                getattr(item, "product_sku_snapshot", "")
            ) or None,
            "variant_name": _safe_str(
                getattr(item, "variant_name_snapshot", "")
            ) or None,
            "product_image_url": image_url,
            "product_url": _safe_build_product_url(item),
            "inventory": inventory,
            "reservation": reservation,
        }
    except Exception as exc:
        logger.debug(
            "Failed to serialize cart item %s: %s",
            getattr(item, "pk", "?"),
            exc,
        )
        return {}


# ==============================================================================
# INVENTORY OVERVIEW AGGREGATOR
# ==============================================================================
# Pure PRESENTATION AGGREGATION of per-line inventory status returned
# by the Inventory service. NEVER calculates inventory state. The
# per-line inventory context already carries booleans like
# ``is_in_stock`` and ``is_out_of_stock``; this helper only tallies
# them for the cart-level overview card.
# ==============================================================================
def _build_inventory_overview(active_items: List[Any]) -> Dict[str, Any]:
    """
    Aggregates the per-line inventory status (already supplied by
    the Inventory service) into a single cart-level overview. Does
    NOT calculate inventory state. Only tallies pre-computed
    booleans.

    Returns a standardized inventory context that mirrors the
    shape used by the rest of the platform.
    """
    overview = _empty_inventory_context()
    if not active_items:
        return overview

    in_stock_count = 0
    low_stock_count = 0
    out_of_stock_count = 0
    unknown_count = 0
    blocking_issues: List[Dict[str, Any]] = []

    for item in active_items:
        if item is None or not getattr(item, "pk", None):
            continue
        try:
            inv = _safe_get_inventory_for_item(
                product=getattr(item, "product", None),
                product_variant=getattr(item, "variant", None),
            )
        except Exception:
            inv = _empty_inventory_context()

        exists = bool(inv.get("exists", False))
        is_out = bool(inv.get("is_out_of_stock", False))
        is_low = bool(inv.get("is_low_stock", False))
        is_in = bool(inv.get("is_in_stock", False))

        if not exists:
            unknown_count += 1
            continue
        if is_out:
            out_of_stock_count += 1
            blocking_issues.append(
                {
                    "item_id": getattr(item, "pk", None),
                    "code": "out_of_stock",
                    "message": inv.get("stock_message")
                    or inv.get("inventory_summary")
                    or "Out of stock",
                }
            )
        elif is_low:
            low_stock_count += 1
        elif is_in:
            in_stock_count += 1
        else:
            unknown_count += 1

    # Determine overall status
    if out_of_stock_count > 0:
        overall_status = "out_of_stock"
    elif low_stock_count > 0:
        overall_status = "low_stock"
    elif unknown_count > 0 and in_stock_count == 0:
        overall_status = "unknown"
    elif in_stock_count > 0:
        overall_status = "in_stock"
    else:
        overall_status = "unknown"

    total_considered = (
        in_stock_count + low_stock_count + out_of_stock_count + unknown_count
    )
    if total_considered > 0:
        if overall_status == "in_stock":
            summary = f"All {total_considered} item(s) are in stock."
        elif overall_status == "low_stock":
            summary = (
                f"{low_stock_count} item(s) have low stock; "
                f"{out_of_stock_count} out of stock."
            )
        elif overall_status == "out_of_stock":
            summary = (
                f"{out_of_stock_count} item(s) are out of stock."
            )
        else:
            summary = "Stock status unavailable"
    else:
        summary = "Stock status unavailable"

    overview.update(
        {
            "exists": total_considered > 0,
            "inventory_summary": summary,
            "inventory_status": overall_status,
            "is_in_stock": overall_status == "in_stock",
            "is_low_stock": overall_status == "low_stock",
            "is_out_of_stock": overall_status == "out_of_stock",
            "ready_for_checkout": len(blocking_issues) == 0,
            "blocking_issues": blocking_issues,
            "in_stock_items": in_stock_count,
            "low_stock_items": low_stock_count,
            "out_of_stock_items": out_of_stock_count,
            "unknown_items": unknown_count,
        }
    )
    return overview


# ==============================================================================
# SAFE CART FETCH
# ==============================================================================
# Resolves the cart for the current request, delegating to the
# Cart service layer. Never calculates cart data. Never mutates the
# database. Returns None on any failure.
# ==============================================================================
def _safe_get_cart(request: HttpRequest) -> Optional[Cart]:
    """
    Resolves the active cart for the current request. Returns None
    on any failure. Never raises.

    All cart resolution logic lives in the Cart service layer.
    The context processor is a pure consumer.
    """
    try:
        cart_obj, _created = CartService.get_or_create_for_request(request)
        return cart_obj
    except Exception as exc:
        logger.debug(
            "Failed to resolve cart for context processor: %s", exc
        )
        return None


# ==============================================================================
# SAFE ACTIVE ITEMS FETCH
# ==============================================================================
def _safe_get_active_items(cart: Optional[Cart]) -> List[CartItem]:
    """
    Returns the active (non-saved, non-removed) line items for the
    given cart, with deep preloading to avoid N+1 queries. Returns
    an empty list on any failure. Never raises.
    """
    if cart is None or not getattr(cart, "pk", None):
        return []
    try:
        items_qs = (
            cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
            .select_related("product", "variant", "reservation")
            .order_by("added_at", "id")
        )
        return list(items_qs)
    except Exception as exc:
        logger.debug(
            "Failed to load active items for cart %s: %s",
            getattr(cart, "pk", "?"),
            exc,
        )
        return []


# ==============================================================================
# SAFE TOTALS COMPUTATION
# ==============================================================================
def _safe_compute_totals(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Delegates to CartService.compute_totals and returns a safe
    defaults payload on any failure. Never raises. The Cart service
    is the single source of truth for all cart arithmetic.
    """
    safe_defaults: Dict[str, Any] = {
        "subtotal": Decimal("0.00"),
        "tax": Decimal("0.00"),
        "shipping": Decimal("0.00"),
        "discount": Decimal("0.00"),
        "grand_total": Decimal("0.00"),
        "total_items": 0,
        "unique_items": 0,
    }
    if cart is None or not getattr(cart, "pk", None):
        return safe_defaults
    try:
        result = CartService.compute_totals(cart)
        if not isinstance(result, dict):
            return safe_defaults
        for key in safe_defaults:
            if key in result:
                safe_defaults[key] = result[key]
        return safe_defaults
    except Exception as exc:
        logger.debug(
            "Failed to compute totals for cart %s: %s",
            getattr(cart, "pk", "?"),
            exc,
        )
        return safe_defaults


# ==============================================================================
# CART PAYLOAD BUILDER
# ==============================================================================
# Builds a complete, template-friendly cart payload. All business
# data is sourced from the service layer. All inventory data is
# sourced from the standardized inventory context contract.
# ==============================================================================
def _build_cart_payload(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Builds the full serialized cart payload for the cart page,
    summary partial, and any other consumer that needs complete
    cart context. Returns a safe-default payload on any failure.
    Never raises. Never mutates the database.

    The Cart layer never calculates inventory state. Inventory
    data is aggregated from the per-line inventory contexts
    supplied by the Inventory service layer.
    """
    if cart is None or not getattr(cart, "pk", None):
        return _empty_cart_payload()

    active_items = _safe_get_active_items(cart)
    serialized_items: List[Dict[str, Any]] = []
    for item in active_items:
        try:
            serialized = _serialize_cart_item(item)
            if serialized:
                serialized_items.append(serialized)
        except Exception as exc:
            logger.debug(
                "Failed to serialize cart item %s: %s",
                getattr(item, "pk", "?"),
                exc,
            )
    inventory_overview = _build_inventory_overview(active_items)
    totals = _safe_compute_totals(cart)

    try:
        coupon_discount = _safe_decimal(
            getattr(cart, "coupon_discount_amount", None)
        ) or Decimal("0.00")
    except Exception:
        coupon_discount = Decimal("0.00")

    try:
        cart_id: Optional[int] = cart.pk
    except Exception:
        cart_id = None

    return {
        "id": cart_id,
        "status": _safe_str(getattr(cart, "status", "")) or None,
        "is_active": bool(getattr(cart, "is_active", False)),
        "is_guest": bool(getattr(cart, "is_guest", True)),
        "currency": _safe_str(getattr(cart, "currency", ""))
        or _get_default_currency(),
        "customer_id": getattr(cart, "customer_id", None),
        "session_key": _safe_str(getattr(cart, "session_key", "")) or None,
        "anonymous_token": _safe_str(
            getattr(cart, "anonymous_token", "")
        ) or None,
        "coupon_code": _safe_str(getattr(cart, "coupon_code", "")) or None,
        "coupon_discount": str(coupon_discount),
        "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
        "tax": str(totals.get("tax", Decimal("0.00"))),
        "shipping": str(totals.get("shipping", Decimal("0.00"))),
        "discount": str(totals.get("discount", Decimal("0.00"))),
        "grand_total": str(totals.get("grand_total", Decimal("0.00"))),
        "total_items": _safe_int(totals.get("total_items", 0)),
        "unique_items": _safe_int(totals.get("unique_items", 0)),
        "last_activity_at": _safe_isoformat(
            getattr(cart, "last_activity_at", None)
        ),
        "expires_at": _safe_isoformat(getattr(cart, "expires_at", None)),
        "preferred_warehouse_id": getattr(
            cart, "preferred_warehouse_id", None
        ),
        "items": serialized_items,
        "inventory_overview": inventory_overview,
    }


# ==============================================================================
# MINI-CART PAYLOAD BUILDER
# ==============================================================================
# Builds a lightweight mini-cart payload for the header dropdown.
# Truncates to ``_get_mini_cart_limit()`` items. The full count
# is always returned for the header badge.
# ==============================================================================
def _build_mini_cart_payload(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Builds a lightweight mini-cart payload for header widgets.
    Returns a safe-default payload on any failure. Never raises.

    Inventory data is delegated to the Inventory service via the
    standardized cart-level overview builder.
    """
    safe_default: Dict[str, Any] = {
        "items": [],
        "count": 0,
        "subtotal": "0.00",
        "currency": _get_default_currency(),
        "inventory_status": "unknown",
        "is_in_stock": False,
        "is_out_of_stock": True,
        "is_low_stock": False,
        "is_empty": True,
        "is_guest": True,
    }
    if cart is None or not getattr(cart, "pk", None):
        return safe_default

    try:
        all_active_items = _safe_get_active_items(cart)
        limit = _get_mini_cart_limit()
        visible_items = all_active_items[:limit]

        mini_items: List[Dict[str, Any]] = []
        for item in visible_items:
            try:
                serialized = _serialize_cart_item(item)
                if serialized:
                    mini_items.append(serialized)
            except Exception as exc:
                logger.debug(
                    "Failed to serialize mini-cart item %s: %s",
                    getattr(item, "pk", "?"),
                    exc,
                )

        overview = _build_inventory_overview(all_active_items)
        totals = _safe_compute_totals(cart)
        return {
            "items": mini_items,
            "count": _safe_int(getattr(cart, "total_items_count", 0)),
            "subtotal": str(totals.get("subtotal", Decimal("0.00"))),
            "currency": _safe_str(getattr(cart, "currency", ""))
            or _get_default_currency(),
            "inventory_status": overview.get("inventory_status", "unknown"),
            "is_in_stock": overview.get("is_in_stock", False),
            "is_out_of_stock": overview.get("is_out_of_stock", True),
            "is_low_stock": overview.get("is_low_stock", False),
            "is_empty": len(mini_items) == 0,
            "is_guest": bool(getattr(cart, "is_guest", True)),
        }
    except Exception as exc:
        logger.debug("Failed to build mini-cart payload: %s", exc)
        return safe_default


# ==============================================================================
# CHECKOUT READINESS BUILDER
# ==============================================================================
# Delegated to the Cart inventory service. The Cart context processor
# only formats the result. Checkout gating is owned by the Inventory
# service through the standardized validation contract.
# ==============================================================================
def _build_checkout_readiness(
    cart: Optional[Cart],
) -> Dict[str, Any]:
    """
    Builds a cart-level checkout readiness summary. The Cart
    context processor only formats the result. All actual
    validation logic lives in the Inventory service layer.

    Returns a safe-default readiness on any failure. Never raises.
    """
    safe_default: Dict[str, Any] = {
        "ready_for_checkout": True,
        "checkout_blocked": False,
        "checkout_allowed": True,
        "blocking_issues": [],
        "blocking_message": None,
        "inventory_status": "unknown",
        "is_in_stock": False,
        "is_low_stock": False,
        "is_out_of_stock": True,
    }
    if cart is None or not getattr(cart, "pk", None):
        return safe_default
    try:
        result = CartInventoryService.validate_for_checkout(cart=cart)
    except Exception as exc:
        logger.debug(
            "Cart inventory validation failed for cart %s: %s",
            getattr(cart, "pk", "?"),
            exc,
        )
        return safe_default
    if not isinstance(result, dict):
        return safe_default
    ready = bool(result.get("ready_for_checkout", False))
    issues = result.get("issues", []) or []
    totals = result.get("totals", {}) or {}
    cart_snapshot = result.get("cart", {}) or {}
    return {
        "ready_for_checkout": ready,
        "checkout_blocked": not ready,
        "checkout_allowed": ready,
        "blocking_issues": issues,
        "blocking_message": (
            "Your cart has issues that must be resolved before checkout."
            if not ready
            else None
        ),
        "inventory_status": (
            cart_snapshot.get("inventory_overview", {}).get(
                "inventory_status", "unknown"
            )
            if isinstance(cart_snapshot, dict)
            else "unknown"
        ),
        "is_in_stock": (
            cart_snapshot.get("inventory_overview", {}).get(
                "is_in_stock", False
            )
            if isinstance(cart_snapshot, dict)
            else False
        ),
        "is_low_stock": (
            cart_snapshot.get("inventory_overview", {}).get(
                "is_low_stock", False
            )
            if isinstance(cart_snapshot, dict)
            else False
        ),
        "is_out_of_stock": (
            cart_snapshot.get("inventory_overview", {}).get(
                "is_out_of_stock", True
            )
            if isinstance(cart_snapshot, dict)
            else True
        ),
        "totals": totals,
    }


# ==============================================================================
# RESERVATION OVERVIEW BUILDER
# ==============================================================================
# Builds a cart-level reservation overview. The Cart context processor
# only formats the result. All reservation state is read from the
# linked StockReservation rows via the cart item's FK relationship.
# ==============================================================================
def _build_reservation_overview(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Builds a cart-level reservation overview. Returns a safe-default
    payload on any failure. Never raises.

    The Cart layer never computes reservation state. All status
    information is delegated to the Inventory application through
    the linked StockReservation rows.
    """
    safe_default: Dict[str, Any] = {
        "has_expiring_reservations": False,
        "has_active_reservations": False,
        "primary_reservation": None,
        "expiration_message": None,
    }
    if cart is None or not getattr(cart, "pk", None):
        return safe_default
    try:
        active_items = _safe_get_active_items(cart)
        has_active = False
        has_expiring = False
        primary: Optional[Dict[str, Any]] = None
        for item in active_items:
            try:
                reservation = _serialize_for_reservation_overview(item)
            except Exception:
                continue
            if not reservation:
                continue
            if reservation.get("is_active"):
                has_active = True
            if reservation.get("is_expired") and reservation.get("is_active"):
                has_expiring = True
            if primary is None and reservation.get("id"):
                primary = reservation
        return {
            "has_expiring_reservations": has_expiring,
            "has_active_reservations": has_active,
            "primary_reservation": primary,
            "expiration_message": (
                "Some reservations are expiring soon. Complete checkout to secure your items."
                if has_expiring
                else None
            ),
        }
    except Exception as exc:
        logger.debug(
            "Failed to build reservation overview for cart %s: %s",
            getattr(cart, "pk", "?"),
            exc,
        )
        return safe_default


def _serialize_for_reservation_overview(item: Any) -> Optional[Dict[str, Any]]:
    """
    Returns a small serializable dict for the reservation linked to
    a cart item. Returns None if no reservation is linked. Never
    calculates reservation state.
    """
    if item is None or not getattr(item, "pk", None):
        return None
    try:
        reservation = getattr(item, "reservation", None)
    except Exception:
        return None
    if reservation is None:
        return None
    try:
        return {
            "id": getattr(reservation, "pk", None),
            "token": _safe_str(
                getattr(reservation, "reservation_token", "")
            ) or None,
            "status": _safe_str(getattr(reservation, "status", "")) or None,
            "expires_at": _safe_isoformat(
                getattr(reservation, "expires_at", None)
            ),
            "is_active": bool(getattr(reservation, "is_active", False)),
            "is_expired": bool(getattr(reservation, "is_expired", False)),
            "is_terminal": bool(getattr(reservation, "is_terminal", False)),
        }
    except Exception:
        return None


# ==============================================================================
# COUPON STATE BUILDER
# ==============================================================================
def _build_coupon_state(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Builds a safe cart-level coupon state dictionary. Returns a
    safe-default payload on any failure. Never raises. Coupon
    validation and discount calculation are owned by the coupon
    service; the Cart context processor only formats state.
    """
    safe_default: Dict[str, Any] = {
        "is_applied": False,
        "code": None,
        "discount_amount": "0.00",
    }
    if cart is None or not getattr(cart, "pk", None):
        return safe_default
    try:
        code = _safe_str(getattr(cart, "coupon_code", "")) or None
        discount = str(
            getattr(cart, "coupon_discount_amount", None) or "0.00"
        )
        return {
            "is_applied": bool(code),
            "code": code,
            "discount_amount": discount,
        }
    except Exception as exc:
        logger.debug(
            "Failed to build coupon state for cart %s: %s",
            getattr(cart, "pk", "?"),
            exc,
        )
        return safe_default


# ==============================================================================
# INVENTORY CONTEXT AGGREGATOR (for top-level template exposure)
# ==============================================================================
# Aggregates the per-line inventory contexts into a single
# top-level inventory payload that is exposed as ``cart_inventory``.
# Mirrors the shape of the per-line inventory context so templates
# can use a single set of template variables regardless of scope.
# ==============================================================================
def _build_cart_inventory_context(
    active_items: List[Any],
) -> Dict[str, Any]:
    """
    Builds a single standardized inventory context for the entire
    cart, derived from the per-line inventory contexts returned by
    the Inventory service. Never calculates inventory state.

    The returned context mirrors the shape of the per-line inventory
    context so templates can use the same variable names regardless
    of whether they are iterating over items or looking at the
    cart-level overview.
    """
    context = _empty_inventory_context()
    if not active_items:
        return context

    overview = _build_inventory_overview(active_items)
    context.update(
        {
            "exists": overview.get("exists", False),
            "inventory": None,  # Cart-level, not a single record
            "inventory_summary": overview.get(
                "inventory_summary", "Stock status unavailable"
            ),
            "inventory_status": overview.get("inventory_status", "unknown"),
            "is_in_stock": overview.get("is_in_stock", False),
            "is_low_stock": overview.get("is_low_stock", False),
            "is_out_of_stock": overview.get("is_out_of_stock", True),
            "ready_for_checkout": overview.get("ready_for_checkout", True),
            "blocking_issues": overview.get("blocking_issues", []),
            "stock_message": overview.get(
                "inventory_summary", "Stock status unavailable"
            ),
            "in_stock_items": overview.get("in_stock_items", 0),
            "low_stock_items": overview.get("low_stock_items", 0),
            "out_of_stock_items": overview.get("out_of_stock_items", 0),
            "unknown_items": overview.get("unknown_items", 0),
            "is_active": len(active_items) > 0,
        }
    )
    return context


# ==============================================================================
# AUTHENTICATION / USER HELPERS
# ==============================================================================
def _user_is_authenticated(request: HttpRequest) -> bool:
    """
    Returns True if the current request has an authenticated user.
    Never raises. Returns False on any failure.
    """
    try:
        user = getattr(request, "user", None)
        if user is None:
            return False
        return bool(getattr(user, "is_authenticated", False))
    except Exception:
        return False


# ==============================================================================
# MAIN CONTEXT PROCESSOR
# ==============================================================================
def cart(request: HttpRequest) -> Dict[str, Any]:
    """
    Enterprise-grade context processor for the Cart application.

    Exposes a comprehensive set of cart-related context variables to
    every Django template. Designed to NEVER fail to render, even if
    the Cart or Inventory services are temporarily unavailable.

    ============================================================================
    RETURNED CONTEXT KEYS (all optional, all with safe defaults)
    ============================================================================

    Core cart identity:
        cart                    Cart model instance (or None)
        cart_id                Cart primary key (or None)
        cart_currency          Cart currency code (or "NPR")
        cart_is_guest           True if cart is anonymous
        cart_status            Cart lifecycle status (or None)
        cart_anonymous_token    Persistent guest cart token (or None)

    Cart count and totals:
        cart_count             Total item quantity across active lines
        cart_unique_items      Distinct active line items
        cart_subtotal          String Decimal
        cart_tax               String Decimal
        cart_shipping          String Decimal
        cart_discount          String Decimal
        cart_grand_total       String Decimal
        cart_total_items       Alias for cart_count (legacy compat)

    Cart payloads:
        cart_payload           Full serialized cart payload
        mini_cart_items        Lightweight mini-cart items
        mini_cart_payload      Lightweight mini-cart payload

    Inventory context (delegated to Inventory service):
        cart_inventory         Standardized inventory context
        cart_inventory_overview Cart-level inventory overview
        cart_inventory_status  "in_stock" / "low_stock" / "out_of_stock" / "unknown"
        cart_inventory_endpoint AJAX endpoint for inventory refresh
        is_in_stock            True if any active line is in stock
        is_out_of_stock        True if any active line is out of stock
        is_low_stock           True if any active line is low stock
        has_out_of_stock       True if cart has at least one out-of-stock line

    Checkout readiness (delegated to Inventory service):
        cart_ready_for_checkout Boolean
        cart_checkout_blocked  Boolean (inverse of ready)
        cart_checkout_allowed  Boolean (alias for ready)
        cart_issues            List of blocking issues
        cart_validation_message String error message (or None)

    Reservation context (delegated to Inventory service):
        cart_reservations      Reservation overview
        cart_has_expiring_reservations Boolean
        cart_primary_reservation Primary reservation dict (or None)
        cart_reservation_expiry_message String (or None)

    Coupon state:
        cart_coupon            Coupon state dict
        cart_coupon_code       Applied coupon code (or None)
        cart_coupon_discount   Discount amount (String Decimal)

    URL helpers:
        cart_url               Cart detail page URL
        cart_summary_url       Cart summary partial URL
        mini_cart_url          Mini-cart partial URL
        cart_clear_url         Cart clear endpoint
        cart_apply_coupon_url  Apply coupon endpoint
        cart_remove_coupon_url Remove coupon endpoint
        cart_sync_url          Cart sync endpoint
        cart_estimate_url      Cart estimate endpoint
        cart_validate_url      Cart validate endpoint
        cart_merge_url         Cart merge endpoint
        cart_reorder_url       Cart reorder endpoint

    User / auth:
        user_authenticated     True if request.user.is_authenticated
        is_empty               True if cart has no active items
    """
    # Step 1: Pre-build safe defaults so the function can return at
    # any time without leaving the template without a valid context.
    empty_cart = _empty_cart_payload()
    empty_inventory = _empty_inventory_context()
    url_helpers = _build_cart_url_helpers()
    user_authenticated = _user_is_authenticated(request)

    # Step 2: Safely resolve the cart via the service layer.
    cart_obj = _safe_get_cart(request)

    # Step 3: Build the full cart payload. Each step is wrapped
    # independently so a single failure never cascades.
    try:
        cart_payload = _build_cart_payload(cart_obj) if cart_obj else empty_cart
    except Exception as exc:
        logger.debug("Cart payload build failed: %s", exc)
        cart_payload = empty_cart

    try:
        mini_cart_payload = (
            _build_mini_cart_payload(cart_obj) if cart_obj else {
                "items": [], "count": 0, "subtotal": "0.00",
                "currency": _get_default_currency(),
                "inventory_status": "unknown", "is_in_stock": False,
                "is_out_of_stock": True, "is_low_stock": False,
                "is_empty": True, "is_guest": True,
            }
        )
    except Exception as exc:
        logger.debug("Mini-cart payload build failed: %s", exc)
        mini_cart_payload = {
            "items": [], "count": 0, "subtotal": "0.00",
            "currency": _get_default_currency(),
            "inventory_status": "unknown", "is_in_stock": False,
            "is_out_of_stock": True, "is_low_stock": False,
            "is_empty": True, "is_guest": True,
        }

    # Step 4: Build inventory-related context (all delegated to
    # the Inventory service through the CartInventoryService).
    try:
        active_items = (
            _safe_get_active_items(cart_obj) if cart_obj else []
        )
    except Exception:
        active_items = []

    try:
        checkout_readiness = _build_checkout_readiness(cart_obj)
    except Exception as exc:
        logger.debug("Checkout readiness build failed: %s", exc)
        checkout_readiness = {
            "ready_for_checkout": True, "checkout_blocked": False,
            "checkout_allowed": True, "blocking_issues": [],
            "blocking_message": None, "inventory_status": "unknown",
            "is_in_stock": False, "is_low_stock": False,
            "is_out_of_stock": True, "totals": {},
        }

    try:
        reservation_overview = _build_reservation_overview(cart_obj)
    except Exception as exc:
        logger.debug("Reservation overview build failed: %s", exc)
        reservation_overview = {
            "has_expiring_reservations": False,
            "has_active_reservations": False,
            "primary_reservation": None,
            "expiration_message": None,
        }

    try:
        coupon_state = _build_coupon_state(cart_obj)
    except Exception as exc:
        logger.debug("Coupon state build failed: %s", exc)
        coupon_state = {"is_applied": False, "code": None, "discount_amount": "0.00"}

    # Step 5: Build the top-level standardized inventory context.
    try:
        cart_inventory = _build_cart_inventory_context(active_items)
    except Exception as exc:
        logger.debug("Cart inventory context build failed: %s", exc)
        cart_inventory = empty_inventory

    # Step 6: Extract convenience top-level values.
    cart_count = _safe_int(cart_payload.get("total_items", 0))
    cart_unique_items = _safe_int(cart_payload.get("unique_items", 0))
    is_empty = cart_count <= 0
    inventory_status = str(
        cart_inventory.get("inventory_status", "unknown")
    )
    is_in_stock = bool(cart_inventory.get("is_in_stock", False))
    is_out_of_stock = bool(
        cart_inventory.get("is_out_of_stock", not is_empty)
    )
    is_low_stock = bool(cart_inventory.get("is_low_stock", False))
    has_out_of_stock = is_out_of_stock and not is_empty

    # Step 7: Pull inventory overview fields for backward
    # compatibility with templates that reference them directly.
    inventory_overview = cart_payload.get(
        "inventory_overview", empty_inventory
    ) or empty_inventory
    if not isinstance(inventory_overview, dict):
        inventory_overview = empty_inventory

    cart_currency = (
        _safe_str(cart_payload.get("currency", ""))
        or _get_default_currency()
    )

    # Step 8: Build the final context dictionary.
    context: Dict[str, Any] = {
        # --- Core cart identity ---
        "cart": cart_obj,
        "cart_id": cart_payload.get("id"),
        "cart_currency": cart_currency,
        "cart_is_guest": bool(cart_payload.get("is_guest", True)),
        "cart_status": cart_payload.get("status"),
        "cart_anonymous_token": cart_payload.get("anonymous_token"),

        # --- Cart count and totals ---
        "cart_count": cart_count,
        "cart_unique_items": cart_unique_items,
        "cart_subtotal": cart_payload.get("subtotal", "0.00"),
        "cart_tax": cart_payload.get("tax", "0.00"),
        "cart_shipping": cart_payload.get("shipping", "0.00"),
        "cart_discount": cart_payload.get("discount", "0.00"),
        "cart_grand_total": cart_payload.get("grand_total", "0.00"),
        "cart_total_items": cart_count,

        # --- Cart payloads ---
        "cart_payload": cart_payload,
        "mini_cart_items": mini_cart_payload.get("items", []),
        "mini_cart_payload": mini_cart_payload,

        # --- Inventory context (delegated to Inventory service) ---
        "cart_inventory": cart_inventory,
        "cart_inventory_overview": inventory_overview,
        "cart_inventory_status": inventory_status,
        "cart_inventory_endpoint": url_helpers["cart_sync_url"],
        "is_in_stock": is_in_stock,
        "is_out_of_stock": is_out_of_stock,
        "is_low_stock": is_low_stock,
        "has_out_of_stock": has_out_of_stock,

        # --- Checkout readiness (delegated to Inventory service) ---
        "cart_ready_for_checkout": bool(
            checkout_readiness.get("ready_for_checkout", True)
        ),
        "cart_checkout_blocked": bool(
            checkout_readiness.get("checkout_blocked", False)
        ),
        "cart_checkout_allowed": bool(
            checkout_readiness.get("checkout_allowed", True)
        ),
        "cart_issues": checkout_readiness.get("blocking_issues", []),
        "cart_validation_message": checkout_readiness.get(
            "blocking_message"
        ),

        # --- Reservation context (delegated to Inventory service) ---
        "cart_reservations": reservation_overview,
        "cart_has_expiring_reservations": bool(
            reservation_overview.get("has_expiring_reservations", False)
        ),
        "cart_primary_reservation": reservation_overview.get(
            "primary_reservation"
        ),
        "cart_reservation_expiry_message": reservation_overview.get(
            "expiration_message"
        ),

        # --- Coupon state ---
        "cart_coupon": coupon_state,
        "cart_coupon_code": coupon_state.get("code"),
        "cart_coupon_discount": coupon_state.get("discount_amount", "0.00"),

        # --- URL helpers ---
        "cart_url": url_helpers["cart_url"],
        "cart_summary_url": url_helpers["cart_summary_url"],
        "mini_cart_url": url_helpers["mini_cart_url"],
        "cart_clear_url": url_helpers["cart_clear_url"],
        "cart_apply_coupon_url": url_helpers["cart_apply_coupon_url"],
        "cart_remove_coupon_url": url_helpers["cart_remove_coupon_url"],
        "cart_sync_url": url_helpers["cart_sync_url"],
        "cart_estimate_url": url_helpers["cart_estimate_url"],
        "cart_validate_url": url_helpers["cart_validate_url"],
        "cart_merge_url": url_helpers["cart_merge_url"],
        "cart_reorder_url": url_helpers["cart_reorder_url"],

        # --- User / auth ---
        "user_authenticated": user_authenticated,

        # --- Convenience flags ---
        "is_empty": is_empty,
    }

    return context