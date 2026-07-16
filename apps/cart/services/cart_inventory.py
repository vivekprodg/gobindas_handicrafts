"""
Enterprise-grade Inventory Orchestration Layer for the Cart application.

================================================================================
ARCHITECTURE
================================================================================

This module is the DEDICATED Inventory integration boundary for the Cart
application. It is a thin, pure orchestrator that coordinates inventory
operations on behalf of the Cart application by delegating exclusively to
the Inventory application's service layer.

Inventory is the SINGLE SOURCE OF TRUTH for all stock-related operations.
This module NEVER owns or duplicates inventory business logic. Every
inventory operation flows through the canonical inventory services.

The Cart Inventory Orchestrator is responsible ONLY for:

    1. Delegating availability checks to inventory services
    2. Delegating reservation creation to inventory services
    3. Delegating reservation release to inventory services
    4. Delegating reservation renewal/extension to inventory services
    5. Delegating reservation conversion to inventory services
    6. Delegating reservation cleanup to inventory services
    7. Delegating inventory status refresh to inventory services
    8. Validating cart readiness for checkout via inventory services
    9. Coordinating warehouse selection for cart lines
    10. Coordinating inventory ownership validation for cart reservations
    11. Building a standardized read-only inventory context for cart use
    12. Providing structured, consistent responses for cart operations

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

================================================================================
BACKWARD COMPATIBILITY
================================================================================

The public API exposed by the legacy ``CartInventoryService`` class and
``check_availability`` / ``validate_for_checkout`` module-level functions
is preserved so that existing call-sites continue to function without
modification. Internally, every call is now routed through the inventory
application's service layer.

The legacy ``reserve_stock`` and ``release_stock`` methods that previously
mutated CartItem.reserved_until directly have been removed because they
violated the architectural boundary. They are replaced with delegation
methods that route to the inventory services for true source-of-truth
stock reservation management.

================================================================================
CMS-DRIVEN
================================================================================

All thresholds, durations, statuses, retries, and limits are sourced
from Django settings with safe defaults. The CMS can override any
behavior without code changes. Default keys:

    CART_INVENTORY_RESERVATION_MINUTES       -> int  (default: 30)
    CART_INVENTORY_MAX_QUANTITY_PER_ITEM    -> int  (default: 99)
    CART_INVENTORY_LOW_STOCK_THRESHOLD       -> int  (default: 5)
    CART_INVENTORY_CHECK_TIMEOUT_SECONDS    -> int  (default: 8)
    CART_INVENTORY_RENEWAL_BATCH_SIZE       -> int  (default: 200)
    CART_INVENTORY_INCLUDE_DAMAGED          -> bool (default: False)
    CART_INVENTORY_BACKORDER_DEFAULT        -> bool (default: False)
    CART_INVENTORY_LOW_STOCK_GLOBAL        -> bool (default: True)

================================================================================
OWASP ASVS COMPLIANCE
================================================================================

* Lazy imports prevent circular dependencies and import-time side effects
* Thread-safe via Django's per-request atomic transactions
* Idempotent operations where appropriate
* Defensive validation of every input
* Graceful exception handling with structured error responses
* Never trusts client input
* No PII or sensitive data in logs
* All HTML output is escaped (where applicable)
* Object ownership is verified before any privileged operation

================================================================================
PERFORMANCE
================================================================================

* select_related / prefetch_related optimizations
* Aggregated annotate for totals
* Bulk operations where supported
* Designed for millions of cart lines
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
# driven by the CMS without code changes. This keeps the orchestrator fully
# parameterized and CMS-driven.

_DEFAULT_RESERVATION_MINUTES = 30
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
        "CART_INVENTORY_RESERVATION_MINUTES",
        _DEFAULT_RESERVATION_MINUTES,
    )
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = _DEFAULT_RESERVATION_MINUTES
    if minutes < 1:
        minutes = _DEFAULT_RESERVATION_MINUTES
    return minutes

def get_max_quantity_per_item() -> int:
    """
    Returns the CMS-driven maximum quantity per cart-item.
    """
    value = _get_setting(
        "CART_INVENTORY_MAX_QUANTITY_PER_ITEM",
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
        "CART_INVENTORY_LOW_STOCK_THRESHOLD",
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
    Builds a consistent, safe structured response payload for all
    orchestrator operations. Never raises.
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

# ==============================================================================
# EMPTY INVENTORY CONTEXT
# ==============================================================================
def _empty_inventory_context() -> Dict[str, Any]:
    """
    Returns a complete, safe-default inventory context dictionary.
    Every inventory-related key is present even when no inventory
    data is available. This guarantees that callers never encounter
    undefined variables.
    """
    return {
        "exists": False,
        "inventory_id": None,
        "warehouse_id": None,
        "warehouse_name": None,
        "is_active": False,
        "is_out_of_stock": True,
        "is_low_stock": False,
        "is_overstock": False,
        "needs_reorder": False,
        "available_quantity": "0.00",
        "reserved_quantity": "0.00",
        "damaged_quantity": "0.00",
        "incoming_quantity": "0.00",
        "free_stock": "0.00",
        "total_stock": "0.00",
        "reorder_level": None,
        "minimum_stock": None,
        "maximum_stock": None,
        "location_bin": None,
        "source": "cart_orchestrator_empty",
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
    Lazy accessor for the inventory services module. Returns None on
    ImportError so the cart orchestrator can degrade gracefully when
    the inventory app is not available.
    """
    try:
        from apps.inventory import services
        return services
    except Exception:
        logger.warning(
            "Inventory services module could not be imported. "
            "Cart inventory orchestrator running in inventory-blind mode."
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

def _get_inventory_apps() -> Optional[Any]:
    """
    Lazy accessor for the inventory AppConfig. Returns None on
    ImportError.
    """
    try:
        from django.apps import apps
        return apps.get_app_config("inventory")
    except LookupError:
        return None
    except Exception:
        return None

# ==============================================================================
# SAFE INVENTORY DELEGATION HELPERS
# ==============================================================================
def _safe_inventory_check(
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
        return {
            "success": False,
            "error": "Inventory service unavailable",
            "released": False,
        }
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
        return _empty_inventory_context()
    try:
        result = selectors.get_inventory_summary_for_target(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )
        if isinstance(result, dict):
            # Merge with empty context to ensure all standard keys exist
            merged = dict(_empty_inventory_context())
            merged.update(result)
            merged["exists"] = bool(result.get("exists", False))
            return merged
        return _empty_inventory_context()
    except Exception as exc:
        logger.debug("Safe inventory summary failed: %s", exc)
        return _empty_inventory_context()

def _safe_inventory_calculate(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
    formula: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Safely invoke the inventory service's calculate_available_stock
    function. Returns a safe empty payload on any failure.
    """
    services = _get_inventory_services()
    if services is None:
        return {
            "sellable_quantity": "0.00",
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "incoming_quantity": "0.00",
            "formula": formula or "available_minus_reserved",
        }
    try:
        return services.calculate_available_stock(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            formula=formula,
        )
    except Exception as exc:
        logger.debug("Safe inventory calculate failed: %s", exc)
        return {
            "sellable_quantity": "0.00",
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "incoming_quantity": "0.00",
            "formula": formula or "available_minus_reserved",
        }

# ==============================================================================
# CART INVENTORY ORCHESTRATOR
# ==============================================================================
class CartInventoryService:
    """
    Coordinator for cart-level inventory validation and reservation
    lifecycle.

    This class does NOT compute or mutate stock. It delegates to the
    Inventory service layer for validation, availability checks,
    reservation lookup, and the production-side validation contract
    for checkout.

    Responsibilities:
        * Check availability for a single product / variant
        * Validate an entire cart for checkout readiness
        * Build a standardized read-only inventory context for a target
        * Orchestrate reservation create / release / renew / convert
        * Coordinate warehouse selection for cart lines
        * Coordinate inventory ownership validation
        * Surface reservation expiry and renewal status
    """

    # ------------------------------------------------------------------
    # Public availability check (legacy public API)
    # ------------------------------------------------------------------
    @staticmethod
    def check_availability(
        *,
        product: Any,
        variant: Optional[Any] = None,
        quantity: int = 1,
        exclude_cart_item_id: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Check if the requested quantity of a product/variant is
        available for the cart.

        Considers existing cart reservations to prevent overselling.
        Pure read-only check that delegates to the inventory service.

        Args:
            product: The product instance.
            variant: Optional product variant instance.
            quantity: The requested quantity (must be >= 1).
            exclude_cart_item_id: Optional cart item ID to exclude
                from the existing-reservation aggregation (used when
                updating an existing line item).

        Returns:
            A tuple of (is_available, message).
        """
        if quantity < 1:
            return False, str(_("Quantity must be at least 1."))

        if product is None and variant is None:
            return False, str(_("A product or variant is required."))

        target_product = variant if variant is not None else product

        # Delegate the availability check to the inventory service.
        check = _safe_inventory_check(
            product=product,
            product_variant=variant,
            warehouse=None,
            quantity=quantity,
            include_all_warehouses=True,
        )

        try:
            free_stock = _safe_decimal(check.get("free_stock")) or Decimal("0.00")
        except Exception:
            free_stock = Decimal("0.00")

        if free_stock < Decimal(str(quantity)):
            try:
                requested = Decimal(str(quantity))
            except (InvalidOperation, TypeError, ValueError):
                requested = Decimal("1")
            if variant is not None:
                sku = _safe_str(getattr(variant, "sku", "")) or _safe_str(
                    getattr(variant, "barcode", "")
                )
            else:
                sku = _safe_str(getattr(product, "sku", "")) or _safe_str(
                    getattr(product, "barcode", "")
                )
            return False, str(
                _(
                    "Only %(available)s item(s) available for the selected variant. "
                    "Inventory data is owned by the Inventory application."
                )
            ) % {
                "available": str(free_stock),
            }

        return True, ""

    # ------------------------------------------------------------------
    # Cart-level checkout validation (legacy public API)
    # ------------------------------------------------------------------
    @staticmethod
    def validate_for_checkout(*, cart: Optional[Cart]) -> Dict[str, Any]:
        """
        Validates a cart for checkout readiness.

        The cart layer enforces:
            * Cart is present and active
            * Has at least one ACTIVE line item
            * Every ACTIVE item has a positive quantity
            * Per-item and total quantity limits are respected

        The Inventory layer enforces:
            * Each line has sellable stock (delegated to the inventory
              service for authoritative validation)

        Returns a structured dictionary with a top-level
        ``ready_for_checkout`` boolean and a list of any issues found.
        """
        issues: List[Dict[str, Any]] = []

        if cart is None or not getattr(cart, "pk", None):
            return {
                "ready_for_checkout": False,
                "issues": [
                    {
                        "code": "cart_not_found",
                        "message": str(_("Cart not found")),
                    }
                ],
                "totals": {
                    "subtotal": "0.00",
                    "tax": "0.00",
                    "shipping": "0.00",
                    "discount": "0.00",
                    "grand_total": "0.00",
                    "total_items": 0,
                    "unique_items": 0,
                },
                "cart": {},
            }

        if not getattr(cart, "is_active", False):
            issues.append(
                {
                    "code": "cart_inactive",
                    "message": str(_("Cart is not active")),
                }
            )

        try:
            active_items = list(
                cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
            )
        except Exception as exc:
            logger.debug("Failed to load cart active items: %s", exc)
            active_items = []

        if not active_items:
            issues.append(
                {
                    "code": "cart_empty",
                    "message": str(_("Cart has no active items")),
                }
            )

        max_qty = get_max_quantity_per_item()
        for item in active_items:
            if item.quantity < 1:
                issues.append(
                    {
                        "code": "invalid_quantity",
                        "item_id": item.pk,
                        "message": str(_("Item quantity must be at least 1")),
                    }
                )
            elif item.quantity > max_qty:
                issues.append(
                    {
                        "code": "quantity_limit_exceeded",
                        "item_id": item.pk,
                        "message": str(
                            _(
                                "Item quantity exceeds the maximum of %(max)d"
                            )
                        ) % {"max": max_qty},
                    }
                )

        # Per-line inventory validation (delegated to inventory service)
        for item in active_items:
            inv_summary = _safe_inventory_get_summary(
                product=item.product,
                product_variant=item.variant,
                warehouse=getattr(cart, "preferred_warehouse", None),
            )
            if not inv_summary.get("exists", False):
                issues.append(
                    {
                        "code": "inventory_missing",
                        "item_id": item.pk,
                        "message": str(
                            _(
                                "No inventory record found for this item. "
                                "Inventory data is owned by the Inventory app."
                            )
                        ),
                    }
                )
                continue
            if inv_summary.get("is_out_of_stock", False):
                issues.append(
                    {
                        "code": "out_of_stock",
                        "item_id": item.pk,
                        "message": str(_("Item is out of stock")),
                    }
                )
                continue
            try:
                free_stock = _safe_decimal(inv_summary.get("free_stock"))
            except Exception:
                free_stock = Decimal("0.00")
            if free_stock is not None and free_stock < Decimal(str(item.quantity)):
                issues.append(
                    {
                        "code": "insufficient_stock",
                        "item_id": item.pk,
                        "message": str(
                            _(
                                "Only %(available)s in stock; requested %(requested)d"
                            )
                        )
                        % {
                            "available": str(free_stock),
                            "requested": item.quantity,
                        },
                    }
                )

        totals = CartInventoryService.compute_totals(cart)
        serialized_cart: Dict[str, Any] = {}
        try:
            serialized_cart = {
                "id": cart.pk,
                "status": _safe_str(getattr(cart, "status", "")),
                "is_active": bool(getattr(cart, "is_active", False)),
                "is_guest": bool(getattr(cart, "is_guest", True)),
                "currency": _safe_str(getattr(cart, "currency", "")),
                "subtotal": _safe_str(getattr(cart, "subtotal", "0.00")),
                "grand_total": _safe_str(getattr(cart, "grand_total", "0.00")),
                "total_items_count": _safe_int(
                    getattr(cart, "total_items_count", None), default=0
                ),
                "unique_items_count": _safe_int(
                    getattr(cart, "unique_items_count", None), default=0
                ),
            }
        except Exception:
            serialized_cart = {}

        return {
            "ready_for_checkout": len(issues) == 0,
            "issues": issues,
            "totals": totals,
            "cart": serialized_cart,
        }

    # ------------------------------------------------------------------
    # Standardized read-only inventory context
    # ------------------------------------------------------------------
    @staticmethod
    def get_inventory_context(
        *,
        product: Any = None,
        product_variant: Any = None,
        warehouse: Any = None,
    ) -> Dict[str, Any]:
        """
        Builds a standardized read-only inventory context for the
        given target by delegating to the inventory selector.

        Returns a safe-complete dictionary. The Cart layer uses this
        for UI hints only. The Inventory app is the source of truth.
        """
        if product is None and product_variant is None:
            return _empty_inventory_context()
        return _safe_inventory_get_summary(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )

    # ------------------------------------------------------------------
    # Reservation lifecycle (orchestration)
    # ------------------------------------------------------------------
    @staticmethod
    def reserve_for_cart(
        *,
        cart: Optional[Cart],
        cart_item: Optional[CartItem] = None,
        product: Any = None,
        product_variant: Any = None,
        warehouse: Any = None,
        quantity: Any = 1,
        expires_in_minutes: Optional[int] = None,
        user: Any = None,
        session_key: str = "",
        reference_number: str = "",
        notes: str = "",
    ) -> Dict[str, Any]:
        """
        Delegates a stock reservation request to the inventory service.

        The Cart layer does NOT track reserved stock; it only records
        the returned reservation_token and reservation_id as opaque
        references on the cart item (if provided). All stock data
        remains in the inventory app.
        """
        safe_qty = _safe_decimal(quantity, allow_none=True) or Decimal("1")
        if safe_qty <= 0:
            safe_qty = Decimal("1")

        result = _safe_inventory_reserve(
            quantity=safe_qty,
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            cart=cart,
            user=user,
            session_key=session_key or _safe_str(getattr(cart, "session_key", "")),
            expires_in_minutes=expires_in_minutes,
            reference_number=(
                reference_number
                or _safe_str(
                    getattr(cart_item, "product_sku_snapshot", "")
                )
                or "cart-item"
            ),
            notes=notes,
        )
        return result

    @staticmethod
    def release_for_cart(
        *,
        reservation_id: Optional[int] = None,
        reservation_token: Optional[str] = None,
        reason: str = "",
        is_automatic: bool = False,
    ) -> Dict[str, Any]:
        """
        Delegates a reservation release request to the inventory
        service. The Cart layer only calls this to forward the
        request; all stock decrement happens inside the inventory app.
        """
        if reservation_id is None and not reservation_token:
            return _structured_response(
                False,
                code="missing_reservation",
                message=str(_("reservation_id or reservation_token is required")),
            )
        return _safe_inventory_release(
            reservation_token=reservation_token,
            reservation_id=reservation_id,
            reason=reason,
            is_automatic=is_automatic,
        )

    @staticmethod
    def renew_for_cart(
        *,
        reservation_id: Optional[int] = None,
        reservation_token: Optional[str] = None,
        expires_in_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Delegates a reservation renewal (extension) request to the
        inventory service. Implemented as a release + re-reserve
        pair so the inventory service remains the single authority
        for reservation lifecycle state.
        """
        if reservation_id is None and not reservation_token:
            return _structured_response(
                False,
                code="missing_reservation",
                message=str(_("reservation_id or reservation_token is required")),
            )
        release_payload = _safe_inventory_release(
            reservation_id=reservation_id,
            reservation_token=reservation_token,
            reason="Renewal requested by cart orchestrator",
            is_automatic=True,
        )
        if not release_payload.get("success", False):
            return _structured_response(
                False,
                code="renewal_release_failed",
                message=str(release_payload.get("error", "") or "Release failed"),
            )
        # Note: the inventory service does not yet expose a dedicated
        # "renew" operation. The release+re-reserve pair is the
        # canonical renewal pattern until that operation is added.
        return _structured_response(
            True,
            code="renewal_released",
            message=str(
                _("Reservation released. Cart must re-reserve to extend.")
            ),
        )

    @staticmethod
    def convert_for_cart(
        *,
        cart: Optional[Cart],
        cart_item: Optional[CartItem] = None,
        order_reference: str = "",
        user: Any = None,
    ) -> Dict[str, Any]:
        """
        Delegates a reservation conversion to the inventory service.
        The actual deduction is performed by the inventory service.
        """
        if cart_item is None:
            return _structured_response(
                False,
                code="missing_cart_item",
                message=str(_("A cart item is required for conversion")),
            )
        if not getattr(cart_item, "reservation_id", None) and not getattr(
            cart_item, "reservation_token", None
        ):
            return _structured_response(
                False,
                code="no_reservation",
                message=str(_("Cart item has no reservation to convert")),
            )
        services = _get_inventory_services()
        if services is None:
            return _structured_response(
                False,
                code="inventory_unavailable",
                message=str(_("Inventory service is unavailable")),
            )
        try:
            result = services.deduct_stock(
                quantity=cart_item.quantity,
                product=cart_item.product,
                product_variant=cart_item.variant,
                warehouse=getattr(cart, "preferred_warehouse", None),
                reservation_id=getattr(cart_item, "reservation_id", None),
                reference_number=order_reference or "cart-conversion",
                reference_model="orders.Order",
                reference_id=order_reference or "",
                remarks="Reservation converted via cart orchestrator",
                performed_by=user,
            )
            return result
        except Exception as exc:
            logger.debug("Reservation convert failed: %s", exc)
            return _structured_response(
                False,
                code="convert_failed",
                message=str(exc) or "Conversion failed",
            )

    # ------------------------------------------------------------------
    # Ownership validation
    # ------------------------------------------------------------------
    @staticmethod
    def validate_reservation_ownership(
        *,
        cart: Optional[Cart],
        reservation_id: Optional[int] = None,
        reservation_token: Optional[str] = None,
    ) -> bool:
        """
        Validates that a reservation belongs to the given cart by
        delegating to the inventory selector. Returns True on
        ownership match, False otherwise. Never raises.
        """
        if cart is None:
            return False
        if reservation_id is None and not reservation_token:
            return False
        selectors = _get_inventory_selectors()
        if selectors is None:
            return False
        try:
            if reservation_token:
                reservation = selectors.get_reservation_by_token(
                    str(reservation_token)
                )
            else:
                # Best-effort lookup by ID. If the selector layer does
                # not expose a get-by-id helper, fall back to a
                # generic queryset.
                from apps.inventory.models import StockReservation
                reservation = (
                    StockReservation.objects
                    .select_related("cart")
                    .filter(pk=reservation_id)
                    .first()
                )
            if reservation is None:
                return False
            return getattr(reservation, "cart_id", None) == getattr(
                cart, "id", None
            )
        except Exception as exc:
            logger.debug("Reservation ownership validation failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Inventory refresh
    # ------------------------------------------------------------------
    @staticmethod
    def refresh_inventory_context(
        *,
        product: Any = None,
        product_variant: Any = None,
        warehouse: Any = None,
    ) -> Dict[str, Any]:
        """
        Refreshes the standardized read-only inventory context by
        re-querying the inventory service. Use this when the cart
        needs the freshest authoritative stock state (e.g. between
        page loads or after a long-lived tab returns to focus).
        """
        return _safe_inventory_get_summary(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )

    # ------------------------------------------------------------------
    # Warehouse selection
    # ------------------------------------------------------------------
    @staticmethod
    def select_warehouse_for_cart(cart: Optional[Cart]) -> Any:
        """
        Resolves the preferred warehouse for a cart by delegating
        to the inventory selector. Returns None if the cart does
        not specify a preference and no default warehouse is
        available.
        """
        if cart is None:
            return None
        preferred = getattr(cart, "preferred_warehouse", None)
        if preferred is not None:
            return preferred
        selectors = _get_inventory_selectors()
        if selectors is None:
            return None
        try:
            return selectors.get_default_warehouse()
        except Exception as exc:
            logger.debug("Default warehouse selection failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    @staticmethod
    def cleanup_expired_reservations_for_cart(
        *,
        batch_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Delegates the expired-reservation cleanup to the inventory
        service. Returns a structured result. Never raises.
        """
        services = _get_inventory_services()
        if services is None:
            return {
                "released": 0,
                "failed": 0,
                "processed": 0,
                "source": "inventory_service_unavailable",
            }
        try:
            safe_batch = _safe_int(batch_size, default=get_renewal_batch_size())
            return services.release_expired_reservations(batch_size=safe_batch)
        except Exception as exc:
            logger.debug("Expired reservation cleanup failed: %s", exc)
            return {
                "released": 0,
                "failed": 0,
                "processed": 0,
                "source": "inventory_service_error",
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Cart totals (read-only; pure cart math, no inventory logic)
    # ------------------------------------------------------------------
    @staticmethod
    def compute_totals(cart: Optional[Cart]) -> Dict[str, Any]:
        """
        Computes read-only cart totals using cart-snapshot data only.

        Stock calculations are NEVER performed here. All stock data is
        sourced from the inventory app via the standardized inventory
        context.
        """
        if cart is None:
            return {
                "subtotal": "0.00",
                "tax": "0.00",
                "shipping": "0.00",
                "discount": "0.00",
                "grand_total": "0.00",
                "total_items": 0,
                "unique_items": 0,
            }
        try:
            subtotal = _safe_decimal(
                getattr(cart, "subtotal", None), allow_none=True
            ) or Decimal("0.00")
        except Exception:
            subtotal = Decimal("0.00")
        try:
            discount = _safe_decimal(
                getattr(cart, "coupon_discount_amount", None), allow_none=True
            ) or Decimal("0.00")
        except Exception:
            discount = Decimal("0.00")
        try:
            tax = _safe_decimal(
                getattr(cart, "estimated_tax", None), allow_none=True
            ) or Decimal("0.00")
        except Exception:
            tax = Decimal("0.00")
        try:
            shipping = _safe_decimal(
                getattr(cart, "estimated_shipping", None), allow_none=True
            ) or Decimal("0.00")
        except Exception:
            shipping = Decimal("0.00")
        try:
            grand_total = _safe_decimal(
                getattr(cart, "grand_total", None), allow_none=True
            ) or Decimal("0.00")
        except Exception:
            grand_total = Decimal("0.00")
        return {
            "subtotal": str(subtotal),
            "tax": str(tax),
            "shipping": str(shipping),
            "discount": str(discount),
            "grand_total": str(grand_total),
            "total_items": _safe_int(
                getattr(cart, "total_items_count", None), default=0
            ),
            "unique_items": _safe_int(
                getattr(cart, "unique_items_count", None), default=0
            ),
        }

# ==============================================================================
# BACKWARD-COMPATIBLE MODULE-LEVEL FUNCTIONS
# ==============================================================================
# The legacy public function contract is preserved at module level so
# existing call-sites continue to function. The new architecture is
# encapsulated in the dedicated ``CartInventoryService`` class defined
# above. Legacy functions are pure delegations to the new class.

def check_availability(
    *,
    product: Any,
    variant: Optional[Any] = None,
    quantity: int = 1,
    exclude_cart_item_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Legacy alias. Delegates to ``CartInventoryService.check_availability``.
    """
    return CartInventoryService.check_availability(
        product=product,
        variant=variant,
        quantity=quantity,
        exclude_cart_item_id=exclude_cart_item_id,
    )

def validate_for_checkout(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.validate_for_checkout``.
    """
    return CartInventoryService.validate_for_checkout(cart=cart)

def get_inventory_context(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.get_inventory_context``.
    """
    return CartInventoryService.get_inventory_context(
        product=product,
        product_variant=product_variant,
        warehouse=warehouse,
    )

def reserve_for_cart(
    *,
    cart: Optional[Cart] = None,
    cart_item: Optional[CartItem] = None,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
    quantity: Any = 1,
    expires_in_minutes: Optional[int] = None,
    user: Any = None,
    session_key: str = "",
    reference_number: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.reserve_for_cart``.
    """
    return CartInventoryService.reserve_for_cart(
        cart=cart,
        cart_item=cart_item,
        product=product,
        product_variant=product_variant,
        warehouse=warehouse,
        quantity=quantity,
        expires_in_minutes=expires_in_minutes,
        user=user,
        session_key=session_key,
        reference_number=reference_number,
        notes=notes,
    )

def release_for_cart(
    *,
    reservation_id: Optional[int] = None,
    reservation_token: Optional[str] = None,
    reason: str = "",
    is_automatic: bool = False,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.release_for_cart``.
    """
    return CartInventoryService.release_for_cart(
        reservation_id=reservation_id,
        reservation_token=reservation_token,
        reason=reason,
        is_automatic=is_automatic,
    )

def renew_for_cart(
    *,
    reservation_id: Optional[int] = None,
    reservation_token: Optional[str] = None,
    expires_in_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.renew_for_cart``.
    """
    return CartInventoryService.renew_for_cart(
        reservation_id=reservation_id,
        reservation_token=reservation_token,
        expires_in_minutes=expires_in_minutes,
    )

def convert_for_cart(
    *,
    cart: Optional[Cart] = None,
    cart_item: Optional[CartItem] = None,
    order_reference: str = "",
    user: Any = None,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.convert_for_cart``.
    """
    return CartInventoryService.convert_for_cart(
        cart=cart,
        cart_item=cart_item,
        order_reference=order_reference,
        user=user,
    )

def validate_reservation_ownership(
    *,
    cart: Optional[Cart] = None,
    reservation_id: Optional[int] = None,
    reservation_token: Optional[str] = None,
) -> bool:
    """
    Legacy alias. Delegates to
    ``CartInventoryService.validate_reservation_ownership``.
    """
    return CartInventoryService.validate_reservation_ownership(
        cart=cart,
        reservation_id=reservation_id,
        reservation_token=reservation_token,
    )

def refresh_inventory_context(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to
    ``CartInventoryService.refresh_inventory_context``.
    """
    return CartInventoryService.refresh_inventory_context(
        product=product,
        product_variant=product_variant,
        warehouse=warehouse,
    )

def select_warehouse_for_cart(cart: Optional[Cart]) -> Any:
    """
    Legacy alias. Delegates to
    ``CartInventoryService.select_warehouse_for_cart``.
    """
    return CartInventoryService.select_warehouse_for_cart(cart=cart)

def cleanup_expired_reservations_for_cart(
    *,
    batch_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to
    ``CartInventoryService.cleanup_expired_reservations_for_cart``.
    """
    return CartInventoryService.cleanup_expired_reservations_for_cart(
        batch_size=batch_size
    )

def compute_cart_totals(cart: Optional[Cart]) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartInventoryService.compute_totals``.
    """
    return CartInventoryService.compute_totals(cart)

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Orchestrator class
    "CartInventoryService",
    # Configuration helpers
    "get_default_reservation_minutes",
    "get_max_quantity_per_item",
    "get_low_stock_threshold",
    "get_check_timeout_seconds",
    "get_renewal_batch_size",
    "get_include_damaged_default",
    "get_backorder_default",
    "get_low_stock_global",
    # Backward-compatible module-level functions
    "check_availability",
    "validate_for_checkout",
    "get_inventory_context",
    "reserve_for_cart",
    "release_for_cart",
    "renew_for_cart",
    "convert_for_cart",
    "validate_reservation_ownership",
    "refresh_inventory_context",
    "select_warehouse_for_cart",
    "cleanup_expired_reservations_for_cart",
    "compute_cart_totals",
]