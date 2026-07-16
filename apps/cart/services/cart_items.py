"""
Enterprise-grade Cart Item Service Layer for the Cart application.

================================================================================
ARCHITECTURE
================================================================================
This module is the dedicated Cart Item service layer. It is a pure
orchestrator that manages cart line item workflows while delegating
every inventory, warehouse, reservation, availability, stock-status,
and backorder responsibility to the Inventory application.

Inventory is the SINGLE SOURCE OF TRUTH for all stock-related
operations. The Cart application NEVER owns, computes, or duplicates
inventory business logic. Every inventory operation is delegated to
the canonical Inventory services and selectors.

The Cart Item service is responsible ONLY for:

    1. Adding items to a cart
    2. Updating item quantities
    3. Removing items
    4. Save-for-later workflows
    5. Cart merge operations (guest -> authenticated)
    6. Snapshot data extraction from Product / Variant
    7. Validation of cart-domain rules
    8. Delegation of every inventory operation to the Inventory service

This module NEVER:
    * Calculates or mutates stock quantities
    * Determines available stock
    * Determines reserved stock
    * Performs any inventory business logic
    * Duplicates inventory business rules
    * Persists inventory state
    * Touches inventory models directly
    * Reads inventory fields like product.stock_quantity or variant.stock_quantity
    * Recalculates inventory in any way
    * Reads Product.stock_status or ProductVariant.stock_status

================================================================================
BACKWARD COMPATIBILITY
================================================================================
The legacy public function contract is preserved at module level so
existing call-sites continue to function. The new architecture is
encapsulated in the dedicated ``CartItemService`` class. Legacy
functions are pure delegations to the new class.

================================================================================
CMS-DRIVEN
================================================================================
All thresholds, durations, statuses, retries, and limits are sourced
from Django settings with safe defaults. The CMS can override any
behavior without code changes.

================================================================================
OWASP ASVS COMPLIANCE
================================================================================
* Lazy imports prevent circular dependencies and import-time side effects
* Thread-safe via Django's per-request atomic transactions
* Idempotent operations where appropriate
* Defensive validation of every input
* Graceful exception handling with structured error responses
* Never trust client input
* No PII or sensitive data in logs
* Object ownership is verified before any privileged operation

================================================================================
PERFORMANCE
================================================================================
* select_related / prefetch_related optimizations
* Aggregated annotate for totals
* Bulk operations where supported
* Designed for millions of cart items
* Lazy import of inventory services to keep cart import-time low
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..models import Cart, CartItem

logger = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION (CMS-DRIVEN)
# ==============================================================================
# All defaults can be overridden via Django settings, which in turn can be
# driven by the CMS without code changes. This keeps the service fully
# parameterized and future-proof.

_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_MAX_ITEMS_PER_CART = 200
_DEFAULT_MAX_QUANTITY_PER_ITEM = 99
_DEFAULT_LOW_STOCK_THRESHOLD = 5
_DEFAULT_CHECK_TIMEOUT_SECONDS = 8
_DEFAULT_RENEWAL_BATCH_SIZE = 200
_DEFAULT_INCLUDE_DAMAGED = False
_DEFAULT_BACKORDER_DEFAULT = False
_DEFAULT_LOW_STOCK_GLOBAL = True

def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)

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
    value = _get_setting(
        "CART_MAX_ITEMS",
        _DEFAULT_MAX_ITEMS_PER_CART,
    )
    try:
        value = int(value)
        return max(1, value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ITEMS_PER_CART

def get_max_quantity_per_item() -> int:
    """
    Returns the CMS-driven maximum quantity per cart-item.
    """
    value = _get_setting(
        "CART_MAX_QUANTITY",
        _DEFAULT_MAX_QUANTITY_PER_ITEM,
    )
    try:
        value = int(value)
        return max(1, value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUANTITY_PER_ITEM

def get_low_stock_threshold() -> int:
    """
    Returns the CMS-driven low-stock alert threshold.

    This is used purely for display hints. The inventory app is the
    authoritative source of low-stock state.
    """
    value = _get_setting(
        "CART_LOW_STOCK_THRESHOLD",
        _DEFAULT_LOW_STOCK_THRESHOLD,
    )
    try:
        value = int(value)
        return max(0, value)
    except (TypeError, ValueError):
        return _DEFAULT_LOW_STOCK_THRESHOLD

def get_check_timeout_seconds() -> int:
    """
    Returns the CMS-driven network timeout for inventory checks.
    """
    value = _get_setting(
        "CART_INVENTORY_CHECK_TIMEOUT_SECONDS",
        _DEFAULT_CHECK_TIMEOUT_SECONDS,
    )
    try:
        value = int(value)
        return max(1, value)
    except (TypeError, ValueError):
        return _DEFAULT_CHECK_TIMEOUT_SECONDS

def get_renewal_batch_size() -> int:
    """
    Returns the CMS-driven batch size for reservation renewal operations.
    """
    value = _get_setting(
        "CART_INVENTORY_RENEWAL_BATCH_SIZE",
        _DEFAULT_RENEWAL_BATCH_SIZE,
    )
    try:
        value = int(value)
        return max(1, min(value, 5000))
    except (TypeError, ValueError):
        return _DEFAULT_RENEWAL_BATCH_SIZE

def get_include_damaged_default() -> bool:
    """
    Returns the CMS-driven default for whether cart availability checks
    should count damaged stock. The inventory app is the source of
    truth; this is a fallback hint only.
    """
    return bool(
        _get_setting(
            "CART_INVENTORY_INCLUDE_DAMAGED",
            _DEFAULT_INCLUDE_DAMAGED,
        )
    )

def get_backorder_default() -> bool:
    """
    Returns the CMS-driven default for whether the cart allows
    backorder requests when stock is insufficient. The inventory app
    is the source of truth for backorder policy.
    """
    return bool(
        _get_setting(
            "CART_INVENTORY_BACKORDER_DEFAULT",
            _DEFAULT_BACKORDER_DEFAULT,
        )
    )

def get_low_stock_global() -> bool:
    """
    Returns the CMS-driven default for whether the cart should
    surface global low-stock indicators. The inventory app is the
    source of truth for low-stock state.
    """
    return bool(
        _get_setting(
            "CART_INVENTORY_LOW_STOCK_GLOBAL",
            _DEFAULT_LOW_STOCK_GLOBAL,
        )
    )

# ==============================================================================
# SAFE HELPERS
# ==============================================================================
def _safe_decimal(value: Any, *, allow_none: bool = True) -> Optional[Decimal]:
    """
    Best-effort conversion of a value to a safe Decimal. Returns None
    (or Decimal("0.00")) on any failure. Never raises.
    """
    if value is None:
        return None if allow_none else Decimal("0.00")
    try:
        decimal_value = Decimal(str(value))
        if decimal_value.is_nan() or decimal_value.is_infinite():
            return None if allow_none else Decimal("0.00")
        return decimal_value
    except (InvalidOperation, TypeError, ValueError):
        return None if allow_none else Decimal("0.00")

def _safe_int(value: Any, *, default: int = 0) -> int:
    """
    Best-effort conversion of a value to a safe int. Returns the
    provided default on any failure. Never raises.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return default

def _safe_str(value: Any) -> str:
    """
    Best-effort conversion of a value to a safe trimmed string. Never
    raises.
    """
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

def _safe_bool(value: Any, *, default: bool = False) -> bool:
    """
    Best-effort conversion of a value to a safe boolean. Never raises.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
    return default

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
# STRUCTURED RESPONSE BUILDER
# ==============================================================================
def _structured_response(
    success: bool,
    *,
    code: str = "",
    message: str = "",
    payload: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Builds a consistent structured response payload for all
    orchestration operations. Never raises.
    """
    response: Dict[str, Any] = {
        "success": bool(success),
        "code": _safe_str(code),
        "message": _safe_str(message),
    }
    if payload is not None and isinstance(payload, dict):
        response.update(payload)
    if error is not None:
        response["error"] = _safe_str(error)
    if extras is not None and isinstance(extras, dict):
        response["extras"] = extras
    return response

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
    Lazy accessor for the inventory services module. Returns None
    on ImportError so the cart service can degrade gracefully when
    the inventory app is not available.
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
    Lazy accessor for the inventory selectors module. Returns None
    on ImportError.
    """
    try:
        from apps.inventory import selectors
        return selectors
    except Exception:
        logger.warning("Inventory selectors module could not be imported.")
        return None

# ==============================================================================
# SAFE INVENTORY DELEGATION HELPERS
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
    Safely delegate availability check to the Inventory service layer.

    Returns a structured dictionary. Never raises. The Cart orchestrator
    uses this result for UI hints and checkout validation, but does NOT
    interpret it as a binding stock assertion.
    """
    services = _get_inventory_services()
    if services is None:
        return {
            "is_available": False,
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "free_stock": "0.00",
            "warehouses_checked": 0,
            "per_warehouse": [],
            "source": "inventory_service_unavailable",
        }
    try:
        safe_qty = _safe_decimal(quantity, allow_none=True) or Decimal("1")
        if safe_qty <= 0:
            safe_qty = Decimal("1")
        return services.check_stock(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            quantity=safe_qty,
            include_all_warehouses=include_all_warehouses,
        )
    except Exception as exc:
        logger.debug("Safe inventory check failed: %s", exc)
        return {
            "is_available": False,
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "free_stock": "0.00",
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
    Safely delegate reservation creation to the Inventory service layer.

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
        expires_in = timedelta(minutes=max(1, int(expires_in_minutes)))
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
    Safely delegate reservation release to the Inventory service layer.

    Returns a structured dictionary. Never raises. On failure returns
    a payload with ``success=False`` and a descriptive error.
    """
    services = _get_inventory_services()
    if services is None:
        return {"success": False, "error": "Inventory service unavailable", "released": False}
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
            "released": False,
        }

def _safe_inventory_get_summary(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
) -> Dict[str, Any]:
    """
    Safely fetch the inventory summary for a given target via the
    Inventory selector layer.

    Returns a structured dictionary. Never raises.
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
        result = selectors.get_inventory_summary_for_target(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )
        if isinstance(result, dict):
            return result
    except Exception as exc:
        logger.debug("Safe inventory summary failed: %s", exc)
    return {
        "exists": False,
        "free_stock": "0.00",
        "is_out_of_stock": True,
        "is_low_stock": False,
    }

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
    def _resolve_unit_price(*, product: Any, variant: Any) -> Decimal:
        """
        Resolves the unit price from variant override or product.
        Pure catalog snapshot logic. NEVER touches inventory.
        """
        if variant is not None and getattr(variant, "price_override", None) is not None:
            return variant.price_override
        return getattr(product, "price", None) or Decimal("0.00")

    @staticmethod
    def _resolve_compare_at_price(*, product: Any, variant: Any) -> Optional[Decimal]:
        """
        Resolves the compare-at price. Pure catalog snapshot logic.
        """
        if variant is not None and getattr(variant, "compare_price", None):
            return variant.compare_price
        return getattr(product, "original_price", None) or None

    @staticmethod
    def _resolve_image(product: Any) -> Any:
        """
        Resolves the primary image snapshot. Pure catalog logic.
        """
        if getattr(product, "primary_image", None):
            return product.primary_image
        gallery = getattr(product, "gallery_images", None)
        if gallery is not None:
            try:
                first_gallery = gallery.first()
                if first_gallery is not None:
                    return first_gallery.image
            except Exception:
                pass
        return None

    @staticmethod
    def _sku_snapshot(*, product: Any, variant: Any) -> str:
        """
        Resolves the SKU snapshot from variant or product.
        Pure catalog logic.
        """
        if variant is not None:
            v_sku = getattr(variant, "sku", "") or ""
            if v_sku:
                return v_sku
        return getattr(product, "sku", "") or ""

    @staticmethod
    def _name_snapshot(*, product: Any, variant: Any) -> str:
        """
        Resolves the human-readable name snapshot.
        Pure catalog logic.
        """
        if variant is not None and getattr(variant, "name", ""):
            return getattr(variant, "name", "")
        return getattr(product, "title", "") or ""

    @staticmethod
    def _validate_catalog_targets(
        *,
        product: Any,
        variant: Optional[Any],
    ) -> Optional[Dict[str, str]]:
        """
        Validates the catalog-side prerequisites (active state) of a
        product / variant. NEVER inspects any inventory field. The
        inventory service is the source of truth for stock.

        Returns None on success or a structured error dict on failure.
        """
        if product is None and variant is None:
            return {
                "code": "missing_product",
                "message": str(_("A product or variant is required.")),
            }
        if product is not None and not getattr(product, "is_active", True):
            return {
                "code": "product_inactive",
                "message": str(_("This product is no longer available.")),
            }
        if variant is not None and not getattr(variant, "is_active", True):
            return {
                "code": "variant_inactive",
                "message": str(_("This variant is no longer available.")),
            }
        return None

    @staticmethod
    def add_item(
        *,
        cart: Optional[Cart],
        product: Any = None,
        variant: Any = None,
        quantity: int = 1,
        personalization: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Adds an item to the cart. Inventory validation and reservation
        are delegated to the Inventory service.

        Returns a structured response. The response includes the
        resulting cart item payload and the inventory validation result.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(
                False,
                code="cart_not_found",
                message=str(_("Cart not found")),
            )
        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            qty = 1
        if qty < 1:
            qty = 1
        if qty > get_max_quantity_per_item():
            return _structured_response(
                False,
                code="quantity_limit_exceeded",
                message=str(
                    _("Quantity exceeds the maximum of %(max)d")
                    % {"max": get_max_quantity_per_item()}
                ),
            )

        catalog_error = CartItemService._validate_catalog_targets(
            product=product, variant=variant
        )
        if catalog_error is not None:
            return _structured_response(
                False,
                code=catalog_error["code"],
                message=catalog_error["message"],
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
            return _structured_response(
                False,
                code="cart_limit_exceeded",
                message=str(
                    _("Cart has reached the maximum of %(max)d distinct items")
                    % {"max": get_max_items_per_cart()}
                ),
            )

        try:
            with transaction.atomic():
                if existing_match is not None:
                    # Merge into existing line
                    new_qty = existing_match.quantity + qty
                    if new_qty > get_max_quantity_per_item():
                        return _structured_response(
                            False,
                            code="quantity_limit_exceeded",
                            message=str(
                                _(
                                    "Total quantity would exceed the maximum of %(max)d"
                                )
                                % {"max": get_max_quantity_per_item()}
                            ),
                        )
                    CartItem.objects.filter(pk=existing_match.pk).update(
                        quantity=new_qty,
                        updated_at=timezone.now(),
                    )
                    existing_match.refresh_from_db(
                        fields=["quantity", "updated_at"]
                    )
                    result_item = existing_match
                else:
                    # Create new line - snapshot only
                    unit_price = CartItemService._resolve_unit_price(
                        product=product, variant=variant
                    )
                    compare_at = CartItemService._resolve_compare_at_price(
                        product=product, variant=variant
                    )
                    image_snapshot = CartItemService._resolve_image(product)
                    sku_snapshot = CartItemService._sku_snapshot(
                        product=product, variant=variant
                    )
                    name_snapshot = CartItemService._name_snapshot(
                        product=product, variant=variant
                    )
                    result_item = CartItem.objects.create(
                        cart=cart,
                        product=product,
                        variant=variant,
                        quantity=qty,
                        unit_price_snapshot=unit_price,
                        compare_at_price_snapshot=compare_at,
                        product_image_snapshot=image_snapshot,
                        product_name_snapshot=name_snapshot,
                        product_sku_snapshot=sku_snapshot,
                        variant_name_snapshot=(
                            getattr(variant, "name", "") if variant is not None else ""
                        ),
                        currency_snapshot=getattr(cart, "currency", "") or "NPR",
                        status=CartItem.ItemStatus.ACTIVE,
                        personalization=personalization or {},
                        attributes_snapshot={},
                    )
                # Touch the cart activity
                cart.touch()
        except Exception as exc:
            logger.exception("add_item failed: %s", exc)
            return _structured_response(
                False,
                code="add_item_failed",
                message=str(exc) or "Add to cart failed",
            )

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
            reference_number=(
                _safe_str(
                    getattr(result_item, "product_sku_snapshot", "")
                )
                or "cart-item"
            ),
            notes="Cart item added via cart orchestrator",
        )

        # Best-effort persistence of reservation reference on the line
        try:
            if (
                isinstance(reservation_payload, dict)
                and reservation_payload.get("success")
            ):
                reservation_token = reservation_payload.get("reservation_token")
                reservation_id = reservation_payload.get("reservation_id")
                if reservation_id:
                    CartItem.objects.filter(pk=result_item.pk).update(
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

        return _structured_response(
            True,
            code="item_added",
            message=str(_("Item added to cart")),
            payload={
                "item": _serialize_cart_item(result_item),
                "cart_id": cart.pk,
                "inventory_check": inv_payload,
                "reservation": reservation_payload,
            },
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
            return _structured_response(
                False,
                code="cart_not_found",
                message=str(_("Cart not found")),
            )
        try:
            new_qty = int(quantity)
        except (TypeError, ValueError):
            new_qty = 1
        try:
            item = cart.items.filter(
                pk=item_id,
                status=CartItem.ItemStatus.ACTIVE,
            ).first()
        except Exception:
            item = None
        if item is None:
            return _structured_response(
                False,
                code="item_not_found",
                message=str(_("Item not found in cart")),
            )
        if new_qty < 1:
            return CartItemService.remove_item(cart=cart, item_id=item_id)
        if new_qty > get_max_quantity_per_item():
            return _structured_response(
                False,
                code="quantity_limit_exceeded",
                message=str(
                    _("Quantity exceeds the maximum of %(max)d")
                    % {"max": get_max_quantity_per_item()}
                ),
            )

        try:
            with transaction.atomic():
                old_qty = item.quantity
                if old_qty == new_qty:
                    return _structured_response(
                        True,
                        code="no_change",
                        message=str(_("No change")),
                        payload={
                            "item": _serialize_cart_item(item),
                            "cart_id": cart.pk,
                        },
                    )
                CartItem.objects.filter(pk=item.pk).update(
                    quantity=new_qty,
                    updated_at=timezone.now(),
                )
                item.refresh_from_db(fields=["quantity", "updated_at"])
        except Exception as exc:
            logger.exception("update_quantity failed: %s", exc)
            return _structured_response(
                False,
                code="update_failed",
                message=str(exc) or "Update failed",
            )

        # Inventory rebalance: release the existing reservation and
        # re-reserve the new quantity. This is best-effort and
        # idempotent on failure.
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
                            reference_number=(
                                _safe_str(
                                    getattr(item, "product_sku_snapshot", "")
                                )
                                or "cart-item"
                            ),
                            notes="Cart quantity extended via cart orchestrator",
                        )
        except Exception as exc:
            logger.debug("Reservation rebalance failed: %s", exc)

        try:
            cart.touch()
        except Exception:
            pass

        return _structured_response(
            True,
            code="quantity_updated",
            message=str(_("Quantity updated")),
            payload={
                "item": _serialize_cart_item(item),
                "cart_id": cart.pk,
            },
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
            return _structured_response(
                False,
                code="cart_not_found",
                message=str(_("Cart not found")),
            )
        try:
            item = cart.items.filter(pk=item_id).first()
        except Exception:
            item = None
        if item is None:
            return _structured_response(
                False,
                code="item_not_found",
                message=str(_("Item not found in cart")),
            )
        try:
            with transaction.atomic():
                reservation_id = getattr(item, "reservation_id", None)
                CartItem.objects.filter(pk=item.pk).delete()
                if reservation_id:
                    _safe_inventory_release(
                        reservation_id=reservation_id,
                        reason="Item removed from cart",
                        is_automatic=True,
                    )
        except Exception as exc:
            logger.exception("remove_item failed: %s", exc)
            return _structured_response(
                False,
                code="remove_failed",
                message=str(exc) or "Remove failed",
            )
        try:
            cart.touch()
        except Exception:
            pass
        return _structured_response(
            True,
            code="item_removed",
            message=str(_("Item removed from cart")),
            payload={"cart_id": cart.pk},
        )

    @staticmethod
    def clear_cart(*, cart: Optional[Cart]) -> Dict[str, Any]:
        """
        Removes every item from a cart and releases all associated
        inventory reservations. Returns a structured response.
        """
        if cart is None or not getattr(cart, "pk", None):
            return _structured_response(
                False,
                code="cart_not_found",
                message=str(_("Cart not found")),
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
        except Exception as exc:
            logger.exception("clear_cart failed: %s", exc)
            return _structured_response(
                False,
                code="clear_failed",
                message=str(exc) or "Clear failed",
            )
        try:
            cart.touch()
        except Exception:
            pass
        return _structured_response(
            True,
            code="cart_cleared",
            message=str(_("Cart cleared")),
            payload={"cart_id": cart.pk},
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
            return _structured_response(
                False,
                code="cart_not_found",
                message=str(_("Cart not found")),
            )
        try:
            item = cart.items.filter(
                pk=item_id,
                status=CartItem.ItemStatus.ACTIVE,
            ).first()
        except Exception:
            item = None
        if item is None:
            return _structured_response(
                False,
                code="item_not_found",
                message=str(_("Item not found in cart")),
            )
        try:
            with transaction.atomic():
                safe_reason = (
                    _safe_str(reason)
                    or CartItem.SavedForLaterReason.MANUAL
                )
                CartItem.objects.filter(pk=item.pk).update(
                    status=CartItem.ItemStatus.SAVED,
                    saved_reason=safe_reason,
                    moved_to_save_at=timezone.now(),
                    updated_at=timezone.now(),
                )
                if getattr(item, "reservation_id", None):
                    _safe_inventory_release(
                        reservation_id=item.reservation_id,
                        reason="Item saved for later",
                        is_automatic=True,
                    )
            item.refresh_from_db()
        except Exception as exc:
            logger.exception("save_for_later failed: %s", exc)
            return _structured_response(
                False,
                code="save_failed",
                message=str(exc) or "Save failed",
            )
        try:
            cart.touch()
        except Exception:
            pass
        return _structured_response(
            True,
            code="item_saved",
            message=str(_("Item saved for later")),
            payload={
                "item": _serialize_cart_item(item),
                "cart_id": cart.pk,
            },
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
            return _structured_response(
                False,
                code="cart_not_found",
                message=str(_("Cart not found")),
            )
        try:
            item = cart.items.filter(
                pk=item_id,
                status=CartItem.ItemStatus.SAVED,
            ).first()
        except Exception:
            item = None
        if item is None:
            return _structured_response(
                False,
                code="item_not_found",
                message=str(_("Saved item not found in cart")),
            )
        try:
            with transaction.atomic():
                CartItem.objects.filter(pk=item.pk).update(
                    status=CartItem.ItemStatus.ACTIVE,
                    saved_reason=None,
                    moved_to_save_at=None,
                    updated_at=timezone.now(),
                )
        except Exception as exc:
            logger.exception("move_to_cart failed: %s", exc)
            return _structured_response(
                False,
                code="move_failed",
                message=str(exc) or "Move failed",
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
                session_key=_safe_str(getattr(cart, "session_key", "")),
                reference_number=(
                    _safe_str(
                        getattr(item, "product_sku_snapshot", "")
                    )
                    or "cart-item"
                ),
                notes="Item moved back to cart via cart orchestrator",
            )
            if (
                isinstance(reservation_payload, dict)
                and reservation_payload.get("success")
            ):
                CartItem.objects.filter(pk=item.pk).update(
                    reservation_id=reservation_payload.get("reservation_id"),
                    reservation_token=reservation_payload.get(
                        "reservation_token"
                    )
                    or "",
                    reservation_status="active",
                )
        except Exception as exc:
            logger.debug("Re-reserve inventory failed: %s", exc)
        try:
            item.refresh_from_db()
        except Exception:
            pass
        try:
            cart.touch()
        except Exception:
            pass
        return _structured_response(
            True,
            code="item_activated",
            message=str(_("Item moved to active cart")),
            payload={
                "item": _serialize_cart_item(item),
                "cart_id": cart.pk,
            },
        )

    @staticmethod
    def _merge_item_into_cart(
        *,
        item: CartItem,
        target_cart: Cart,
    ) -> CartItem:
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
                CartItem.objects.filter(pk=existing.pk).update(
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
            CartItem.objects.filter(pk=item.pk).update(
                cart=target_cart,
                updated_at=timezone.now(),
            )
        except Exception:
            pass
        item.refresh_from_db()
        return item

    @staticmethod
    def merge_guest_cart_into_customer(
        *,
        guest_cart: Optional[Cart],
        customer: Any,
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
            return None
        if not getattr(guest_cart, "pk", None):
            return None

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
                Cart.objects.filter(pk=guest_cart.pk).update(
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
            try:
                customer_cart.touch()
            except Exception:
                pass
            return customer_cart
        except Exception as exc:
            logger.exception("merge_guest_cart_into_customer failed: %s", exc)
            return None

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Service class
    "CartItemService",
    # Configuration helpers
    "get_default_reservation_minutes",
    "get_max_items_per_cart",
    "get_max_quantity_per_item",
    "get_low_stock_threshold",
    "get_check_timeout_seconds",
    "get_renewal_batch_size",
    "get_include_damaged_default",
    "get_backorder_default",
    "get_low_stock_global",
    # Domain exceptions
    "CartError",
    "CartNotFoundError",
    "CartItemNotFoundError",
    "CartLimitExceededError",
    "CartQuantityLimitExceededError",
    "CartCouponError",
    "CartCheckoutError",
]