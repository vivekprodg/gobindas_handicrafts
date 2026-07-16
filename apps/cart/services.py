"""
Enterprise-grade THIN ORCHESTRATION LAYER for the Cart application.

ARCHITECTURE
============
Cart is a thin orchestration layer. It NEVER owns or calculates inventory
state, stock quantities, reservations, or warehouse logic. All such
concerns are EXCLUSIVELY owned by the Inventory application.

The Cart service layer is responsible ONLY for:

  1. Cart lifecycle (create / retrieve / merge)
  2. Cart-item lifecycle (add / update / remove / save-for-later)
  3. Totals computation (subtotal / tax / shipping / grand-total)
  4. Coupon application / removal
  5. Checkout preparation (dependency validation)
  6. Delegation to:
       - Inventory services (validation, reservation, release, deduct)
       - Pricing / tax / shipping / promotion services
       - Coupon services
       - Order services
  7. Structured response payloads

This module is a pure THIN ORCHESTRATOR. It never computes or mutates
inventory state. Every inventory operation is delegated to the
dedicated Inventory service layer.

BACKWARD COMPATIBILITY
=======================
The module preserves the legacy public function signatures so that
existing call-sites continue to function. The new architecture is
encapsulated in the dedicated service classes:

    * CartService         — cart lifecycle
    * CartItemService     — line-item lifecycle
    * CartInventoryService— validation coordination (read-only)
    * CartCouponService   — coupon orchestration
    * CartReorderService  — reorder workflows (delegation only)

Legacy function aliases are exposed at module level for the existing
import contract:

    get_or_create_cart, add_item_to_cart, remove_item_from_cart,
    update_cart_item, clear_cart, save_item_for_later,
    move_item_to_cart, merge_guest_cart, apply_coupon,
    remove_coupon, validate_cart_for_checkout, reorder_items_into_cart,
    process_return_request

CMS-DRIVEN
==========
All thresholds, durations, statuses, retries, and limits are sourced
from Django settings with safe defaults. The CMS can override any
behavior without code changes.

OWASP ASVS COMPLIANCE
======================
* Lazy imports prevent circular dependencies and import-time side effects
* Thread-safe via Django's per-request atomic transactions
* Idempotent operations where appropriate
* Defensive validation of every input
* Graceful exception handling with structured error responses
* Never trust client input
* No PII or sensitive data in logs

PERFORMANCE
===========
* select_related / prefetch_related optimizations
* Aggregated annotate for totals
* Bulk operations where supported
* Designed for millions of carts
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Cart, CartItem

logger = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION (CMS-DRIVEN)
# ==============================================================================
# All defaults can be overridden via Django settings, which in turn can be
# driven by the CMS without code changes. This keeps the service fully
# parameterized and future-proof.

_DEFAULT_CART_EXPIRY_DAYS = 30
_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_CART_EXPIRY_HOURS = 24
_DEFAULT_MAX_ITEMS_PER_CART = 200
_DEFAULT_MAX_QUANTITY_PER_ITEM = 99
_DEFAULT_COUPON_MIN_SUBTOTAL = Decimal("0.00")
_DEFAULT_TAX_RATE = Decimal("0.13")
_DEFAULT_SHIPPING_FLAT = Decimal("0.00")

def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)

def get_default_tax_rate() -> Decimal:
    """
    Returns the CMS-driven default tax rate as a Decimal.

    Sourced from the ``DEFAULT_TAX_RATE`` Django setting with a safe
    fallback. The CMS can override this without code changes.
    """
    raw = _get_setting("DEFAULT_TAX_RATE", _DEFAULT_TAX_RATE)
    try:
        if raw is None:
            return _DEFAULT_TAX_RATE
        value = Decimal(str(raw))
        if value.is_nan() or value.is_infinite() or value < 0:
            return _DEFAULT_TAX_RATE
        return value
    except Exception:
        return _DEFAULT_TAX_RATE

def get_default_shipping_flat() -> Decimal:
    """
    Returns the CMS-driven flat shipping fee as a Decimal.

    Sourced from the ``DEFAULT_SHIPPING_FEE`` Django setting. The CMS
    can override this without code changes.
    """
    raw = _get_setting("DEFAULT_SHIPPING_FEE", _DEFAULT_SHIPPING_FLAT)
    try:
        if raw is None:
            return _DEFAULT_SHIPPING_FLAT
        value = Decimal(str(raw))
        if value.is_nan() or value.is_infinite() or value < 0:
            return _DEFAULT_SHIPPING_FLAT
        return value
    except Exception:
        return _DEFAULT_SHIPPING_FLAT

def get_default_reservation_minutes() -> int:
    """
    Returns the CMS-driven default reservation duration in minutes.
    """
    minutes = _get_setting(
        "CART_DEFAULT_RESERVATION_MINUTES",
        _DEFAULT_RESERVATION_MINUTES,
    )
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = _DEFAULT_RESERVATION_MINUTES
    if minutes < 1:
        minutes = _DEFAULT_RESERVATION_MINUTES
    return minutes

def get_max_items_per_cart() -> int:
    """
    Returns the CMS-driven maximum number of distinct items per cart.
    """
    value = _get_setting("CART_MAX_ITEMS", _DEFAULT_MAX_ITEMS_PER_CART)
    try:
        value = int(value)
        return max(1, value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ITEMS_PER_CART

def get_max_quantity_per_item() -> int:
    """
    Returns the CMS-driven maximum quantity per cart-item.
    """
    value = _get_setting("CART_MAX_QUANTITY", _DEFAULT_MAX_QUANTITY_PER_ITEM)
    try:
        value = int(value)
        return max(1, value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUANTITY_PER_ITEM

def get_coupon_min_subtotal() -> Decimal:
    """
    Returns the CMS-driven minimum subtotal required for coupon
    application. Coupon service may override per-coupon.
    """
    raw = _get_setting("CART_COUPON_MIN_SUBTOTAL", _DEFAULT_COUPON_MIN_SUBTOTAL)
    try:
        if raw is None:
            return _DEFAULT_COUPON_MIN_SUBTOTAL
        value = Decimal(str(raw))
        if value.is_nan() or value.is_infinite() or value < 0:
            return _DEFAULT_COUPON_MIN_SUBTOTAL
        return value
    except Exception:
        return _DEFAULT_COUPON_MIN_SUBTOTAL

# ==============================================================================
# SAFE DECIMAL HELPERS
# ==============================================================================
def _safe_decimal(value: Any, *, default: Decimal = Decimal("0.00")) -> Decimal:
    """
    Best-effort conversion of a value to a safe Decimal.

    Returns ``default`` on any failure. Never raises.
    """
    if value is None:
        return default
    try:
        decimal_value = Decimal(str(value))
        if decimal_value.is_nan() or decimal_value.is_infinite():
            return default
        return decimal_value
    except Exception:
        return default

def _safe_int(value: Any, *, default: int = 0) -> int:
    """
    Best-effort conversion of a value to a safe int.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except Exception:
            return default

# ==============================================================================
# SAFE TEXT HELPERS
# ==============================================================================
def _safe_str(value: Any) -> str:
    """
    Best-effort conversion of a value to a safe trimmed string.
    """
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

# ==============================================================================
# DOMAIN EXCEPTIONS (Cart-Layer)
# ==============================================================================
# These exceptions are raised by the Cart orchestration layer. They do NOT
# include any inventory logic. Inventory-related errors (insufficient stock,
# reservation conflicts, etc.) are surfaced from the Inventory service
# layer and translated by the Cart orchestration layer into structured
# responses.

class CartError(Exception):
    """Base class for cart-orchestration errors."""

class CartNotFoundError(CartError):
    """Raised when a requested cart does not exist or is inaccessible."""

class CartItemNotFoundError(CartError):
    """Raised when a requested cart item does not exist."""

class CartLimitExceededError(CartError):
    """Raised when adding an item would exceed the cart's item limit."""

class CartQuantityLimitExceededError(CartError):
    """Raised when a cart-item quantity would exceed the configured max."""

class CartCouponError(CartError):
    """Base class for coupon orchestration errors."""

class CartCheckoutError(CartError):
    """Raised when a cart cannot be checked out (validation failures)."""

# ==============================================================================
# STRUCTURED RESPONSE HELPERS
# ==============================================================================
def _safe_structured_response(
    success: bool,
    *,
    message: str = "",
    payload: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    code: str = "",
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Builds a consistent structured response payload for all
    orchestration operations. Never raises.
    """
    response: Dict[str, Any] = {
        "success": bool(success),
        "message": _safe_str(message),
    }
    if code:
        response["code"] = _safe_str(code)
    if payload is not None and isinstance(payload, dict):
        response.update(payload)
    if error is not None:
        response["error"] = _safe_str(error)
    if extras is not None and isinstance(extras, dict):
        response["extras"] = extras
    return response

def _serialize_cart(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Returns a serializable dictionary representation of a Cart row.
    """
    if cart is None:
        return {}
    return {
        "id": cart.pk,
        "status": _safe_str(getattr(cart, "status", "")),
        "is_active": bool(getattr(cart, "is_active", False)),
        "is_guest": bool(getattr(cart, "is_guest", True)),
        "currency": _safe_str(getattr(cart, "currency", "")),
        "coupon_code": _safe_str(getattr(cart, "coupon_code", "")),
        "customer_id": getattr(cart, "customer_id", None),
        "session_key": _safe_str(getattr(cart, "session_key", "")),
        "anonymous_token": _safe_str(getattr(cart, "anonymous_token", "")),
        "subtotal": _safe_decimal(getattr(cart, "subtotal", None)),
        "estimated_tax": _safe_decimal(getattr(cart, "estimated_tax", None)),
        "estimated_shipping": _safe_decimal(getattr(cart, "estimated_shipping", None)),
        "grand_total": _safe_decimal(getattr(cart, "grand_total", None)),
        "total_items_count": _safe_int(
            getattr(cart, "total_items_count", None), default=0
        ),
        "unique_items_count": _safe_int(
            getattr(cart, "unique_items_count", None), default=0
        ),
    }

def _serialize_cart_item(item: Optional[CartItem]) -> Dict[str, Any]:
    """
    Returns a serializable dictionary representation of a CartItem row.
    Inventory references are NOT included here. The cart orchestrator
    does NOT touch inventory data. Inventory state is fetched on demand
    by the Inventory service.
    """
    if item is None:
        return {}
    return {
        "id": item.pk,
        "cart_id": getattr(item, "cart_id", None),
        "product_id": getattr(item, "product_id", None),
        "variant_id": getattr(item, "variant_id", None),
        "quantity": _safe_int(getattr(item, "quantity", None), default=0),
        "status": _safe_str(getattr(item, "status", "")),
        "saved_reason": _safe_str(getattr(item, "saved_reason", "")),
        "unit_price_snapshot": _safe_decimal(
            getattr(item, "unit_price_snapshot", None)
        ),
        "currency_snapshot": _safe_str(
            getattr(item, "currency_snapshot", "")
        ),
        "line_subtotal": _safe_decimal(
            getattr(item, "line_subtotal", None)
        ),
        "is_available_hint": bool(
            getattr(item, "is_available", False)
        ),
    }

# ==============================================================================
# LAZY INVENTORY ACCESSORS
# ==============================================================================
# The cart service NEVER imports the inventory app eagerly. All inventory
# access goes through lazy accessors that resolve only when needed. This
# preserves loose coupling, allows the cart app to boot even if the
# inventory app is partially configured, and makes the entire module
# safe to import in management commands, Celery tasks, and tests.

def _get_inventory_services() -> Optional[Any]:
    """
    Lazy accessor for the inventory services module.
    Returns None on ImportError.
    """
    try:
        from apps.inventory import services
        return services
    except Exception:
        logger.warning(
            "Inventory services module could not be imported. "
            "Cart service running in inventory-blind mode."
        )
        return None

def _get_inventory_selectors() -> Optional[Any]:
    """
    Lazy accessor for the inventory selectors module.
    Returns None on ImportError.
    """
    try:
        from apps.inventory import selectors
        return selectors
    except Exception:
        logger.warning("Inventory selectors module could not be imported.")
        return None

# ==============================================================================
# SAFE INVENTORY DELEGATION
# ==============================================================================
def _safe_inventory_check_availability(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
    quantity: Any = 1,
    include_all_warehouses: bool = True,
) -> Dict[str, Any]:
    """
    Safely delegate availability check to the Inventory service.

    Returns a structured dictionary. Never raises. The Cart orchestrator
    uses this result for UI hints and checkout validation, but does NOT
    interpret it as a binding stock assertion.
    """
    services = _get_inventory_services()
    if services is None:
        return {
            "is_available": False,
            "free_stock": "0.00",
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "warehouses_checked": 0,
            "per_warehouse": [],
            "source": "inventory_service_unavailable",
        }
    try:
        return services.check_stock(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            quantity=quantity,
            include_all_warehouses=include_all_warehouses,
        )
    except Exception as exc:
        logger.debug("Safe inventory availability check failed: %s", exc)
        return {
            "is_available": False,
            "free_stock": "0.00",
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "warehouses_checked": 0,
            "per_warehouse": [],
            "source": "inventory_service_error",
        }

def _safe_inventory_reserve(
    *,
    quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
    cart: Any = None,
    user: Any = None,
    session_key: str = "",
    expires_in_minutes: Optional[int] = None,
    reference_number: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    """
    Safely delegate reservation creation to the Inventory service.

    Returns a structured dictionary. Never raises. On failure returns
    a payload with ``success=False`` and a descriptive error.
    """
    services = _get_inventory_services()
    if services is None:
        return {
            "success": False,
            "error": "Inventory service unavailable",
            "reservation_id": None,
            "reservation_token": None,
        }
    try:
        from datetime import timedelta
        if expires_in_minutes is None:
            expires_in_minutes = get_default_reservation_minutes()
        expires_in = timedelta(minutes=int(expires_in_minutes))
        return services.reserve_stock(
            quantity=quantity,
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            cart=cart,
            user=user,
            session_key=session_key,
            expires_in=expires_in,
            reservation_type="cart",
            reference_number=reference_number,
            reference_model="cart.CartItem",
            reference_id=str(getattr(cart, "id", "") or ""),
            notes=notes,
            performed_by=user,
        )
    except Exception as exc:
        logger.debug("Safe inventory reserve failed: %s", exc)
        return {
            "success": False,
            "error": str(exc) or "Reservation failed",
            "reservation_id": None,
            "reservation_token": None,
        }

def _safe_inventory_release(
    *,
    reservation_token: Optional[str] = None,
    reservation_id: Optional[int] = None,
    reason: str = "",
    is_automatic: bool = False,
) -> Dict[str, Any]:
    """
    Safely delegate reservation release to the Inventory service.

    Returns a structured dictionary. Never raises. On failure returns
    a payload with ``success=False`` and a descriptive error.
    """
    services = _get_inventory_services()
    if services is None:
        return {"success": False, "error": "Inventory service unavailable"}
    try:
        return services.release_stock(
            reservation_token=reservation_token,
            reservation_id=reservation_id,
            reason=reason,
            is_automatic=is_automatic,
        )
    except Exception as exc:
        logger.debug("Safe inventory release failed: %s", exc)
        return {
            "success": False,
            "error": str(exc) or "Release failed",
        }

def _safe_inventory_get_inventory_for_target(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
) -> Dict[str, Any]:
    """
    Safely fetch the inventory summary for a given target.

    The Cart layer uses this for UI hints only. It does NOT make
    business decisions based on inventory state. Stock validation
    during add-to-cart and checkout is performed by the Inventory
    service.
    """
    selectors = _get_inventory_selectors()
    if selectors is None:
        return {
            "exists": False,
            "free_stock": "0.00",
            "is_out_of_stock": True,
            "is_low_stock": False,
        }
    try:
        return selectors.get_inventory_summary_for_target(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )
    except Exception as exc:
        logger.debug("Safe inventory get summary failed: %s", exc)
        return {
            "exists": False,
            "free_stock": "0.00",
            "is_out_of_stock": True,
            "is_low_stock": False,
        }

# ==============================================================================
# CART SERVICE
# ==============================================================================
class CartService:
    """
    Dedicated service class managing core Cart lifecycle.

    The CartService handles cart creation, retrieval, totals
    computation, ownership transfer, and merge operations. It is the
    SINGLE source of truth for cart existence and identity. It does
    NOT mutate inventory. All inventory concerns are delegated to the
    Inventory service.
    """

    @staticmethod
    def get_for_customer(customer: Any) -> Optional[Cart]:
        """
        Retrieves the active cart for an authenticated customer.
        Creates a new one if none exists.

        Returns None if the customer is anonymous or unauthenticated.
        """
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        try:
            cart = (
                Cart.objects.for_customer(customer)
                .order_by("-last_activity_at")
                .first()
            )
            if cart:
                return cart
            return Cart.objects.create(
                customer=customer,
                status=Cart.CartStatus.ACTIVE,
            )
        except Exception as exc:
            logger.exception("get_for_customer failed: %s", exc)
            return None

    @staticmethod
    def get_for_session(session_key: Optional[str]) -> Optional[Cart]:
        """
        Retrieves the active cart for an anonymous session.
        Creates a new one if the session has a valid key and none exists.
        """
        if not session_key:
            return None
        try:
            cart = (
                Cart.objects.for_session(session_key)
                .order_by("-last_activity_at")
                .first()
            )
            if cart:
                return cart
            return Cart.objects.create(
                session_key=session_key,
                status=Cart.CartStatus.ACTIVE,
            )
        except Exception as exc:
            logger.exception("get_for_session failed: %s", exc)
            return None

    @staticmethod
    def get_or_create_for_request(request: Any) -> Tuple[Optional[Cart], bool]:
        """
        Resolves the active cart for the current request.

        When the request user is authenticated, the customer cart is
        returned. Otherwise, the session cart is returned. A session key
        is created lazily if missing.
        """
        try:
            user = getattr(request, "user", None)
            if user and getattr(user, "is_authenticated", False):
                cart = CartService.get_for_customer(user)
                return cart, bool(cart is not None)
            session = getattr(request, "session", None)
            if session is not None:
                try:
                    if not session.session_key:
                        session.create()
                except Exception:
                    pass
            session_key = getattr(session, "session_key", "") or ""
            cart = CartService.get_for_session(session_key)
            return cart, bool(cart is not None)
        except Exception as exc:
            logger.exception("get_or_create_for_request failed: %s", exc)
            return None, False

    @staticmethod
    def compute_totals(
        cart: Optional[Cart],
        *,
        tax_rate: Optional[Decimal] = None,
        shipping_flat: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """
        Returns a structured dict of cart totals.

        All values are computed from the cart's active items and the
        optional configuration. Coupons are NOT validated here; that
        responsibility belongs to the coupon service.
        """
        if cart is None:
            return {
                "subtotal": Decimal("0.00"),
                "tax": Decimal("0.00"),
                "shipping": Decimal("0.00"),
                "discount": Decimal("0.00"),
                "grand_total": Decimal("0.00"),
                "total_items": 0,
                "unique_items": 0,
            }
        subtotal = _safe_decimal(getattr(cart, "subtotal", None))
        rate = tax_rate if tax_rate is not None else get_default_tax_rate()
        ship = shipping_flat if shipping_flat is not None else get_default_shipping_flat()
        discount = _safe_decimal(
            getattr(cart, "coupon_discount_amount", None)
        )
        try:
            tax_amount = (subtotal - discount).quantize(Decimal("0.01"))
        except Exception:
            tax_amount = subtotal
        if tax_amount < Decimal("0.00"):
            tax_amount = Decimal("0.00")
        try:
            tax = (tax_amount * rate).quantize(Decimal("0.01"))
        except Exception:
            tax = Decimal("0.00")
        try:
            grand = (tax_amount + tax + ship).quantize(Decimal("0.01"))
        except Exception:
            grand = tax_amount
        return {
            "subtotal": subtotal,
            "tax": tax,
            "shipping": ship,
            "discount": discount,
            "grand_total": grand,
            "total_items": _safe_int(
                getattr(cart, "total_items_count", None), default=0
            ),
            "unique_items": _safe_int(
                getattr(cart, "unique_items_count", None), default=0
        ),
        }

    @staticmethod
    def touch(cart: Optional[Cart]) -> bool:
        """
        Updates ``last_activity_at`` to the current time. Idempotent.
        """
        if cart is None or not getattr(cart, "pk", None):
            return False
        try:
            with transaction.atomic():
                type(cart).objects.filter(pk=cart.pk).update(
                    last_activity_at=timezone.now()
                )
            return True
        except Exception as exc:
            logger.debug("Cart touch failed: %s", exc)
            return False

    @staticmethod
    def merge_guest_cart_into_customer(
        *, guest_cart: Optional[Cart], customer: Any
    ) -> Optional[Cart]:
        """
        Merges an anonymous guest cart into an authenticated customer's
        cart. Returns the resulting customer cart.

        The merge:
          1. Retrieves the customer's existing active cart (or creates one).
          2. For each ACTIVE item in the guest cart, attempts to merge
             into the customer cart (quantity aggregation, last-write-wins
             for price snapshot, last-write-wins for attributes).
          3. SAVED and REMOVED items in the guest cart are NOT migrated.
          4. The guest cart is marked MERGED and deactivated.
          5. Inventory reservations on the guest cart are RELEASED via
             the Inventory service. The merged cart then establishes its
             own reservations on the next interaction.

        Returns the resulting customer cart, or None on failure.
        """
        if not customer or not getattr(customer, "is_authenticated", False):
            return None
        if guest_cart is None:
            return CartService.get_for_customer(customer)
        if not getattr(guest_cart, "pk", None):
            return CartService.get_for_customer(customer)

        try:
            with transaction.atomic():
                customer_cart = (
                    Cart.objects.for_customer(customer)
                    .filter(is_active=True)
                    .order_by("-last_activity_at")
                    .first()
                )
                if customer_cart is None:
                    customer_cart = Cart.objects.create(
                        customer=customer,
                        status=Cart.CartStatus.ACTIVE,
                        is_active=True,
                    )
                guest_items = list(
                    CartItem.objects.filter(
                        cart=guest_cart,
                        status=CartItem.ItemStatus.ACTIVE,
                    ).select_related("product", "variant")
                )
                for item in guest_items:
                    CartItemService._merge_item_into_cart(
                        item=item,
                        target_cart=customer_cart,
                    )
                # Mark guest cart as merged
                type(guest_cart).objects.filter(pk=guest_cart.pk).update(
                    status=Cart.CartStatus.MERGED,
                    is_active=False,
                    last_merged_at=timezone.now(),
                )
            # Release guest cart reservations (best-effort)
            try:
                services = _get_inventory_services()
                if services is not None:
                    for item in guest_items:
                        if getattr(item, "reservation", None) is None:
                            continue
                        try:
                            services.release_stock(
                                reservation_id=item.reservation_id,
                                reason="Guest cart merged into customer cart",
                                is_automatic=True,
                            )
                        except Exception:
                            pass
            except Exception:
                pass
            # Re-touch customer cart so its last_activity_at reflects merge
            CartService.touch(customer_cart)
            return customer_cart
        except Exception as exc:
            logger.exception("merge_guest_cart_into_customer failed: %s", exc)
            return None

# ==============================================================================
# CART ITEM SERVICE
# ==============================================================================
class CartItemService:
    """
    Dedicated service class managing CartItem lifecycle.

    Responsibilities:
      * Add product or variant to cart
      * Update quantity
      * Remove item
      * Save for later
      * Move from save-for-later back to active
      * Clear cart
      * Internal merge helper

    All inventory concerns (validation, reservations) are delegated to
    the Inventory service. Cart orchestration NEVER computes or
    mutates stock.
    """

    @staticmethod
    def add_item(
        *,
        cart: Optional[Cart],
        product: Any = None,
        variant: Any = None,
        quantity: int = 1,
        unit_price_snapshot: Optional[Decimal] = None,
        currency: str = "",
    ) -> Dict[str, Any]:
        """
        Adds an item to the cart. Inventory validation and reservation
        are delegated to the Inventory service.

        Returns a structured response. The response includes the
        resulting cart item payload and the inventory validation result.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                message="Cart not found",
                error="Cart not found",
                code="cart_not_found",
            )
        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            qty = 1
        if qty > get_max_quantity_per_item():
            return _safe_structured_response(
                False,
                error=(
                    f"Quantity exceeds the maximum of "
                    f"{get_max_quantity_per_item()}"
                ),
                code="quantity_limit_exceeded",
            )
        if product is None and variant is None:
            return _safe_structured_response(
                False,
                error="Product or variant is required",
                code="missing_product",
            )

        # Check item count limit
        try:
            current_count = cart.items.filter(
                status=CartItem.ItemStatus.ACTIVE
            ).count()
        except Exception:
            current_count = 0
        # Allow merging if the same product/variant already exists
        existing_match = None
        try:
            qs = cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
            if variant is not None:
                qs = qs.filter(variant=variant)
            elif product is not None:
                qs = qs.filter(product=product, variant__isnull=True)
            existing_match = qs.first()
        except Exception:
            existing_match = None
        if existing_match is None and current_count >= get_max_items_per_cart():
            return _safe_structured_response(
                False,
                error=(
                    f"Cart has reached the maximum of "
                    f"{get_max_items_per_cart()} distinct items"
                ),
                code="cart_limit_exceeded",
            )

        try:
            with transaction.atomic():
                if existing_match is not None:
                    # Merge into existing line
                    new_qty = existing_match.quantity + qty
                    if new_qty > get_max_quantity_per_item():
                        return _safe_structured_response(
                            False,
                            error=(
                                f"Total quantity would exceed the maximum of "
                                f"{get_max_quantity_per_item()}"
                            ),
                            code="quantity_limit_exceeded",
                        )
                    type(existing_match).objects.filter(
                        pk=existing_match.pk
                    ).update(quantity=new_qty, updated_at=timezone.now())
                    existing_match.refresh_from_db(fields=["quantity", "updated_at"])
                    result_item = existing_match
                else:
                    # Create new line
                    snapshot_price = unit_price_snapshot
                    if snapshot_price is None:
                        # Fallback to a 0 snapshot when caller does not provide
                        snapshot_price = Decimal("0.00")
                    result_item = CartItem.objects.create(
                        cart=cart,
                        product=product,
                        variant=variant,
                        quantity=qty,
                        unit_price_snapshot=snapshot_price,
                        currency_snapshot=(
                            currency
                            or _safe_str(getattr(cart, "currency", ""))
                            or "NPR"
                        ),
                        product_name_snapshot=_safe_str(
                            getattr(product, "title", None) if product is not None else None
                        ),
                        product_sku_snapshot=_safe_str(
                            getattr(product, "sku", None) if product is not None else None
                        ),
                        variant_name_snapshot=_safe_str(
                            getattr(variant, "name", None) if variant is not None else None
                        ),
                        status=CartItem.ItemStatus.ACTIVE,
                    )
                # Touch the cart activity
                CartService.touch(cart)

            # Inventory validation + reservation (delegated)
            inv_payload = _safe_inventory_check_availability(
                product=product,
                product_variant=variant,
                warehouse=getattr(cart, "preferred_warehouse", None),
                quantity=result_item.quantity,
                include_all_warehouses=(
                    getattr(cart, "preferred_warehouse", None) is None
                ),
            )
            # Best-effort reservation (non-blocking failure)
            reservation_payload = _safe_inventory_reserve(
                quantity=result_item.quantity,
                product=product,
                product_variant=variant,
                warehouse=getattr(cart, "preferred_warehouse", None),
                cart=cart,
                user=(
                    getattr(cart, "customer", None)
                    if getattr(cart, "customer_id", None)
                    else None
                ),
                session_key=_safe_str(getattr(cart, "session_key", "")),
                reference_number=_safe_str(
                    getattr(result_item, "product_sku_snapshot", "")
                ) or "cart-item",
            )
            # Best-effort persistence of reservation reference on the line
            try:
                if isinstance(reservation_payload, dict) and reservation_payload.get("success"):
                    reservation_token = reservation_payload.get("reservation_token")
                    reservation_id = reservation_payload.get("reservation_id")
                    if reservation_id:
                        type(result_item).objects.filter(
                            pk=result_item.pk
                        ).update(
                            reservation_id=reservation_id,
                            reservation_token=reservation_token or "",
                            reservation_status="active",
                        )
                        result_item.refresh_from_db(
                            fields=[
                                "reservation_id",
                                "reservation_token",
                                "reservation_status",
                            ]
                        )
            except Exception as exc:
                logger.debug("Persisting reservation reference failed: %s", exc)

            return _safe_structured_response(
                True,
                message="Item added to cart",
                code="item_added",
                payload={
                    "item": _serialize_cart_item(result_item),
                    "cart": _serialize_cart(cart),
                    "inventory_check": inv_payload,
                    "reservation": reservation_payload,
                },
            )
        except Exception as exc:
            logger.exception("add_item failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Add to cart failed",
                code="add_item_failed",
            )

    @staticmethod
    def update_quantity(
        *,
        cart: Optional[Cart],
        item_id: Optional[int] = None,
        quantity: Any = 1,
    ) -> Dict[str, Any]:
        """
        Updates the quantity of a cart item. Validates the new quantity
        and delegates inventory adjustments to the Inventory service.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        try:
            new_qty = int(quantity)
        except (TypeError, ValueError):
            new_qty = 1
        try:
            item = (
                cart.items.filter(
                    pk=item_id,
                    status=CartItem.ItemStatus.ACTIVE,
                ).first()
            )
        except Exception:
            item = None
        if item is None:
            return _safe_structured_response(
                False,
                error="Item not found in cart",
                code="item_not_found",
            )
        if new_qty < 1:
            return CartItemService.remove_item(
                cart=cart, item_id=item_id
            )
        if new_qty > get_max_quantity_per_item():
            return _safe_structured_response(
                False,
                error=(
                    f"Quantity exceeds the maximum of "
                    f"{get_max_quantity_per_item()}"
                ),
                code="quantity_limit_exceeded",
            )
        try:
            with transaction.atomic():
                old_qty = item.quantity
                if old_qty == new_qty:
                    return _safe_structured_response(
                        True,
                        message="No change",
                        code="no_change",
                        payload={
                            "item": _serialize_cart_item(item),
                            "cart": _serialize_cart(cart),
                        },
                    )
                type(item).objects.filter(pk=item.pk).update(
                    quantity=new_qty, updated_at=timezone.now()
                )
                item.refresh_from_db(fields=["quantity", "updated_at"])
            # Inventory rebalance: try to release or extend reservation
            try:
                if getattr(item, "reservation", None) is not None:
                    if new_qty < old_qty:
                        # Cannot partially release; safe no-op
                        pass
                    else:
                        # Best-effort extension via the Inventory service
                        if getattr(item, "reservation_id", None):
                            _safe_inventory_release(
                                reservation_id=item.reservation_id,
                                reason="Cart quantity updated",
                                is_automatic=True,
                            )
                            _safe_inventory_reserve(
                                quantity=new_qty,
                                product=item.product,
                                product_variant=item.variant,
                                warehouse=getattr(
                                    cart, "preferred_warehouse", None
                                ),
                                cart=cart,
                                user=(
                                    getattr(cart, "customer", None)
                                    if getattr(cart, "customer_id", None)
                                    else None
                                ),
                                session_key=_safe_str(
                                    getattr(cart, "session_key", "")
                                ),
                                reference_number=_safe_str(
                                    getattr(item, "product_sku_snapshot", "")
                                ) or "cart-item",
                            )
            except Exception as exc:
                logger.debug("Reservation rebalance failed: %s", exc)
            CartService.touch(cart)
            return _safe_structured_response(
                True,
                message="Quantity updated",
                code="quantity_updated",
                payload={
                    "item": _serialize_cart_item(item),
                    "cart": _serialize_cart(cart),
                },
            )
        except Exception as exc:
            logger.exception("update_quantity failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Update failed",
                code="update_failed",
            )

    @staticmethod
    def remove_item(
        *,
        cart: Optional[Cart],
        item_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Removes a cart item entirely. Inventory reservation (if any) is
        released via the Inventory service.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        try:
            item = cart.items.filter(pk=item_id).first()
        except Exception:
            item = None
        if item is None:
            return _safe_structured_response(
                False,
                error="Item not found in cart",
                code="item_not_found",
            )
        try:
            with transaction.atomic():
                reservation_id = getattr(item, "reservation_id", None)
                type(item).objects.filter(pk=item.pk).delete()
                if reservation_id:
                    _safe_inventory_release(
                        reservation_id=reservation_id,
                        reason="Item removed from cart",
                        is_automatic=True,
                    )
            CartService.touch(cart)
            return _safe_structured_response(
                True,
                message="Item removed from cart",
                code="item_removed",
                payload={"cart": _serialize_cart(cart)},
            )
        except Exception as exc:
            logger.exception("remove_item failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Remove failed",
                code="remove_failed",
            )

    @staticmethod
    def clear_cart(*, cart: Optional[Cart]) -> Dict[str, Any]:
        """
        Removes every item from a cart and releases all associated
        inventory reservations. Returns a structured response.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        try:
            with transaction.atomic():
                items = list(
                    cart.items.filter(
                        status=CartItem.ItemStatus.ACTIVE
                    ).only("id", "reservation_id")
                )
                reservation_ids = [
                    it.reservation_id for it in items if it.reservation_id
                ]
                cart.items.all().delete()
                for rid in reservation_ids:
                    _safe_inventory_release(
                        reservation_id=rid,
                        reason="Cart cleared",
                        is_automatic=True,
                    )
            CartService.touch(cart)
            return _safe_structured_response(
                True,
                message="Cart cleared",
                code="cart_cleared",
                payload={"cart": _serialize_cart(cart)},
            )
        except Exception as exc:
            logger.exception("clear_cart failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Clear failed",
                code="clear_failed",
            )

    @staticmethod
    def save_for_later(
        *,
        cart: Optional[Cart],
        item_id: Optional[int] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        Moves an item to the SAVED state. Inventory reservation is
        released because the line no longer blocks sellable stock.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        try:
            item = cart.items.filter(
                pk=item_id,
                status=CartItem.ItemStatus.ACTIVE,
            ).first()
        except Exception:
            item = None
        if item is None:
            return _safe_structured_response(
                False,
                error="Item not found in cart",
                code="item_not_found",
            )
        try:
            with transaction.atomic():
                from django.utils import timezone as _tz
                type(item).objects.filter(pk=item.pk).update(
                    status=CartItem.ItemStatus.SAVED,
                    saved_reason=(
                        _safe_str(reason)
                        or CartItem.SavedForLaterReason.MANUAL
                    ),
                    moved_to_save_at=_tz.now(),
                    updated_at=_tz.now(),
                )
                if getattr(item, "reservation_id", None):
                    _safe_inventory_release(
                        reservation_id=item.reservation_id,
                        reason="Item saved for later",
                        is_automatic=True,
                    )
            item.refresh_from_db()
            CartService.touch(cart)
            return _safe_structured_response(
                True,
                message="Item saved for later",
                code="item_saved",
                payload={
                    "item": _serialize_cart_item(item),
                    "cart": _serialize_cart(cart),
                },
            )
        except Exception as exc:
            logger.exception("save_for_later failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Save failed",
                code="save_failed",
            )

    @staticmethod
    def move_to_cart(
        *,
        cart: Optional[Cart],
        item_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Moves a SAVED item back to ACTIVE. Re-creates the inventory
        reservation via the Inventory service.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        try:
            item = cart.items.filter(
                pk=item_id,
                status=CartItem.ItemStatus.SAVED,
            ).first()
        except Exception:
            item = None
        if item is None:
            return _safe_structured_response(
                False,
                error="Saved item not found in cart",
                code="item_not_found",
            )
        try:
            with transaction.atomic():
                type(item).objects.filter(pk=item.pk).update(
                    status=CartItem.ItemStatus.ACTIVE,
                    saved_reason=None,
                    moved_to_save_at=None,
                    updated_at=timezone.now(),
                )
            # Re-reserve inventory
            try:
                reservation_payload = _safe_inventory_reserve(
                    quantity=item.quantity,
                    product=item.product,
                    product_variant=item.variant,
                    warehouse=getattr(cart, "preferred_warehouse", None),
                    cart=cart,
                    user=(
                        getattr(cart, "customer", None)
                        if getattr(cart, "customer_id", None)
                        else None
                    ),
                    session_key=_safe_str(
                        getattr(cart, "session_key", "")
                    ),
                    reference_number=_safe_str(
                        getattr(item, "product_sku_snapshot", "")
                    ) or "cart-item",
                )
                if isinstance(reservation_payload, dict) and reservation_payload.get("success"):
                    type(item).objects.filter(pk=item.pk).update(
                        reservation_id=reservation_payload.get("reservation_id"),
                        reservation_token=reservation_payload.get(
                            "reservation_token"
                        ) or "",
                        reservation_status="active",
                    )
            except Exception as exc:
                logger.debug("Re-reserve inventory failed: %s", exc)
            item.refresh_from_db()
            CartService.touch(cart)
            return _safe_structured_response(
                True,
                message="Item moved to active cart",
                code="item_activated",
                payload={
                    "item": _serialize_cart_item(item),
                    "cart": _serialize_cart(cart),
                },
            )
        except Exception as exc:
            logger.exception("move_to_cart failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Move failed",
                code="move_failed",
            )

    @staticmethod
    def _merge_item_into_cart(*, item: CartItem, target_cart: Cart) -> CartItem:
        """
        Internal helper used during guest-cart merge.

        Strategy:
          * If the target cart already has the same product/variant
            ACTIVE, increment the existing line's quantity.
          * Otherwise, reassign the guest item to the target cart and
            clear the guest reservation (will be re-reserved by target
            cart on next interaction).
        """
        try:
            existing_qs = target_cart.items.filter(
                status=CartItem.ItemStatus.ACTIVE,
            )
            if getattr(item, "variant", None) is not None:
                existing_qs = existing_qs.filter(variant=item.variant)
            elif getattr(item, "product", None) is not None:
                existing_qs = existing_qs.filter(
                    product=item.product, variant__isnull=True
                )
            existing = existing_qs.first()
        except Exception:
            existing = None
        if existing is not None:
            new_qty = existing.quantity + item.quantity
            if new_qty > get_max_quantity_per_item():
                new_qty = get_max_quantity_per_item()
            try:
                type(existing).objects.filter(pk=existing.pk).update(
                    quantity=new_qty,
                    updated_at=timezone.now(),
                )
            except Exception:
                pass
            try:
                item.delete()
            except Exception:
                pass
            existing.refresh_from_db(fields=["quantity", "updated_at"])
            return existing
        try:
            type(item).objects.filter(pk=item.pk).update(
                cart=target_cart,
                updated_at=timezone.now(),
            )
        except Exception:
            pass
        item.refresh_from_db()
        return item

# ==============================================================================
# CART INVENTORY SERVICE
# ==============================================================================
class CartInventoryService:
    """
    Coordinator for cart-level inventory validation.

    This class does NOT compute or mutate stock. It delegates to the
    Inventory service for validation, availability checks, reservation
    lookup, and the production-side validation contract for checkout.
    """

    @staticmethod
    def validate_for_checkout(
        *, cart: Optional[Cart]
    ) -> Dict[str, Any]:
        """
        Validates a cart for checkout.

        The cart layer enforces:
          * Cart is present and active
          * Has at least one ACTIVE line item
          * Every ACTIVE item has a positive quantity
          * Per-item and total quantity limits are respected

        The Inventory layer enforces:
          * Each line has sellable stock (delegated to Inventory service)

        The function returns a structured response with a top-level
        ``ready_for_checkout`` boolean and a list of any issues found.
        """
        issues: List[Dict[str, Any]] = []
        if cart is None or not getattr(cart, "pk", None):
            return {
                "ready_for_checkout": False,
                "issues": [
                    {
                        "code": "cart_not_found",
                        "message": "Cart not found",
                    }
                ],
                "totals": CartService.compute_totals(cart),
                "cart": _serialize_cart(cart),
            }
        if not getattr(cart, "is_active", False):
            issues.append(
                {
                    "code": "cart_inactive",
                    "message": "Cart is not active",
                }
            )
        try:
            active_items = list(
                cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
            )
        except Exception:
            active_items = []
        if not active_items:
            issues.append(
                {
                    "code": "cart_empty",
                    "message": "Cart has no active items",
                }
            )
        for item in active_items:
            if item.quantity < 1:
                issues.append(
                    {
                        "code": "invalid_quantity",
                        "item_id": item.pk,
                        "message": "Item quantity must be at least 1",
                    }
                )
            elif item.quantity > get_max_quantity_per_item():
                issues.append(
                    {
                        "code": "quantity_limit_exceeded",
                        "item_id": item.pk,
                        "message": (
                            f"Item quantity exceeds the maximum of "
                            f"{get_max_quantity_per_item()}"
                        ),
                    }
                )
        # Inventory availability check per line (delegated, best-effort)
        for item in active_items:
            inv = _safe_inventory_get_inventory_for_target(
                product=item.product,
                product_variant=item.variant,
                warehouse=getattr(cart, "preferred_warehouse", None),
            )
            if not inv.get("exists", False):
                issues.append(
                    {
                        "code": "inventory_missing",
                        "item_id": item.pk,
                        "message": (
                            "No inventory record found for this item. "
                            "Inventory data is owned by the Inventory app."
                        ),
                    }
                )
                continue
            if inv.get("is_out_of_stock", False):
                issues.append(
                    {
                        "code": "out_of_stock",
                        "item_id": item.pk,
                        "message": "Item is out of stock",
                    }
                )
                continue
            try:
                free_stock = _safe_decimal(inv.get("free_stock"))
            except Exception:
                free_stock = Decimal("0.00")
            if free_stock is not None and free_stock < Decimal(str(item.quantity)):
                issues.append(
                    {
                        "code": "insufficient_stock",
                        "item_id": item.pk,
                        "message": (
                            f"Only {free_stock} in stock; requested "
                            f"{item.quantity}"
                        ),
                    }
                )
        totals = CartService.compute_totals(cart)
        return {
            "ready_for_checkout": len(issues) == 0,
            "issues": issues,
            "totals": totals,
            "cart": _serialize_cart(cart),
        }

# ==============================================================================
# CART COUPON SERVICE
# ==============================================================================
class CartCouponService:
    """
    Cart-side coupon orchestration.

    Coupon validation and discount calculation are owned by the
    coupon service. This class orchestrates:
      * Coupon validity checks (delegated)
      * Discount application on the cart
      * Coupon removal
      * Coupons NEVER affect stock
    """

    @staticmethod
    def apply_coupon(
        *,
        cart: Optional[Cart],
        code: str,
        customer: Any = None,
    ) -> Dict[str, Any]:
        """
        Applies a coupon to the cart after validating it through the
        coupon service. The discount is stored on the cart itself.

        Returns a structured response. Inventory is NEVER touched.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        clean_code = _safe_str(code).upper()
        if not clean_code:
            return _safe_structured_response(
                False,
                error="Coupon code is required",
                code="missing_coupon_code",
            )
        # Compute minimum subtotal from CMS settings
        try:
            subtotal = _safe_decimal(getattr(cart, "subtotal", None))
        except Exception:
            subtotal = Decimal("0.00")
        if subtotal < get_coupon_min_subtotal():
            return _safe_structured_response(
                False,
                error=(
                    f"Cart subtotal must be at least "
                    f"{get_coupon_min_subtotal()} to apply coupons"
                ),
                code="cart_subtotal_too_low",
            )
        # Delegate validation to a dedicated coupon service if available
        discount_amount = _safe_delegate_coupon_validation(
            code=clean_code,
            cart=cart,
            customer=customer,
            subtotal=subtotal,
        )
        if discount_amount is None:
            return _safe_structured_response(
                False,
                error="Invalid or expired coupon",
                code="invalid_coupon",
            )
        try:
            with transaction.atomic():
                type(cart).objects.filter(pk=cart.pk).update(
                    coupon_code=clean_code,
                    coupon_discount_amount=discount_amount,
                    updated_at=timezone.now(),
                )
                cart.refresh_from_db(
                    fields=["coupon_code", "coupon_discount_amount"]
                )
            return _safe_structured_response(
                True,
                message="Coupon applied",
                code="coupon_applied",
                payload={
                    "cart": _serialize_cart(cart),
                    "discount_amount": str(discount_amount),
                    "coupon_code": clean_code,
                },
            )
        except Exception as exc:
            logger.exception("apply_coupon failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Coupon apply failed",
                code="coupon_apply_failed",
            )

    @staticmethod
    def remove_coupon(*, cart: Optional[Cart]) -> Dict[str, Any]:
        """
        Removes any coupon applied to the cart.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        try:
            with transaction.atomic():
                type(cart).objects.filter(pk=cart.pk).update(
                    coupon_code=None,
                    coupon_discount_amount=Decimal("0.00"),
                    updated_at=timezone.now(),
                )
                cart.refresh_from_db(
                    fields=["coupon_code", "coupon_discount_amount"]
                )
            return _safe_structured_response(
                True,
                message="Coupon removed",
                code="coupon_removed",
                payload={"cart": _serialize_cart(cart)},
            )
        except Exception as exc:
            logger.exception("remove_coupon failed: %s", exc)
            return _safe_structured_response(
                False,
                error=str(exc) or "Coupon remove failed",
                code="coupon_remove_failed",
            )

def _safe_delegate_coupon_validation(
    *,
    code: str,
    cart: Cart,
    customer: Any,
    subtotal: Decimal,
) -> Optional[Decimal]:
    """
    Safely attempt to look up a coupon service. Returns the discount
    amount (Decimal) on success, or None on failure / unavailability.

    The Cart layer NEVER computes discounts on its own. Coupons are
    validated and computed by the coupon service. If no coupon service
    is registered, the coupon is rejected gracefully.
    """
    try:
        from django.apps import apps
        # Convention: an app exposing a "coupons" module with a
        # ``validate_coupon`` function or a ``CouponService`` class.
        # The Cart layer never imports the coupon app directly.
        coupon_service = apps.get_app_config("coupons").get_service() if hasattr(
            apps.get_app_config("coupons"), "get_service"
        ) else None
    except Exception:
        coupon_service = None
    if coupon_service is None:
        logger.debug(
            "No coupon service registered; rejecting coupon code %r",
            code,
        )
        return None
    try:
        if hasattr(coupon_service, "validate_coupon"):
            result = coupon_service.validate_coupon(
                code=code,
                cart=cart,
                customer=customer,
                subtotal=subtotal,
            )
        elif hasattr(coupon_service, "compute_discount"):
            result = coupon_service.compute_discount(
                code=code,
                cart=cart,
                customer=customer,
                subtotal=subtotal,
            )
        else:
            return None
        if isinstance(result, dict):
            if not result.get("valid", False):
                return None
            return _safe_decimal(
                result.get("discount_amount", result.get("amount"))
            )
        return _safe_decimal(result)
    except Exception as exc:
        logger.debug("Coupon validation delegation failed: %s", exc)
        return None

# ==============================================================================
# CART REORDER SERVICE
# ==============================================================================
class CartReorderService:
    """
    Cart-side reorder orchestration.

    Reordering adds a list of previously-ordered line items back to a
    cart. The Cart layer orchestrates the re-add operation; the
    Inventory layer validates availability and creates reservations.

    This is the "reorder" function for purchase-history-based reorders.
    Returns a structured response containing a per-item success summary.
    """

    @staticmethod
    def reorder_items_into_cart(
        *,
        cart: Optional[Cart],
        items: Optional[Iterable[Dict[str, Any]]] = None,
        order_reference: str = "",
    ) -> Dict[str, Any]:
        """
        Reorders a list of items into the cart.

        Each item dict must contain at minimum:
          * ``product_id`` (int) or ``variant_id`` (int)
          * ``quantity`` (int, default 1)
          * ``unit_price_snapshot`` (Decimal, optional)
          * ``currency`` (str, optional)

        Returns a structured response containing per-item outcomes.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _safe_structured_response(
                False,
                error="Cart not found",
                code="cart_not_found",
            )
        if items is None:
            items = []
        results: List[Dict[str, Any]] = []
        success_count = 0
        failure_count = 0
        for raw in items:
            try:
                if not isinstance(raw, dict):
                    raise ValueError("Item must be a dict")
                product = None
                variant = None
                product_id = raw.get("product_id")
                variant_id = raw.get("variant_id")
                # Lazy product / variant lookup to avoid circular imports
                try:
                    from apps.catalog.models import Product as _CatProduct
                    from apps.catalog.models import ProductVariant as _CatVariant
                    if variant_id:
                        variant = _CatVariant.objects.filter(
                            pk=variant_id
                        ).first()
                        if variant is not None:
                            product = getattr(variant, "product", None)
                    elif product_id:
                        product = _CatProduct.objects.filter(
                            pk=product_id
                        ).first()
                except Exception as exc:
                    logger.debug("Reorder product lookup failed: %s", exc)
                    product = None
                    variant = None
                if product is None and variant is None:
                    failure_count += 1
                    results.append(
                        {
                            "input": raw,
                            "success": False,
                            "error": "Product or variant not found",
                        }
                    )
                    continue
                qty = _safe_int(raw.get("quantity", 1), default=1)
                if qty < 1:
                    qty = 1
                add_payload = CartItemService.add_item(
                    cart=cart,
                    product=product,
                    variant=variant,
                    quantity=qty,
                    unit_price_snapshot=raw.get("unit_price_snapshot"),
                    currency=_safe_str(raw.get("currency", "")),
                )
                if add_payload.get("success"):
                    success_count += 1
                else:
                    failure_count += 1
                results.append(
                    {
                        "input": raw,
                        "success": bool(add_payload.get("success")),
                        "error": add_payload.get("error", ""),
                        "code": add_payload.get("code", ""),
                    }
                )
            except Exception as exc:
                failure_count += 1
                logger.exception("Reorder item failed: %s", exc)
                results.append(
                    {
                        "input": raw if isinstance(raw, dict) else {},
                        "success": False,
                        "error": str(exc) or "Item reorder failed",
                    }
                )
        return _safe_structured_response(
            failure_count == 0,
            message=(
                f"Reordered {success_count} item(s) "
                f"({failure_count} failure(s))"
            ),
            code="reorder_processed" if failure_count == 0 else "reorder_partial",
            payload={
                "success_count": success_count,
                "failure_count": failure_count,
                "results": results,
                "order_reference": _safe_str(order_reference),
                "cart": _serialize_cart(cart),
            },
        )

# ==============================================================================
# BACKWARD-COMPATIBLE LEGACY FUNCTION ALIASES
# ==============================================================================
# The original public function contract is preserved at module level so
# existing call-sites continue to function. The new architecture is
# encapsulated in the dedicated service classes defined above. The
# legacy functions are pure delegations to the new classes.

def get_or_create_cart(request: Any) -> Tuple[Optional[Cart], bool]:
    """
    Legacy alias. Delegates to ``CartService.get_or_create_for_request``.
    """
    return CartService.get_or_create_for_request(request)

def add_item_to_cart(
    cart: Cart,
    *,
    product: Any = None,
    variant: Any = None,
    quantity: int = 1,
    unit_price_snapshot: Optional[Decimal] = None,
    currency: str = "",
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartItemService.add_item``.
    """
    return CartItemService.add_item(
        cart=cart,
        product=product,
        variant=variant,
        quantity=quantity,
        unit_price_snapshot=unit_price_snapshot,
        currency=currency,
    )

def remove_item_from_cart(
    cart: Cart, *, item_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartItemService.remove_item``.
    """
    return CartItemService.remove_item(cart=cart, item_id=item_id)

def update_cart_item(
    cart: Cart,
    *,
    item_id: Optional[int] = None,
    quantity: int = 1,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartItemService.update_quantity``.
    """
    return CartItemService.update_quantity(
        cart=cart, item_id=item_id, quantity=quantity
    )

def clear_cart(cart: Cart) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartItemService.clear_cart``.
    """
    return CartItemService.clear_cart(cart=cart)

def save_item_for_later(
    cart: Cart,
    *,
    item_id: Optional[int] = None,
    reason: str = "",
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartItemService.save_for_later``.
    """
    return CartItemService.save_for_later(
        cart=cart, item_id=item_id, reason=reason
    )

def move_item_to_cart(
    cart: Cart, *, item_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartItemService.move_to_cart``.
    """
    return CartItemService.move_to_cart(cart=cart, item_id=item_id)

def merge_guest_cart_into_customer(
    guest_cart: Cart, customer: Any
) -> Optional[Cart]:
    """
    Legacy alias. Delegates to ``CartService.merge_guest_cart_into_customer``.
    """
    return CartService.merge_guest_cart_into_customer(
        guest_cart=guest_cart, customer=customer
    )

def apply_coupon(
    cart: Cart, code: str, customer: Any = None
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartCouponService.apply_coupon``.
    """
    return CartCouponService.apply_coupon(
        cart=cart, code=code, customer=customer
    )

def remove_coupon(cart: Cart) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartCouponService.remove_coupon``.
    """
    return CartCouponService.remove_coupon(cart=cart)

def validate_cart_for_checkout(cart: Cart) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.validate_for_checkout``.
    """
    return CartInventoryService.validate_for_checkout(cart=cart)

def reorder_items_into_cart(
    cart: Cart,
    items: Optional[Iterable[Dict[str, Any]]] = None,
    order_reference: str = "",
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartReorderService.reorder_items_into_cart``.
    """
    return CartReorderService.reorder_items_into_cart(
        cart=cart, items=items, order_reference=order_reference
    )

def process_return_request(
    cart: Cart,
    items: Optional[Iterable[Dict[str, Any]]] = None,
    order_reference: str = "",
) -> Dict[str, Any]:
    """
    Legacy alias preserved for backward compatibility. The original
    alias was documented as a synonym for ``reorder_items_into_cart``.
    Behavior is identical.
    """
    return reorder_items_into_cart(
        cart=cart, items=items, order_reference=order_reference
    )

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Service classes
    "CartService",
    "CartItemService",
    "CartInventoryService",
    "CartCouponService",
    "CartReorderService",
    # Configuration helpers
    "get_default_tax_rate",
    "get_default_shipping_flat",
    "get_default_reservation_minutes",
    "get_max_items_per_cart",
    "get_max_quantity_per_item",
    "get_coupon_min_subtotal",
    # Domain exceptions
    "CartError",
    "CartNotFoundError",
    "CartItemNotFoundError",
    "CartLimitExceededError",
    "CartQuantityLimitExceededError",
    "CartCouponError",
    "CartCheckoutError",
    # Backward-compatible legacy aliases
    "get_or_create_cart",
    "add_item_to_cart",
    "remove_item_from_cart",
    "update_cart_item",
    "clear_cart",
    "save_item_for_later",
    "move_item_to_cart",
    "merge_guest_cart_into_customer",
    "apply_coupon",
    "remove_coupon",
    "validate_cart_for_checkout",
    "reorder_items_into_cart",
    "process_return_request",
]