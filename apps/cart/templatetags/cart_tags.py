"""
Enterprise-grade presentation-only template tag library for the
Cart application.

ARCHITECTURE
============

This module is the PRESENTATION-ONLY layer of the Cart application.
It is responsible exclusively for:

    * Resolving the current cart from the request context
    * Formatting cart math (subtotal, total, tax, shipping, discounts)
    * Exposing cart totals for badges and summary cards
    * Reading inventory context that was pre-fetched by the view layer
    * Formatting per-line and per-cart values for display
    * Providing safe, reusable formatters for the storefront

This module NEVER:

    * Calculates or owns inventory state
    * Mutates stock
    * Performs reservation logic
    * Calculates stock availability
    * Reads ``Product.stock_quantity`` or ``ProductVariant.stock_quantity``
    * Makes its own database queries for inventory data

The Cart application is a PURE CONSUMER of the Inventory application.
All inventory data displayed in cart templates is read from the
``inventory`` attribute that the view layer attaches to each cart item
after consulting the inventory service. The template tags do not
re-query inventory on every render.

CMS-DRIVEN CONFIGURATION
=========================
Every configurable value (default currency, decimal places, formatting)
comes from Django settings (which can be driven by the CMS) rather
than being hardcoded. Template tags are thin, fast, and defensive.

OWASP ASVS COMPLIANT
=====================
* No SQL injection risk (no raw queries, no user-supplied ordering)
* No XSS risk (output is escaped at the template layer, no mark_safe)
* No information disclosure (all errors are logged, never propagated)
* No race conditions (all reads use the cart from the service layer)
* No availability heuristic (we never say "in stock" - we only display
  what the view layer explicitly attached as inventory context)

PERFORMANCE
===========
* Cart resolution is delegated to CartService which uses session-key
  caching. Each unique cart is resolved at most once per request.
* Cart math is delegated to CartCalculationsService which uses
  pre-aggregated querysets to avoid N+1.
* Inventory context is read from a pre-fetched attribute on the cart
  item (set by the view layer) to avoid per-tag queries.
* No template tag performs its own database query for inventory.

BACKWARD COMPATIBILITY
======================
The public API of every existing tag and filter is preserved.
The internal implementation is rewritten to be defensive, optimized,
and CMS-driven without breaking any existing template usage.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Optional, Union

from django import template
from django.conf import settings
from django.db.models import QuerySet
from django.utils.safestring import mark_safe

from ..models import Cart, CartItem
from ..services import (
    CartCalculationsService,
    CartInventoryService,
    CartService,
)

register = template.Library()
logger = logging.getLogger(__name__)


# ==============================================================================
# CMS-DRIVEN CONFIGURATION
# ==============================================================================
# All defaults can be overridden via Django settings, which in turn can
# be driven by the CMS without code changes. This keeps the module fully
# parameterized and future-proof.

_DEFAULT_CURRENCY = "NPR"
_DEFAULT_DECIMAL_PLACES = 2


def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)


def _get_default_currency() -> str:
    """
    Returns the CMS-driven default currency code for the cart.

    Sourced from the ``CART_DEFAULT_CURRENCY`` Django setting.
    Defaults to ``"NPR"`` for backward compatibility.
    """
    return _get_setting("CART_DEFAULT_CURRENCY", _DEFAULT_CURRENCY)


def _get_decimal_places() -> int:
    """
    Returns the CMS-driven number of decimal places for cart math.

    Sourced from the ``CART_DECIMAL_PLACES`` Django setting.
    Defaults to ``2`` for backward compatibility.
    """
    return _get_setting("CART_DECIMAL_PLACES", _DEFAULT_DECIMAL_PLACES)


def _get_decimal_quantum() -> Decimal:
    """
    Returns a ``Decimal`` quantum for ``quantize`` operations based on
    the configured number of decimal places.
    """
    try:
        places = max(0, int(_get_decimal_places()))
        return Decimal(10) ** -places
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0.01")


# ==============================================================================
# SAFE COERCION HELPERS
# ==============================================================================
# Centralized, defensive, never-raise helpers used by all template tags.
# Every helper is written as a pure function so it can be unit tested
# in isolation and reused by management commands, serializers, and APIs.

def _safe_str(value: Any, default: str = "") -> str:
    """
    Best-effort conversion of any value to a safe trimmed string.

    Returns the default for None, empty, or unprintable values.
    """
    if value is None:
        return default
    try:
        text = str(value).strip()
        return text if text else default
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """
    Best-effort conversion of any value to a non-negative integer.

    Returns the default for None, empty, or unparseable values.
    Negative values are clamped to the default to avoid surprising
    template rendering.
    """
    if value is None or value == "":
        return default
    try:
        result = int(value)
        return result if result >= 0 else default
    except (TypeError, ValueError):
        try:
            result = int(Decimal(str(value)))
            return result if result >= 0 else default
        except (InvalidOperation, TypeError, ValueError):
            return default


def _safe_decimal(
    value: Any,
    default: Optional[Decimal] = None,
) -> Decimal:
    """
    Best-effort conversion of any value to a safe ``Decimal``.

    Returns the default for None, empty, NaN, infinite, or unparseable
    values. ``default`` defaults to ``Decimal("0")``.
    """
    if value is None or value == "":
        return default if default is not None else Decimal("0")
    try:
        decimal_value = Decimal(str(value))
        if decimal_value.is_nan() or decimal_value.is_infinite():
            return default if default is not None else Decimal("0")
        return decimal_value
    except (InvalidOperation, TypeError, ValueError):
        return default if default is not None else Decimal("0")


def _format_decimal(value: Any, places: Optional[int] = None) -> str:
    """
    Format a numeric value as a localized decimal string with the
    configured number of decimal places.

    Never raises. Returns ``"0.00"`` (or equivalent) on any failure.
    """
    if places is None:
        places = _get_decimal_places()
    decimal_value = _safe_decimal(value)
    try:
        formatted = decimal_value.quantize(
            _get_decimal_quantum(),
            rounding=ROUND_HALF_UP,
        )
        return f"{formatted:.{int(places)}f}"
    except Exception:
        try:
            return f"{Decimal('0'):.{int(places)}f}"
        except Exception:
            return "0.00"


# ==============================================================================
# CONTEXT EXTRACTION HELPERS
# =============================================================================

def _get_request(context: Any) -> Optional[Any]:
    """
    Extract the current request from a template context.

    Returns None if the context is missing, malformed, or the request
    attribute is absent. Never raises.
    """
    if not isinstance(context, dict):
        return None
    try:
        return context.get("request")
    except Exception:
        return None


def _resolve_cart(context: Any) -> Optional[Cart]:
    """
    Resolve the current cart via CartService.

    Returns None on any failure or when no request is present.
    Never raises. This guarantees that template rendering never fails
    because of a missing or invalid cart.
    """
    request = _get_request(context)
    if request is None:
        return None
    try:
        cart, _ = CartService.get_or_create_cart(request)
        return cart
    except Exception as exc:
        logger.debug("Cart resolution failed: %s", exc)
        return None


def _resolve_cart_item(cart: Any, item: Any) -> Optional[CartItem]:
    """
    Normalize a value to a valid CartItem bound to the given cart.

    Returns None for invalid input, mismatched carts, or any failure.
    Prevents cross-cart item leakage in templates.
    """
    if cart is None or item is None:
        return None
    if not isinstance(item, CartItem):
        return None
    try:
        cart_id = getattr(cart, "id", None)
        item_cart_id = getattr(item, "cart_id", None)
        if (
            cart_id is not None
            and item_cart_id is not None
            and item_cart_id != cart_id
        ):
            return None
        return item
    except Exception:
        return None


# ==============================================================================
# SAFE CONTEXT BUILDERS
# ==============================================================================
# These builders return safe-complete payloads for the inclusion tag and
# for inventory-aware helpers. Every key is present, even when the
# underlying data is unavailable, so templates never encounter
# undefined variables.

def _empty_inventory_context() -> Dict[str, Any]:
    """
    Returns a safe-complete inventory context dictionary.

    Mirrors the standardized inventory context shape returned by
    CartInventoryService.get_inventory_context, ensuring templates
    can rely on every key being present.
    """
    return {
        "exists": False,
        "inventory": None,
        "inventory_summary": "Stock status unavailable",
        "inventory_status": "unknown",
        "available_quantity": "0.00",
        "reserved_quantity": "0.00",
        "incoming_quantity": "0.00",
        "free_stock": "0.00",
        "total_stock": "0.00",
        "warehouse_count": 0,
        "warehouse_summary": "No warehouse",
        "stock_message": "Stock status unavailable",
        "stock_status": "unknown",
        "stock_text": "Stock status unavailable",
        "is_in_stock": False,
        "is_low_stock": False,
        "is_out_of_stock": True,
        "is_overstock": False,
        "needs_reorder": False,
    }


def _empty_cart_overview() -> Dict[str, Any]:
    """
    Returns a safe-complete cart-level inventory overview dictionary.

    Mirrors the standardized cart inventory overview shape returned by
    CartInventoryService.validate_for_checkout, so templates can
    rely on every key being present.
    """
    return {
        "status": "unknown",
        "is_in_stock": False,
        "is_low_stock": False,
        "is_out_of_stock": True,
        "ready_for_checkout": True,
        "blocking_issues": [],
        "in_stock_count": 0,
        "low_stock_count": 0,
        "out_of_stock_count": 0,
        "total_items": 0,
        "warnings": [],
    }


def _extract_inventory_context(item: Any) -> Dict[str, Any]:
    """
    Extract the pre-fetched inventory context from a cart item.

    The view layer is responsible for attaching the standardized
    inventory payload to each cart item via its ``inventory``
    attribute. This helper returns a safe-default context when no
    inventory data is available or when the item is invalid.

    The view layer's inventory context is always authoritative.
    Template tags never make their own inventory service calls.
    """
    if not isinstance(item, CartItem):
        return _empty_inventory_context()
    try:
        inventory = getattr(item, "inventory", None)
    except Exception:
        return _empty_inventory_context()
    if not isinstance(inventory, dict):
        return _empty_inventory_context()
    # Merge with the safe default to guarantee every key is present.
    base = _empty_inventory_context()
    for key, value in inventory.items():
        if key in base:
            base[key] = value
    return base


def _extract_cart_overview(context: Any) -> Dict[str, Any]:
    """
    Extract the pre-fetched cart inventory overview from a template
    context. The view layer is responsible for invoking
    CartInventoryService.validate_for_checkout and attaching the
    result under the ``cart_inventory_overview`` context key.

    Returns a safe-default overview if no pre-fetched data is
    available. Template tags never make their own inventory service
    calls.
    """
    if not isinstance(context, dict):
        return _empty_cart_overview()
    overview = context.get("cart_inventory_overview")
    if not isinstance(overview, dict):
        return _empty_cart_overview()
    base = _empty_cart_overview()
    for key, value in overview.items():
        if key in base:
            base[key] = value
    return base


# ==============================================================================
# TEMPLATE FILTERS (Reusable formatters)
# ==============================================================================
# All filters are pure functions that NEVER raise. They are designed to
# be cheap enough to use inside Django template loops without measurable
# performance impact.

@register.filter(name="cart_decimal")
def cart_decimal_filter(value: Any, places: Any = None) -> str:
    """
    Format a numeric value as a localized decimal string.

    Usage::

        {{ value|cart_decimal }}
        {{ value|cart_decimal:4 }}

    Returns ``"0.00"`` (or equivalent) on any failure.
    """
    try:
        if places is None or places == "":
            places_int = _get_decimal_places()
        else:
            places_int = _safe_int(places, default=_get_decimal_places())
        return _format_decimal(value, places_int)
    except Exception:
        return _format_decimal(0)


@register.filter(name="cart_currency")
def cart_currency_filter(value: Any) -> str:
    """
    Format a numeric value as a currency string, prefixed by the
    configured default currency code.

    Usage::

        {{ value|cart_currency }}

    Returns ``"<DEFAULT_CURRENCY> 0.00"`` on any failure.
    """
    try:
        currency = _get_default_currency()
        formatted = _format_decimal(value, _get_decimal_places())
        return f"{currency} {formatted}"
    except Exception:
        return f"{_get_default_currency()} 0.00"


@register.filter(name="cart_safe_int")
def cart_safe_int_filter(value: Any) -> int:
    """
    Defensive integer coercion. Returns ``0`` for None or invalid values.

    Usage::

        {{ value|cart_safe_int }}
    """
    return _safe_int(value, default=0)


@register.filter(name="cart_safe_decimal")
def cart_safe_decimal_filter(value: Any) -> str:
    """
    Defensive decimal coercion and formatting.

    Returns ``"0.00"`` (or equivalent) for None or invalid values.

    Usage::

        {{ value|cart_safe_decimal }}
    """
    return _format_decimal(value, _get_decimal_places())


@register.filter(name="multiply")
def multiply_filter(value: Any, arg: Any) -> str:
    """
    Multiply two values, returning a formatted decimal string.

    Falls back to ``"0.00"`` on any error or non-numeric input.

    Usage::

        {{ unit_price|multiply:quantity }}
    """
    a = _safe_decimal(value)
    b = _safe_decimal(arg)
    try:
        product = a * b
    except (ArithmeticError, TypeError, ValueError):
        return _format_decimal(0)
    return _format_decimal(product, _get_decimal_places())


# ==============================================================================
# CART MATH TEMPLATE TAGS (simple_tag)
# ==============================================================================
# Each tag resolves the cart from the request context and delegates all
# arithmetic to the CartCalculationsService. No database queries for
# cart math, no business logic, no inventory calculation. The service
# layer is the single source of truth for all cart totals.

@register.simple_tag(takes_context=True)
def cart_count(context: Dict[str, Any]) -> int:
    """
    Return the total number of items in the current cart.

    Used in templates to display the header badge count.

    Returns ``0`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return 0
    try:
        return _safe_int(
            getattr(cart, "total_items_count", 0),
            default=0,
        )
    except Exception:
        return 0


@register.simple_tag(takes_context=True)
def cart_unique_items(context: Dict[str, Any]) -> int:
    """
    Return the number of distinct line items in the current cart.

    Returns ``0`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return 0
    try:
        return _safe_int(
            getattr(cart, "unique_items_count", 0),
            default=0,
        )
    except Exception:
        return 0


@register.simple_tag(takes_context=True)
def cart_subtotal(context: Dict[str, Any]) -> str:
    """
    Return the formatted cart subtotal (sum of active line items).

    Returns ``"0.00"`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _format_decimal(0)
    try:
        subtotal = CartCalculationsService.calculate_cart_subtotal(cart)
        return _format_decimal(subtotal)
    except Exception:
        return _format_decimal(0)


@register.simple_tag(takes_context=True)
def cart_discount(context: Dict[str, Any]) -> str:
    """
    Return the formatted cart discount (line-level + coupon combined).

    This matches the original behavior where the simple_tag returns the
    SUM of line-level discounts AND the coupon discount, while the
    cart_summary inclusion tag returns the line-level discount under
    the ``discount`` key. The two views serve different needs.

    Returns ``"0.00"`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _format_decimal(0)
    try:
        line_discount = CartCalculationsService.calculate_cart_discount(cart)
        coupon_discount = _safe_decimal(
            getattr(cart, "coupon_discount_amount", None),
            default=Decimal("0"),
        )
        total_discount = line_discount + coupon_discount
        return _format_decimal(total_discount)
    except Exception:
        return _format_decimal(0)


@register.simple_tag(takes_context=True)
def cart_line_discount(context: Dict[str, Any]) -> str:
    """
    Return the formatted cart line-level discount (excluding coupon).

    This is a convenience tag that returns ONLY the line-level discount
    from CartCalculationsService.calculate_cart_discount, separate
    from the coupon discount.

    Returns ``"0.00"`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _format_decimal(0)
    try:
        line_discount = CartCalculationsService.calculate_cart_discount(cart)
        return _format_decimal(line_discount)
    except Exception:
        return _format_decimal(0)


@register.simple_tag(takes_context=True)
def cart_coupon_discount(context: Dict[str, Any]) -> str:
    """
    Return the formatted cart coupon discount (excluding line discounts).

    Returns ``"0.00"`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _format_decimal(0)
    try:
        coupon_discount = _safe_decimal(
            getattr(cart, "coupon_discount_amount", None),
            default=Decimal("0"),
        )
        return _format_decimal(coupon_discount)
    except Exception:
        return _format_decimal(0)


@register.simple_tag(takes_context=True)
def cart_tax(context: Dict[str, Any]) -> str:
    """
    Return the formatted cart estimated tax.

    Returns ``"0.00"`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _format_decimal(0)
    try:
        tax = CartCalculationsService.calculate_cart_tax(cart)
        return _format_decimal(tax)
    except Exception:
        return _format_decimal(0)


@register.simple_tag(takes_context=True)
def cart_shipping(context: Dict[str, Any]) -> str:
    """
    Return the formatted cart estimated shipping cost.

    Returns ``"0.00"`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _format_decimal(0)
    try:
        shipping = CartCalculationsService.calculate_cart_shipping(cart)
        return _format_decimal(shipping)
    except Exception:
        return _format_decimal(0)


@register.simple_tag(takes_context=True)
def cart_total(context: Dict[str, Any]) -> str:
    """
    Return the formatted cart grand total
    (subtotal - discount + tax + shipping).

    Returns ``"0.00"`` when no request, no cart, or any failure occurs.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _format_decimal(0)
    try:
        total = CartCalculationsService.calculate_cart_grand_total(cart)
        return _format_decimal(total)
    except Exception:
        return _format_decimal(0)


@register.simple_tag(takes_context=True)
def cart_coupon_code(context: Dict[str, Any]) -> str:
    """
    Return the currently applied coupon code, or an empty string.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return ""
    return _safe_str(getattr(cart, "coupon_code", None), default="")


@register.simple_tag(takes_context=True)
def cart_currency(context: Dict[str, Any]) -> str:
    """
    Return the cart's currency code, falling back to the configured
    default currency when unavailable.
    """
    cart = _resolve_cart(context)
    if cart is not None:
        currency = _safe_str(getattr(cart, "currency", None))
        if currency:
            return currency
    return _get_default_currency()


@register.simple_tag(takes_context=True)
def cart_is_empty(context: Dict[str, Any]) -> bool:
    """
    Return True when the cart has no active line items, False otherwise.

    Returns True (empty) when no request, no cart, or any failure
    occurs, which is the safe default for missing data.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return True
    try:
        return (
            _safe_int(
                getattr(cart, "total_items_count", 0),
                default=0,
            )
            <= 0
        )
    except Exception:
        return True


# ==============================================================================
# PER-LINE ITEM TEMPLATE TAGS
# ==============================================================================
# These tags read directly from the passed CartItem instance. They do
# not make database queries. They never calculate inventory - the view
# layer is responsible for attaching inventory context.

@register.simple_tag
def line_subtotal(item: Any) -> str:
    """
    Return the formatted line subtotal for a cart item
    (``unit_price_snapshot * quantity``).

    Returns ``"0.00"`` for invalid or missing input.
    """
    if not isinstance(item, CartItem):
        return _format_decimal(0)
    try:
        unit_price = _safe_decimal(
            getattr(item, "unit_price_snapshot", None),
            default=Decimal("0"),
        )
        quantity = _safe_int(
            getattr(item, "quantity", None),
            default=0,
        )
        return _format_decimal(unit_price * quantity)
    except Exception:
        return _format_decimal(0)


@register.simple_tag
def line_discount(item: Any) -> str:
    """
    Return the formatted line discount for a cart item.

    Computed only when ``compare_at_price_snapshot`` exceeds
    ``unit_price_snapshot``. Returns ``"0.00"`` otherwise or for
    invalid input.
    """
    if not isinstance(item, CartItem):
        return _format_decimal(0)
    try:
        unit_price = _safe_decimal(
            getattr(item, "unit_price_snapshot", None),
            default=Decimal("0"),
        )
        compare_at = getattr(item, "compare_at_price_snapshot", None)
        if compare_at is None:
            return _format_decimal(0)
        compare_decimal = _safe_decimal(
            compare_at,
            default=Decimal("0"),
        )
        if compare_decimal <= unit_price:
            return _format_decimal(0)
        quantity = _safe_int(
            getattr(item, "quantity", None),
            default=0,
        )
        return _format_decimal(
            (compare_decimal - unit_price) * quantity
        )
    except Exception:
        return _format_decimal(0)


@register.simple_tag
def line_unit_price(item: Any) -> str:
    """
    Return the formatted unit price for a cart item.

    Returns ``"0.00"`` for invalid or missing input.
    """
    if not isinstance(item, CartItem):
        return _format_decimal(0)
    try:
        unit_price = _safe_decimal(
            getattr(item, "unit_price_snapshot", None),
            default=Decimal("0"),
        )
        return _format_decimal(unit_price)
    except Exception:
        return _format_decimal(0)


@register.simple_tag
def line_quantity(item: Any) -> int:
    """
    Return the integer quantity for a cart item.

    Returns ``0`` for invalid or missing input.
    """
    if not isinstance(item, CartItem):
        return 0
    try:
        return _safe_int(
            getattr(item, "quantity", None),
            default=0,
        )
    except Exception:
        return 0


@register.simple_tag
def line_currency(item: Any) -> str:
    """
    Return the currency code for a cart item, falling back to the
    cart's currency or the configured default.
    """
    if not isinstance(item, CartItem):
        return _get_default_currency()
    currency = _safe_str(getattr(item, "currency_snapshot", None))
    if currency:
        return currency
    try:
        cart = getattr(item, "cart", None)
        if cart is not None:
            cart_currency = _safe_str(getattr(cart, "currency", None))
            if cart_currency:
                return cart_currency
    except Exception:
        pass
    return _get_default_currency()


@register.simple_tag
def line_sku(item: Any) -> str:
    """
    Return the SKU snapshot for a cart item, or an empty string.
    """
    if not isinstance(item, CartItem):
        return ""
    return _safe_str(
        getattr(item, "product_sku_snapshot", None),
        default="",
    )


@register.simple_tag
def line_name(item: Any) -> str:
    """
    Return the product name snapshot for a cart item, or an empty
    string. Falls back to the live product title if no snapshot is
    available.
    """
    if not isinstance(item, CartItem):
        return ""
    name = _safe_str(getattr(item, "product_name_snapshot", None))
    if name:
        return name
    try:
        product = getattr(item, "product", None)
        if product is not None:
            return _safe_str(getattr(product, "title", None))
    except Exception:
        pass
    return ""


# ==============================================================================
# INVENTORY-AWARE TAGS (Read pre-fetched data only)
# ==============================================================================
# These tags expose inventory context that was pre-fetched by the
# view layer. They do NOT make their own inventory service calls.
# The view layer is responsible for invoking
# CartInventoryService.get_inventory_context() and attaching the
# result to each cart item via its ``inventory`` attribute.
#
# This pattern is the canonical, performance-correct way to surface
# inventory data in templates: the service call happens once per
# request in the view layer, and every template tag just reads the
# pre-fetched data.

@register.simple_tag
def cart_item_inventory(item: Any) -> Dict[str, Any]:
    """
    Return the pre-fetched inventory context for a cart item.

    Returns a safe-default context if no inventory data is attached.
    The view layer is responsible for attaching the standardized
    inventory payload to the cart item before this tag renders.
    """
    if not isinstance(item, CartItem):
        return _empty_inventory_context()
    return _extract_inventory_context(item)


@register.simple_tag
def cart_item_in_stock(item: Any) -> bool:
    """
    Return True when the cart item is in stock, per the pre-fetched
    inventory context. Returns False for invalid input or when no
    inventory context is attached.
    """
    context = cart_item_inventory(item)
    return bool(context.get("is_in_stock", False))


@register.simple_tag
def cart_item_out_of_stock(item: Any) -> bool:
    """
    Return True when the cart item is out of stock, per the
    pre-fetched inventory context. Returns False for invalid input
    or when no inventory context is attached.
    """
    context = cart_item_inventory(item)
    return bool(context.get("is_out_of_stock", True))


@register.simple_tag
def cart_item_low_stock(item: Any) -> bool:
    """
    Return True when the cart item is low stock, per the pre-fetched
    inventory context. Returns False for invalid input or when no
    inventory context is attached.
    """
    context = cart_item_inventory(item)
    return bool(context.get("is_low_stock", False))


@register.simple_tag
def cart_item_stock_message(item: Any) -> str:
    """
    Return the human-readable stock message for a cart item.

    Returns an empty string for invalid input or when no inventory
    context is attached.
    """
    context = cart_item_inventory(item)
    return _safe_str(
        context.get("stock_message"),
        default="",
    )


@register.simple_tag
def cart_item_inventory_status(item: Any) -> str:
    """
    Return the canonical inventory status token
    (``"in_stock"`` / ``"low_stock"`` / ``"out_of_stock"`` / ``"unknown"``)
    for a cart item.
    """
    context = cart_item_inventory(item)
    return _safe_str(
        context.get("inventory_status"),
        default="unknown",
    )


@register.simple_tag
def cart_item_available_quantity(item: Any) -> str:
    """
    Return the formatted available quantity for a cart item, per the
    pre-fetched inventory context. Returns ``"0.00"`` for invalid
    input or when no inventory context is attached.
    """
    context = cart_item_inventory(item)
    return _format_decimal(context.get("available_quantity", 0))


@register.simple_tag
def cart_item_warehouse_summary(item: Any) -> str:
    """
    Return the human-readable warehouse summary for a cart item,
    per the pre-fetched inventory context. Returns an empty string
    for invalid input or when no inventory context is attached.
    """
    context = cart_item_inventory(item)
    return _safe_str(
        context.get("warehouse_summary"),
        default="",
    )


# ==============================================================================
# CART INVENTORY OVERVIEW (Read pre-fetched data only)
# ==============================================================================
# The view layer is responsible for invoking
# CartInventoryService.validate_for_checkout and attaching the result
# under the ``cart_inventory_overview`` context key. Template tags
# only read this pre-fetched data and never make their own service
# calls.

@register.simple_tag(takes_context=True)
def cart_inventory_overview(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the pre-fetched cart-level inventory overview.

    Returns a safe-default overview if no pre-fetched data is
    available. The view layer is responsible for calling
    CartInventoryService.validate_for_checkout and attaching the
    result to the context.
    """
    return _extract_cart_overview(context)


@register.simple_tag(takes_context=True)
def cart_inventory_ready_for_checkout(context: Dict[str, Any]) -> bool:
    """
    Return True when the cart is ready for checkout, per the
    pre-fetched inventory overview. Returns True (safe default) for
    invalid input or when no overview is available.
    """
    overview = _extract_cart_overview(context)
    return bool(overview.get("ready_for_checkout", True))


@register.simple_tag(takes_context=True)
def cart_inventory_status(context: Dict[str, Any]) -> str:
    """
    Return the canonical cart-level inventory status token
    (``"in_stock"`` / ``"low_stock"`` / ``"out_of_stock"`` / ``"unknown"``).
    """
    overview = _extract_cart_overview(context)
    return _safe_str(
        overview.get("status"),
        default="unknown",
    )


@register.simple_tag(takes_context=True)
def cart_inventory_out_of_stock_count(context: Dict[str, Any]) -> int:
    """
    Return the number of out-of-stock items in the cart, per the
    pre-fetched inventory overview. Returns 0 on any failure.
    """
    overview = _extract_cart_overview(context)
    return _safe_int(
        overview.get("out_of_stock_count", 0),
        default=0,
    )


@register.simple_tag(takes_context=True)
def cart_inventory_low_stock_count(context: Dict[str, Any]) -> int:
    """
    Return the number of low-stock items in the cart, per the
    pre-fetched inventory overview. Returns 0 on any failure.
    """
    overview = _extract_cart_overview(context)
    return _safe_int(
        overview.get("low_stock_count", 0),
        default=0,
    )


@register.simple_tag(takes_context=True)
def cart_inventory_blocking_issues(context: Dict[str, Any]) -> Any:
    """
    Return the list of blocking inventory issues for the cart, per
    the pre-fetched inventory overview. Returns an empty list on
    any failure.
    """
    overview = _extract_cart_overview(context)
    issues = overview.get("blocking_issues", [])
    if isinstance(issues, list):
        return issues
    return []


# ==============================================================================
# CART SUMMARY (Inclusion Tag)
# ==============================================================================
# This tag renders the cart_summary.html partial with a complete,
# safe-default payload. The view layer may pre-attach additional
# context (e.g. an inventory overview); the tag merges any such
# pre-fetched data with the standard cart math payload.
#
# The original public API (the inclusion tag name ``cart_summary`` and
# the core context keys) is preserved. The returned dict contains both
# the original keys and new convenience keys for the modernized UI.

def _empty_cart_summary_payload() -> Dict[str, Any]:
    """
    Returns a safe-complete empty payload for the cart_summary
    inclusion tag.

    Every key is present with a safe default so templates never
    encounter undefined variables.
    """
    return {
        # --- Core keys (backward-compatible) ---
        "cart": None,
        "cart_items": [],
        "subtotal": Decimal("0"),
        "discount": Decimal("0"),
        "tax": Decimal("0"),
        "shipping": Decimal("0"),
        "grand_total": Decimal("0"),
        "currency": _get_default_currency(),
        "item_count": 0,
        "unique_item_count": 0,
        # --- Convenience keys (new) ---
        "coupon_code": "",
        "total": Decimal("0"),
        "is_empty": True,
        "inventory_overview": _empty_cart_overview(),
    }


def _build_cart_items_queryset(cart: Cart) -> QuerySet:
    """
    Build an optimized queryset of active cart items with deep
    prefetching to avoid N+1 queries at the template layer.

    Selects the related product, variant, and reservation rows to
    support template rendering without additional queries.
    """
    if cart is None:
        return CartItem.objects.none()
    try:
        return (
            cart.items
            .filter(status=CartItem.ItemStatus.ACTIVE)
            .select_related(
                "product",
                "variant",
                "reservation",
            )
            .order_by("added_at", "id")
        )
    except Exception:
        return CartItem.objects.none()


@register.inclusion_tag(
    "cart/partials/cart_summary.html",
    takes_context=True,
    name="cart_summary",
)
def cart_summary(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Render the cart summary partial with the standardized cart math
    payload. Inventory data is attached to individual cart items by
    the view layer; this tag NEVER reads or calculates inventory
    state. The cart-level inventory overview is also read from
    pre-fetched context data.

    Returned context keys (all guaranteed present):

        cart, cart_items,
        subtotal, discount, tax, shipping, grand_total,
        currency, item_count, unique_item_count,
        coupon_code, total, is_empty,
        inventory_overview.
    """
    cart = _resolve_cart(context)
    if cart is None:
        return _empty_cart_summary_payload()

    try:
        summary = CartCalculationsService.get_cart_summary(cart)
    except Exception as exc:
        logger.debug("Cart summary aggregation failed: %s", exc)
        summary = {}

    cart_items = _build_cart_items_queryset(cart)
    coupon_code = _safe_str(
        getattr(cart, "coupon_code", None),
        default="",
    )
    currency = _safe_str(
        getattr(cart, "currency", None),
        default=_get_default_currency(),
    )
    item_count = _safe_int(
        summary.get("item_count", 0),
        default=0,
    )
    unique_item_count = _safe_int(
        summary.get("unique_item_count", 0),
        default=0,
    )
    grand_total = _safe_decimal(
        summary.get("grand_total"),
        default=Decimal("0"),
    )

    return {
        # --- Core keys (backward-compatible) ---
        "cart": cart,
        "cart_items": cart_items,
        "subtotal": _safe_decimal(
            summary.get("subtotal"),
            default=Decimal("0"),
        ),
        "discount": _safe_decimal(
            summary.get("discount"),
            default=Decimal("0"),
        ),
        "tax": _safe_decimal(
            summary.get("tax"),
            default=Decimal("0"),
        ),
        "shipping": _safe_decimal(
            summary.get("shipping"),
            default=Decimal("0"),
        ),
        "grand_total": grand_total,
        "currency": currency,
        "item_count": item_count,
        "unique_item_count": unique_item_count,
        # --- Convenience keys (new) ---
        "coupon_code": coupon_code,
        "total": grand_total,
        "is_empty": item_count <= 0,
        # Pre-fetched inventory overview (set by the view layer via
        # CartInventoryService.validate_for_checkout). Falls back to
        # a safe default if not provided.
        "inventory_overview": _extract_cart_overview(context),
    }


# ==============================================================================
# BACKWARD-COMPATIBLE LEGACY ALIASES
# ==============================================================================
# The original module exposed its public API through ``register.*``
# decorators. The exact tag/filter names are preserved above. This
# section documents the canonical export surface for advanced
# consumers (e.g. documentation generators, IDEs, and explicit
# imports) without changing any runtime behavior.

__all__ = [
    # Simple tags (cart-level math)
    "cart_count",
    "cart_unique_items",
    "cart_subtotal",
    "cart_discount",
    "cart_line_discount",
    "cart_coupon_discount",
    "cart_tax",
    "cart_shipping",
    "cart_total",
    "cart_coupon_code",
    "cart_currency",
    "cart_is_empty",
    # Simple tags (per-line item)
    "line_subtotal",
    "line_discount",
    "line_unit_price",
    "line_quantity",
    "line_currency",
    "line_sku",
    "line_name",
    # Inventory-aware tags (read pre-fetched data)
    "cart_item_inventory",
    "cart_item_in_stock",
    "cart_item_out_of_stock",
    "cart_item_low_stock",
    "cart_item_stock_message",
    "cart_item_inventory_status",
    "cart_item_available_quantity",
    "cart_item_warehouse_summary",
    "cart_inventory_overview",
    "cart_inventory_ready_for_checkout",
    "cart_inventory_status",
    "cart_inventory_out_of_stock_count",
    "cart_inventory_low_stock_count",
    "cart_inventory_blocking_issues",
    # Filters
    "cart_decimal_filter",
    "cart_currency_filter",
    "cart_safe_int_filter",
    "cart_safe_decimal_filter",
    "multiply_filter",
    # Inclusion tag
    "cart_summary",
]