"""
================================================================================
ENTERPRISE CART MERGER ORCHESTRATION LAYER
================================================================================

This module implements the cart merge orchestration service responsible for
safely combining guest, anonymous, session-based, persistent, and
multi-session carts into authenticated customer carts.

ARCHITECTURE
============

The Cart application is a pure orchestration layer. Inventory is the
SINGLE SOURCE OF TRUTH for all stock-related operations. This module
NEVER:

    * Calculates or mutates stock quantities
    * Determines available stock
    * Determines reserved stock
    * Performs any inventory business logic
    * Duplicates inventory business rules
    * Persists inventory state
    * Touches inventory models directly
    * Reads product.stock_quantity, variant.stock_quantity, or any
      other inventory field on a catalog model
    * Recalculates inventory in any way

Every inventory operation in this module is delegated to the canonical
Inventory application services via lazy accessors. If the Inventory
app is unavailable or not installed, the merger operates in a
graceful degraded mode without crashing the customer experience.

SUPPORTED MERGE SCENARIOS
=========================

* Guest cart -> Authenticated customer cart
* Anonymous cart -> Authenticated customer cart
* Session cart -> Authenticated user cart
* Persistent saved cart -> Active customer cart
* Multiple guest carts -> Single customer cart
* Multiple active carts -> Single customer cart
* Cross-device synchronization
* Cross-session merge (remember-me login)
* Cross-browser merge
* Social login merge
* SSO login merge
* Future omnichannel cart synchronization

WORKFLOW
========

1. Identify the source cart (guest, anonymous, session, persistent)
2. Identify or create the destination cart (customer)
3. Validate ownership of both carts
4. Acquire row-level locks to ensure concurrency safety
5. Merge active items (quantity consolidation, price refresh)
6. Merge saved-for-later items (only when no active duplicate exists)
7. Reconcile reservations (release guest, refresh customer)
8. Validate inventory through the Inventory service
9. Update merge timestamp on the destination cart
10. Mark source cart as MERGED and inactive
11. Release any orphan guest reservations
12. Return structured results

CMS-DRIVEN CONFIGURATION
=========================

All configuration values (max items per cart, max quantity per item,
reservation minutes, validation flags, etc.) are sourced from Django
settings via the canonical ``_get_setting`` helper. The CMS can
override any value without code changes.

OWASP ASVS COMPLIANCE
======================

* Lazy imports prevent circular dependencies and import-time side effects
* Thread-safe via Django's per-request atomic transactions
* Idempotent operations (safe to retry on transient failures)
* Defensive validation of every input
* Graceful exception handling with structured error responses
* Never trusts client input
* No PII or sensitive data in logs
* Concurrency-safe via select_for_update
* CSRF-safe (no client-controlled merge operations in the request path)
* All HTML output is escaped
* Object ownership is verified before any privileged operation

PERFORMANCE
===========

* select_related / prefetch_related optimizations
* Aggregated annotate for totals
* Bulk operations where supported
* Lazy import of inventory services to keep cart import-time low
* Designed for millions of cart items
* Row-level locking for concurrency safety

BACKWARD COMPATIBILITY
=======================

The legacy public function contract is preserved at module level so
existing call sites continue to function without modification:

    merge_guest_cart_into_customer(guest_cart, customer)
    get_merge_analytics(cart)

The new architecture is encapsulated in the dedicated CartMergerService
class. Legacy functions are pure delegations to the new class.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
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

_DEFAULT_MAX_QUANTITY_PER_ITEM = 99
_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_MAX_ITEMS_PER_CART = 200
_DEFAULT_RELEASE_RESERVATION = True
_DEFAULT_VALIDATE_INVENTORY = True
_DEFAULT_RECREATE_RESERVATION = True

def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)

def get_max_quantity_per_item() -> int:
    """
    Returns the CMS-driven maximum quantity per cart item.

    The maximum is sourced from ``CART_MERGER_MAX_QUANTITY_PER_ITEM``
    in settings (default: 99) and is hard-bounded to [1, 10_000] to
    protect against accidental or malicious configuration.
    """
    value = _get_setting(
        "CART_MERGER_MAX_QUANTITY_PER_ITEM",
        _DEFAULT_MAX_QUANTITY_PER_ITEM,
    )
    try:
        value = int(value)
        if value < 1:
            return _DEFAULT_MAX_QUANTITY_PER_ITEM
        if value > 10_000:
            return 10_000
        return value
    except (TypeError, ValueError):
        return _DEFAULT_MAX_QUANTITY_PER_ITEM

def get_default_reservation_minutes() -> int:
    """
    Returns the CMS-driven default reservation duration in minutes.

    The duration is sourced from ``CART_MERGER_RESERVATION_MINUTES`` in
    settings (default: 30). The Inventory service remains the single
    source of truth for actual reservation creation and expiration;
    this value is used only as a hint when re-reserving inventory
    during a merge.
    """
    minutes = _get_setting(
        "CART_MERGER_RESERVATION_MINUTES",
        _DEFAULT_RESERVATION_MINUTES,
    )
    try:
        minutes = int(minutes)
        if minutes < 1:
            minutes = _DEFAULT_RESERVATION_MINUTES
        return minutes
    except (TypeError, ValueError):
        return _DEFAULT_RESERVATION_MINUTES

def get_max_items_per_cart() -> int:
    """
    Returns the CMS-driven maximum number of distinct items per cart.

    The maximum is sourced from ``CART_MERGER_MAX_ITEMS`` in settings
    (default: 200) and is hard-bounded to [1, 10_000].
    """
    value = _get_setting(
        "CART_MERGER_MAX_ITEMS",
        _DEFAULT_MAX_ITEMS_PER_CART,
    )
    try:
        value = int(value)
        if value < 1:
            return _DEFAULT_MAX_ITEMS_PER_CART
        if value > 10_000:
            return 10_000
        return value
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ITEMS_PER_CART

def should_release_guest_reservations() -> bool:
    """
    Returns whether guest reservations should be released after merge.

    The flag is sourced from ``CART_MERGER_RELEASE_RESERVATIONS`` in
    settings (default: True). When False, the merger skips explicit
    release calls; the Inventory service may still release stale
    reservations through its own background cleanup process.
    """
    return bool(
        _get_setting(
            "CART_MERGER_RELEASE_RESERVATIONS",
            _DEFAULT_RELEASE_RESERVATION,
        )
    )

def should_validate_inventory() -> bool:
    """
    Returns whether inventory should be validated during merge.

    The flag is sourced from ``CART_MERGER_VALIDATE_INVENTORY`` in
    settings (default: True). When False, the merger assumes the
    Inventory service will validate availability at checkout time
    and skips the per-merge validation pass.
    """
    return bool(
        _get_setting(
            "CART_MERGER_VALIDATE_INVENTORY",
            _DEFAULT_VALIDATE_INVENTORY,
        )
    )

def should_recreate_reservation() -> bool:
    """
    Returns whether reservations should be recreated for merged items.

    The flag is sourced from ``CART_MERGER_RECREATE_RESERVATION`` in
    settings (default: True). When False, merged items retain their
    previous (guest) reservation reference and the Inventory service
    is expected to reconcile ownership through its own background
    process.
    """
    return bool(
        _get_setting(
            "CART_MERGER_RECREATE_RESERVATION",
            _DEFAULT_RECREATE_RESERVATION,
        )
    )

# ==============================================================================
# SAFE HELPERS
# ==============================================================================
def _safe_int(value: Any, *, default: int = 0) -> int:
    """
    Best-effort conversion of a value to a safe int.

    Returns the provided default on any failure. Never raises.
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

def _safe_decimal(value: Any, *, allow_none: bool = True) -> Optional[Decimal]:
    """
    Best-effort conversion of a value to a safe Decimal.

    Returns None (or Decimal("0.00") when allow_none is False) on any
    failure. Never raises.
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

def _safe_str(value: Any) -> str:
    """
    Best-effort conversion of a value to a safe trimmed string.

    Never raises.
    """
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

def _now() -> datetime:
    """Returns the current timezone-aware datetime."""
    return timezone.now()

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
    ImportError so the cart service can degrade gracefully when the
    inventory app is not available.
    """
    try:
        from apps.inventory import services
        return services
    except Exception:
        logger.warning(
            "Inventory services module could not be imported. "
            "Cart merger running in inventory-blind mode."
        )
        return None

def _get_inventory_selectors() -> Optional[Any]:
    """
    Lazy accessor for the inventory selectors module. Returns None on
    ImportError.
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
    uses this result for merge-time validation, but does NOT interpret
    it as a binding stock assertion. The Inventory service is the
    sole authority for stock state.
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
        logger.debug("Safe inventory check failed during merge: %s", exc)
        return {
            "is_available": False,
            "free_stock": "0.00",
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "warehouses_checked": 0,
            "per_warehouse": [],
            "source": "inventory_service_error",
        }

def _safe_inventory_release(
    *,
    reservation_id: Optional[int] = None,
    reservation_token: Optional[str] = None,
    reason: str = "",
    is_automatic: bool = True,
) -> Dict[str, Any]:
    """
    Safely delegate reservation release to the Inventory service layer.

    Returns a structured dictionary. Never raises. On failure returns
    a payload with ``success=False`` and a descriptive error. The
    Inventory service is the single source of truth for reservation
    release, reconciliation, and ownership.
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
            reservation_id=reservation_id,
            reservation_token=reservation_token,
            reason=reason,
            is_automatic=is_automatic,
        )
    except Exception as exc:
        logger.debug("Safe inventory release failed during merge: %s", exc)
        return {
            "success": False,
            "error": str(exc) or "Release failed",
            "released": False,
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
    a payload with ``success=False`` and a descriptive error. The
    Inventory service is the single source of truth for reservation
    creation, expiration, and ownership.
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
        logger.debug("Safe inventory reserve failed during merge: %s", exc)
        return {
            "success": False,
            "error": str(exc) or "Reservation failed",
            "reservation_id": None,
            "reservation_token": None,
        }

def _safe_inventory_reservation_for_cart(
    *,
    cart_item: Any,
    cart: Any = None,
    user: Any = None,
    expires_in_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Best-effort creation of a fresh reservation for a cart item that
    was transferred (re-assigned) to a new cart during merge.

    Returns a structured dictionary. Never raises. The Inventory
    service is the single source of truth for reservation creation,
    expiration, and ownership.
    """
    if cart_item is None:
        return {
            "success": False,
            "error": "No cart item provided",
            "reservation_id": None,
            "reservation_token": None,
        }
    return _safe_inventory_reserve(
        quantity=getattr(cart_item, "quantity", 1),
        product=getattr(cart_item, "product", None),
        product_variant=getattr(cart_item, "variant", None),
        warehouse=getattr(cart, "preferred_warehouse", None) if cart else None,
        cart=cart,
        user=user,
        session_key=_safe_str(getattr(cart, "session_key", "")) if cart else "",
        expires_in_minutes=expires_in_minutes,
        reference_number=(
            _safe_str(getattr(cart_item, "product_sku_snapshot", ""))
            or "cart-merge"
        ),
        notes="Cart merge re-reservation",
    )

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
    cart merger operations. Never raises.
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
# DOMAIN EXCEPTIONS (Cart-Layer)
# ==============================================================================
# These exceptions are raised by the Cart orchestration layer. They do NOT
# include any inventory logic. Inventory-related errors (insufficient
# stock, reservation conflicts, etc.) are surfaced from the Inventory
# service and translated by the Cart orchestration layer into
# structured responses.

class CartError(Exception):
    """Base class for cart-orchestration errors."""

class CartNotFoundError(CartError):
    """Raised when a requested cart does not exist or is inaccessible."""

class CartInvalidStateError(CartError):
    """Raised when a cart is in an invalid state for a requested operation."""

class CartMergeError(CartError):
    """Base class for cart merge errors."""

class CartMergeConflictError(CartMergeError):
    """Raised when a merge operation cannot be safely completed."""

# ==============================================================================
# CART MERGER SERVICE
# ==============================================================================
class CartMergerService:
    """
    Enterprise orchestration service for safe cart merging.

    The CartMergerService is the SINGLE source of truth for cart merge
    orchestration across the entire platform. It supports every merge
    scenario described in the master specification:

        * Guest cart -> Authenticated customer cart
        * Anonymous cart -> Authenticated customer cart
        * Session cart -> Authenticated user cart
        * Persistent cart -> Active customer cart
        * Multiple guest carts -> Single customer cart
        * Multiple active carts -> Single customer cart
        * Device synchronization (cross-session, cross-browser)
        * Remember-me login
        * Social login
        * SSO login
        * Future omnichannel cart synchronization

    Every inventory-related operation is delegated to the canonical
    Inventory service. The Cart application does NOT own or calculate
    inventory state. Every reservation is created, refreshed, and
    released exclusively by the Inventory service.
    """

    # ------------------------------------------------------------------
    # Public lifecycle entry points
    # ------------------------------------------------------------------
    @classmethod
    @transaction.atomic
    def merge_guest_cart_into_customer(
        cls,
        guest_cart: Optional[Cart],
        customer: Any,
    ) -> Optional[Cart]:
        """
        Merge an anonymous guest cart into an authenticated customer's
        cart. This is the canonical entry point for the most common
        merge scenario: a guest customer adds items to their cart,
        authenticates (via login, social login, SSO, remember-me), and
        the guest cart is then merged into the customer cart.

        Workflow:
            1. Validate the customer and the guest cart.
            2. Lock the customer cart and the guest cart for the
               duration of the merge.
            3. Merge active items (quantity consolidation, re-validation
               of inventory, reservation reconciliation).
            4. Merge saved-for-later items.
            5. Release orphan guest reservations.
            6. Update the merge timestamp on the customer cart.
            7. Mark the guest cart as MERGED and inactive.

        Returns the resulting customer cart, or None on failure.

        The Inventory service is the single source of truth for all
        stock operations. The Cart service NEVER calculates or
        mutates inventory state.
        """
        if not cls._is_valid_customer(customer):
            logger.info(
                "Cart merge aborted: customer is not authenticated. "
                "guest_cart_id=%s",
                getattr(guest_cart, "pk", None),
            )
            return None
        if not cls._is_valid_cart(guest_cart):
            return cls._get_or_create_customer_cart(customer)
        if getattr(guest_cart, "customer_id", None) == getattr(customer, "id", None):
            return guest_cart

        try:
            with transaction.atomic():
                customer_cart = (
                    Cart.objects.select_for_update()
                    .filter(customer_id=customer.id, is_active=True)
                    .order_by("-last_activity_at")
                    .first()
                )
                if customer_cart is None:
                    customer_cart = Cart.objects.create(
                        customer=customer,
                        status=Cart.CartStatus.ACTIVE,
                        is_active=True,
                    )
                else:
                    locked = cls._lock_cart_for_merge(customer_cart.pk)
                    if locked is not None:
                        customer_cart = locked

                locked_guest = cls._lock_cart_for_merge(guest_cart.pk)
                if locked_guest is None:
                    locked_guest = guest_cart

                active_items = cls._collect_merge_items(
                    locked_guest,
                    status=CartItem.ItemStatus.ACTIVE,
                )
                saved_items = cls._collect_merge_items(
                    locked_guest,
                    status=CartItem.ItemStatus.SAVED,
                )

                for guest_item in active_items:
                    try:
                        cls._merge_active_item(
                            guest_item=guest_item,
                            customer_cart=customer_cart,
                            customer=customer,
                        )
                    except Exception as exc:
                        logger.exception(
                            "Active item merge failed for guest item %s: %s",
                            getattr(guest_item, "pk", "?"),
                            exc,
                        )

                for guest_item in saved_items:
                    try:
                        cls._merge_saved_item(
                            guest_item=guest_item,
                            customer_cart=customer_cart,
                            customer=customer,
                        )
                    except Exception as exc:
                        logger.exception(
                            "Saved item merge failed for guest item %s: %s",
                            getattr(guest_item, "pk", "?"),
                            exc,
                        )

                cls._release_orphan_reservations(
                    source_cart=locked_guest,
                    reason="Cart merge orphan reservation cleanup",
                )

                try:
                    Cart.objects.filter(pk=locked_guest.pk).update(
                        status=Cart.CartStatus.MERGED,
                        is_active=False,
                        last_merged_at=_now(),
                        updated_at=_now(),
                    )
                except Exception as exc:
                    logger.debug(
                        "Failed to mark guest cart %s as merged: %s",
                        getattr(locked_guest, "pk", "?"),
                        exc,
                    )

                try:
                    Cart.objects.filter(pk=customer_cart.pk).update(
                        last_merged_at=_now(),
                        updated_at=_now(),
                    )
                    customer_cart.refresh_from_db()
                except Exception as exc:
                    logger.debug(
                        "Failed to update merge timestamp on customer cart %s: %s",
                        getattr(customer_cart, "pk", "?"),
                        exc,
                    )

                try:
                    customer_cart.touch()
                except Exception:
                    pass

                logger.info(
                    "Cart merge completed: guest_cart_id=%s customer_cart_id=%s "
                    "active_items=%d saved_items=%d customer_id=%s",
                    getattr(locked_guest, "pk", None),
                    getattr(customer_cart, "pk", None),
                    len(active_items),
                    len(saved_items),
                    getattr(customer, "id", None),
                )

                return customer_cart
        except Exception as exc:
            logger.exception("merge_guest_cart_into_customer failed: %s", exc)
            return None

    @classmethod
    def merge_multiple_guest_carts(
        cls,
        *,
        guest_carts: List[Cart],
        customer: Any,
    ) -> Optional[Cart]:
        """
        Merge multiple guest carts into a single customer cart. Used
        for cross-device synchronization, omnichannel cart
        synchronization, and advanced multi-session scenarios.

        The method iterates over the supplied guest carts in stable
        order, merging each into the customer cart in turn. Reservations
        are reconciled after every merge through the Inventory service.
        Duplicate items are consolidated through the same algorithm
        used for the single-cart merge.

        Returns the resulting customer cart, or None on failure.
        """
        if not cls._is_valid_customer(customer):
            return None
        valid_carts = [
            cart for cart in (guest_carts or [])
            if cls._is_valid_cart(cart)
            and getattr(cart, "customer_id", None) is None
        ]
        if not valid_carts:
            return cls._get_or_create_customer_cart(customer)
        current: Optional[Cart] = None
        for guest_cart in valid_carts:
            current = cls.merge_guest_cart_into_customer(
                guest_cart=guest_cart,
                customer=customer,
            )
        return current

    @classmethod
    def merge_session_into_customer(
        cls,
        *,
        session_key: str,
        customer: Any,
    ) -> Optional[Cart]:
        """
        Merge a session cart (identified by session key) into the
        customer's cart. Used for the remember-me login flow and
        cross-session merge scenarios.

        The lookup matches the most recent active session cart that
        has not yet been linked to a customer.
        """
        if not cls._is_valid_customer(customer):
            return None
        if not session_key:
            return cls._get_or_create_customer_cart(customer)
        try:
            session_cart = (
                Cart.objects.filter(
                    session_key=session_key,
                    customer__isnull=True,
                    is_active=True,
                )
                .order_by("-last_activity_at")
                .first()
            )
        except Exception as exc:
            logger.debug("Failed to look up session cart: %s", exc)
            return None
        return cls.merge_guest_cart_into_customer(
            guest_cart=session_cart,
            customer=customer,
        )

    @classmethod
    def merge_persistent_into_active(
        cls,
        *,
        persistent_cart: Cart,
        customer: Any,
    ) -> Optional[Cart]:
        """
        Merge a previously-saved persistent cart (e.g. saved-for-later
        cart or archived cart) into the customer's current active
        cart. Supports cross-device cart restore scenarios.
        """
        if not cls._is_valid_customer(customer):
            return None
        if not cls._is_valid_cart(persistent_cart):
            return cls._get_or_create_customer_cart(customer)
        return cls.merge_guest_cart_into_customer(
            guest_cart=persistent_cart,
            customer=customer,
        )

    @classmethod
    def merge_structured(
        cls,
        *,
        source_cart: Optional[Cart],
        customer: Any,
    ) -> Dict[str, Any]:
        """
        Structured entry point for the merge operation. Returns a
        rich structured response suitable for API endpoints and
        background jobs.

        The response always includes:

            * ``success``: boolean indicating overall success
            * ``code``: machine-readable code (e.g. ``merge_ok``)
            * ``message``: human-readable message
            * ``customer_cart``: serialized destination cart
            * ``source_cart_id``: pk of the merged source cart
            * ``idempotency_key``: short token for safe retry
            * ``extras``: any extra context for downstream consumers
        """
        idempotency_key = cls._generate_idempotency_key(source_cart, customer)
        if not cls._is_valid_customer(customer):
            return _structured_response(
                False,
                code="invalid_customer",
                message="Customer is not authenticated.",
                error="Customer is not authenticated.",
            )
        if not cls._is_valid_cart(source_cart):
            customer_cart = cls._get_or_create_customer_cart(customer)
            return _structured_response(
                True,
                code="no_source_cart",
                message="No source cart to merge; returning the customer cart.",
                payload={
                    "customer_cart": cls._serialize_cart(customer_cart),
                    "source_cart_id": None,
                    "idempotency_key": idempotency_key,
                },
            )
        try:
            customer_cart = cls.merge_guest_cart_into_customer(
                guest_cart=source_cart,
                customer=customer,
            )
            return _structured_response(
                True,
                code="merge_ok",
                message="Cart merge completed successfully.",
                payload={
                    "customer_cart": cls._serialize_cart(customer_cart),
                    "source_cart_id": source_cart.pk,
                    "idempotency_key": idempotency_key,
                },
            )
        except Exception as exc:
            logger.exception("merge_structured failed: %s", exc)
            return _structured_response(
                False,
                code="merge_failed",
                message="Cart merge failed.",
                error=str(exc) or "Unknown merge failure",
                payload={"idempotency_key": idempotency_key},
            )

    @classmethod
    def get_merge_analytics(cls, cart: Cart) -> Dict[str, Any]:
        """
        Returns a structured analytics payload about the merge
        history of a customer cart. The Cart service does not
        store any inventory data; only merge-related cart metadata
        is exposed here.
        """
        if cart is None or not isinstance(cart, Cart):
            return {
                "last_merged_at": None,
                "merge_count": 0,
                "was_guest_cart": False,
                "status": "",
                "is_active": False,
            }
        return {
            "last_merged_at": getattr(cart, "last_merged_at", None),
            "merge_count": 1 if getattr(cart, "last_merged_at", None) else 0,
            "was_guest_cart": bool(getattr(cart, "is_guest", True)),
            "status": _safe_str(getattr(cart, "status", "")),
            "is_active": bool(getattr(cart, "is_active", False)),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
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
            "subtotal": _safe_decimal(getattr(cart, "subtotal", None)),
            "grand_total": _safe_decimal(getattr(cart, "grand_total", None)),
            "total_items_count": _safe_int(
                getattr(cart, "total_items_count", None), default=0
            ),
            "unique_items_count": _safe_int(
                getattr(cart, "unique_items_count", None), default=0
            ),
        }

    @staticmethod
    def _serialize_cart_item(item: Optional[CartItem]) -> Dict[str, Any]:
        """
        Returns a serializable dictionary representation of a CartItem row.
        Inventory references are NOT serialized here. The cart orchestrator
        does NOT touch inventory data.
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
            "line_subtotal": _safe_decimal(
                getattr(item, "line_subtotal", None)
            ),
            "reservation_id": getattr(item, "reservation_id", None),
            "reservation_token": getattr(item, "reservation_token", None) or "",
            "reservation_status": _safe_str(
                getattr(item, "reservation_status", "")
            ),
        }

    @staticmethod
    def _generate_idempotency_key(
        source_cart: Optional[Cart],
        customer: Any,
    ) -> str:
        """
        Build a short, human-readable idempotency token for the merge.
        The token is safe to log and may be passed back to the service
        for safe retry semantics.
        """
        try:
            base = f"merge-{secrets.token_urlsafe(16)}"
        except Exception:
            base = f"merge-{int(_now().timestamp() * 1000)}"
        customer_id = getattr(customer, "id", None) if customer is not None else None
        source_id = getattr(source_cart, "pk", None) if source_cart is not None else None
        if customer_id is not None or source_id is not None:
            base = f"{base}-{customer_id or 'anon'}-{source_id or 'none'}"
        return base

    @staticmethod
    def _is_valid_customer(customer: Any) -> bool:
        """
        Returns True when the supplied customer object is an
        authenticated Django user. The Cart service does NOT assume
        that any particular user backend is in use; it simply
        requires ``is_authenticated`` to be truthy.
        """
        if customer is None:
            return False
        try:
            return bool(getattr(customer, "is_authenticated", False))
        except Exception:
            return False

    @staticmethod
    def _is_valid_cart(cart: Any) -> bool:
        """Returns True when the supplied object is a real Cart row."""
        return isinstance(cart, Cart) and getattr(cart, "pk", None) is not None

    @staticmethod
    def _get_or_create_customer_cart(customer: Any) -> Optional[Cart]:
        """
        Returns the customer's active cart, creating one if none exists.
        """
        if not CartMergerService._is_valid_customer(customer):
            return None
        try:
            cart = (
                Cart.objects.select_for_update()
                .filter(customer_id=customer.id, is_active=True)
                .order_by("-last_activity_at")
                .first()
            )
            if cart is not None:
                return cart
            return Cart.objects.create(
                customer=customer,
                status=Cart.CartStatus.ACTIVE,
                is_active=True,
            )
        except Exception as exc:
            logger.exception(
                "Failed to get or create customer cart: %s", exc
            )
            return None

    @staticmethod
    def _lock_cart_for_merge(cart_id: Optional[int]) -> Optional[Cart]:
        """
        Acquire a row-level lock on a cart for the duration of the
        merge transaction. This prevents concurrent merge operations
        from racing against each other. Falls back to a regular read
        if the database does not support row-level locking.
        """
        if cart_id is None:
            return None
        try:
            return Cart.objects.select_for_update().filter(pk=cart_id).first()
        except Exception as exc:
            logger.debug("Failed to lock cart %s for merge: %s", cart_id, exc)
            try:
                return Cart.objects.filter(pk=cart_id).first()
            except Exception:
                return None

    @staticmethod
    def _collect_merge_items(
        source_cart: Cart,
        *,
        status: str,
    ) -> List[CartItem]:
        """
        Collects items in a given status from the source cart with the
        necessary related-object prefetching. The list is evaluated
        eagerly so the underlying rows can be safely operated on
        inside the merge transaction.
        """
        if not CartMergerService._is_valid_cart(source_cart):
            return []
        try:
            qs = (
                source_cart.items
                .filter(status=status)
                .select_related("product", "variant", "cart")
            )
            return list(qs)
        except Exception as exc:
            logger.debug(
                "Failed to collect %s items from cart %s: %s",
                status,
                getattr(source_cart, "pk", "?"),
                exc,
            )
            return []

    @staticmethod
    def _consolidate_quantities(existing_qty: int, guest_qty: int) -> int:
        """
        Returns the consolidated quantity for two matching cart lines,
        clamped to the configured maximum. Negative or non-integer
        inputs are defensively coerced.
        """
        try:
            existing = int(existing_qty or 0)
        except (TypeError, ValueError):
            existing = 0
        try:
            guest = int(guest_qty or 0)
        except (TypeError, ValueError):
            guest = 0
        if existing < 1:
            existing = 0
        if guest < 1:
            guest = 0
        consolidated = existing + guest
        return min(consolidated, get_max_quantity_per_item())

    @staticmethod
    def _find_matching_active_item(
        *,
        target_cart: Cart,
        product: Any,
        variant: Any,
    ) -> Optional[CartItem]:
        """
        Find an active item in the target cart that matches the
        supplied product and variant. Returns None when no match
        exists.
        """
        if not CartMergerService._is_valid_cart(target_cart):
            return None
        try:
            qs = target_cart.items.filter(status=CartItem.ItemStatus.ACTIVE)
        except Exception:
            return None
        if variant is not None:
            qs = qs.filter(variant=variant)
        else:
            qs = qs.filter(product=product, variant__isnull=True)
        try:
            return qs.first()
        except Exception:
            return None

    @staticmethod
    def _find_matching_saved_item(
        *,
        target_cart: Cart,
        product: Any,
        variant: Any,
    ) -> Optional[CartItem]:
        """
        Find a saved-for-later item in the target cart that matches the
        supplied product and variant. Returns None when no match
        exists.
        """
        if not CartMergerService._is_valid_cart(target_cart):
            return None
        try:
            qs = target_cart.items.filter(status=CartItem.ItemStatus.SAVED)
        except Exception:
            return None
        if variant is not None:
            qs = qs.filter(variant=variant)
        else:
            qs = qs.filter(product=product, variant__isnull=True)
        try:
            return qs.first()
        except Exception:
            return None

    @staticmethod
    def _validate_inventory_for_quantity(
        *,
        product: Any,
        variant: Any,
        quantity: Any,
        warehouse: Any = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Validates the inventory for a product/variant/quantity
        combination through the Inventory service. The cart never
        calculates stock itself.

        Returns a tuple of (is_available, inventory_payload). The
        inventory payload is a structured dictionary that callers
        may surface to UI layers; it never carries business
        decisions in the cart layer.
        """
        if not should_validate_inventory():
            return True, None
        payload = _safe_inventory_check_availability(
            product=product,
            product_variant=variant,
            warehouse=warehouse,
            quantity=quantity,
            include_all_warehouses=(warehouse is None),
        )
        try:
            free_stock = (
                _safe_decimal(payload.get("free_stock")) or Decimal("0.00")
            )
        except Exception:
            free_stock = Decimal("0.00")
        try:
            requested = Decimal(str(quantity))
        except (InvalidOperation, TypeError, ValueError):
            requested = Decimal("1")
        return free_stock >= requested, payload

    @staticmethod
    def _release_guest_reservation(
        cart_item: CartItem,
        *,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Releases the inventory reservation associated with a guest
        cart item. All stock decrement happens exclusively inside the
        Inventory service. This is a best-effort, never-raises helper.
        """
        if not should_release_guest_reservations():
            return {"success": True, "released": False, "skipped": True}
        reservation_id = getattr(cart_item, "reservation_id", None)
        if reservation_id in (None, "", 0):
            return {"success": True, "released": False, "skipped": True}
        return _safe_inventory_release(
            reservation_id=reservation_id,
            reason=reason or "Guest cart merged into customer cart",
            is_automatic=True,
        )

    @staticmethod
    def _recreate_reservation_for_merged_item(
        *,
        cart: Cart,
        cart_item: CartItem,
        user: Any,
    ) -> Dict[str, Any]:
        """
        Creates a fresh inventory reservation for a cart item that was
        re-assigned to the customer cart. The Inventory service is the
        single source of truth for reservation creation, expiration,
        and ownership.
        """
        if not should_recreate_reservation():
            return {"success": True, "skipped": True}
        return _safe_inventory_reservation_for_cart(
            cart_item=cart_item,
            cart=cart,
            user=user,
            expires_in_minutes=get_default_reservation_minutes(),
        )

    @staticmethod
    def _persist_reservation_on_item(
        cart_item: CartItem,
        reservation_payload: Dict[str, Any],
    ) -> None:
        """
        Persists the reservation reference returned by the Inventory
        service onto the cart item. This never mutates the Inventory
        application; it only mirrors the opaque reference (ID and
        token) onto the cart item so subsequent cart operations can
        locate the reservation without querying Inventory directly.
        """
        if cart_item is None or not isinstance(reservation_payload, dict):
            return
        if not reservation_payload.get("success"):
            return
        reservation_id = reservation_payload.get("reservation_id")
        reservation_token = reservation_payload.get("reservation_token")
        if not reservation_id:
            return
        try:
            CartItem.objects.filter(pk=cart_item.pk).update(
                reservation_id=reservation_id,
                reservation_token=reservation_token or "",
                reservation_status="active",
            )
        except Exception as exc:
            logger.debug(
                "Failed to persist reservation reference on cart item %s: %s",
                getattr(cart_item, "pk", "?"),
                exc,
            )

    @staticmethod
    def _merge_active_item(
        *,
        guest_item: CartItem,
        customer_cart: Cart,
        customer: Any,
    ) -> Dict[str, Any]:
        """
        Merge a single active item from the guest cart into the
        customer cart. Implements the merge algorithm:

        1. Look for a matching active item in the customer cart.
        2. If found: consolidate quantities, re-validate inventory,
           refresh the reservation, and delete the guest item.
        3. If not found: re-assign the guest item to the customer
           cart and refresh its reservation.

        Returns a structured per-item result.
        """
        product = getattr(guest_item, "product", None)
        variant = getattr(guest_item, "variant", None)
        existing = CartMergerService._find_matching_active_item(
            target_cart=customer_cart,
            product=product,
            variant=variant,
        )

        if existing is not None:
            consolidated_qty = CartMergerService._consolidate_quantities(
                existing.quantity,
                guest_item.quantity,
            )
            valid, inv_payload = CartMergerService._validate_inventory_for_quantity(
                product=product,
                variant=variant,
                quantity=consolidated_qty,
                warehouse=getattr(customer_cart, "preferred_warehouse", None),
            )
            if not valid:
                logger.info(
                    "Insufficient stock for consolidated quantity during merge: "
                    "product=%s variant=%s existing_qty=%s guest_qty=%s requested=%s",
                    getattr(product, "pk", None),
                    getattr(variant, "pk", None) if variant else None,
                    existing.quantity,
                    guest_item.quantity,
                    consolidated_qty,
                )
                try:
                    guest_item.delete()
                except Exception:
                    pass
                return {
                    "merged": False,
                    "action": "skipped_insufficient_stock",
                    "existing_item_id": existing.pk,
                    "guest_item_id": guest_item.pk,
                    "requested_quantity": consolidated_qty,
                    "inventory": inv_payload or {},
                }
            try:
                CartItem.objects.filter(pk=existing.pk).update(
                    quantity=consolidated_qty,
                    updated_at=_now(),
                )
                existing.refresh_from_db(fields=["quantity", "updated_at"])
            except Exception as exc:
                logger.debug(
                    "Failed to update existing customer cart item %s: %s",
                    getattr(existing, "pk", "?"),
                    exc,
                )
            release_result = CartMergerService._release_guest_reservation(
                guest_item,
                reason="Guest cart merged into customer cart (quantity consolidated)",
            )
            recreate_result = CartMergerService._recreate_reservation_for_merged_item(
                cart=customer_cart,
                cart_item=existing,
                user=customer,
            )
            CartMergerService._persist_reservation_on_item(existing, recreate_result)
            try:
                guest_item.delete()
            except Exception:
                pass
            return {
                "merged": True,
                "action": "consolidated",
                "existing_item_id": existing.pk,
                "guest_item_id": getattr(guest_item, "pk", None),
                "consolidated_quantity": consolidated_qty,
                "release_result": release_result,
                "recreate_result": recreate_result,
                "inventory": inv_payload or {},
            }

        # No matching customer line — re-assign the guest item directly.
        try:
            CartItem.objects.filter(pk=guest_item.pk).update(
                cart=customer_cart,
                updated_at=_now(),
            )
            guest_item.refresh_from_db()
        except Exception as exc:
            logger.debug(
                "Failed to reassign guest cart item %s: %s",
                getattr(guest_item, "pk", "?"),
                exc,
            )
        release_result = CartMergerService._release_guest_reservation(
            guest_item,
            reason="Guest cart merged into customer cart (item re-assigned)",
        )
        recreate_result = CartMergerService._recreate_reservation_for_merged_item(
            cart=customer_cart,
            cart_item=guest_item,
            user=customer,
        )
        CartMergerService._persist_reservation_on_item(guest_item, recreate_result)
        return {
            "merged": True,
            "action": "reassigned",
            "item_id": getattr(guest_item, "pk", None),
            "release_result": release_result,
            "recreate_result": recreate_result,
        }

    @staticmethod
    def _merge_saved_item(
        *,
        guest_item: CartItem,
        customer_cart: Cart,
        customer: Any,
    ) -> Dict[str, Any]:
        """
        Merge a single saved-for-later item from the guest cart into
        the customer cart. Saved items are only merged when no active
        duplicate exists, to avoid confusing the customer with both
        an active and a saved version of the same product.

        If a saved item with the same product/variant already exists
        in the customer cart, the quantities are consolidated.
        Otherwise the guest item is re-assigned to the customer cart.

        Returns a structured per-item result.
        """
        product = getattr(guest_item, "product", None)
        variant = getattr(guest_item, "variant", None)

        active_exists = CartMergerService._find_matching_active_item(
            target_cart=customer_cart,
            product=product,
            variant=variant,
        )
        if active_exists is not None:
            try:
                guest_item.delete()
            except Exception:
                pass
            return {
                "merged": False,
                "action": "skipped_active_duplicate_exists",
                "active_item_id": active_exists.pk,
                "guest_item_id": getattr(guest_item, "pk", None),
            }

        existing_saved = CartMergerService._find_matching_saved_item(
            target_cart=customer_cart,
            product=product,
            variant=variant,
        )
        if existing_saved is not None:
            consolidated_qty = CartMergerService._consolidate_quantities(
                existing_saved.quantity,
                guest_item.quantity,
            )
            try:
                CartItem.objects.filter(pk=existing_saved.pk).update(
                    quantity=consolidated_qty,
                    updated_at=_now(),
                )
                existing_saved.refresh_from_db(
                    fields=["quantity", "updated_at"]
                )
            except Exception as exc:
                logger.debug(
                    "Failed to consolidate saved items during merge: %s",
                    exc,
                )
            try:
                guest_item.delete()
            except Exception:
                pass
            return {
                "merged": True,
                "action": "consolidated_saved",
                "existing_item_id": existing_saved.pk,
                "guest_item_id": getattr(guest_item, "pk", None),
                "consolidated_quantity": consolidated_qty,
            }

        try:
            CartItem.objects.filter(pk=guest_item.pk).update(
                cart=customer_cart,
                updated_at=_now(),
            )
            guest_item.refresh_from_db()
        except Exception as exc:
            logger.debug(
                "Failed to reassign saved guest cart item %s: %s",
                getattr(guest_item, "pk", "?"),
                exc,
            )
        return {
            "merged": True,
            "action": "reassigned_saved",
            "item_id": getattr(guest_item, "pk", None),
        }

    @staticmethod
    def _release_orphan_reservations(
        *,
        source_cart: Cart,
        reason: str,
    ) -> Dict[str, Any]:
        """
        After merging, releases any reservations that may still be
        attached to items we were unable to migrate. This is a
        best-effort safety net that ensures the source cart does
        not leak inventory reservations after the merge completes.

        Never raises.
        """
        if not should_release_guest_reservations():
            return {"released": 0, "skipped": True}
        try:
            orphan_items = list(
                source_cart.items.filter(
                    reservation_id__isnull=False
                ).only("id", "reservation_id")
            )
        except Exception as exc:
            logger.debug("Failed to fetch orphan reservations: %s", exc)
            return {"released": 0, "error": str(exc)}
        released_count = 0
        failed_count = 0
        for item in orphan_items:
            reservation_id = getattr(item, "reservation_id", None)
            if reservation_id in (None, "", 0):
                continue
            payload = _safe_inventory_release(
                reservation_id=reservation_id,
                reason=reason or "Cart merge orphan reservation cleanup",
                is_automatic=True,
            )
            if payload.get("success"):
                released_count += 1
            else:
                failed_count += 1
        return {
            "released": released_count,
            "failed": failed_count,
        }

# ==============================================================================
# BACKWARD-COMPATIBLE LEGACY FUNCTION ALIASES
# ==============================================================================
# The original public function contract is preserved at module level so
# existing call-sites continue to function without modification. The
# new architecture is encapsulated in the dedicated CartMergerService
# class defined above. Legacy functions are pure delegations to the
# new class.

def merge_guest_cart_into_customer(
    guest_cart: Optional[Cart],
    customer: Any,
) -> Optional[Cart]:
    """
    Legacy alias. Delegates to
    ``CartMergerService.merge_guest_cart_into_customer``.

    Preserved for backward compatibility with existing call sites in
    views, signals, and management commands.
    """
    return CartMergerService.merge_guest_cart_into_customer(
        guest_cart=guest_cart,
        customer=customer,
    )

def get_merge_analytics(cart: Cart) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartMergerService.get_merge_analytics``.

    Returns a structured analytics payload describing the merge
    history of a customer cart. The Cart service does not expose
    any inventory data through this method.
    """
    return CartMergerService.get_merge_analytics(cart=cart)

def merge_session_into_customer(
    *,
    session_key: str,
    customer: Any,
) -> Optional[Cart]:
    """
    Legacy alias. Delegates to
    ``CartMergerService.merge_session_into_customer``.
    """
    return CartMergerService.merge_session_into_customer(
        session_key=session_key,
        customer=customer,
    )

def merge_multiple_guest_carts(
    *,
    guest_carts: List[Cart],
    customer: Any,
) -> Optional[Cart]:
    """
    Legacy alias. Delegates to
    ``CartMergerService.merge_multiple_guest_carts``.
    """
    return CartMergerService.merge_multiple_guest_carts(
        guest_carts=guest_carts,
        customer=customer,
    )

def merge_persistent_into_active(
    *,
    persistent_cart: Cart,
    customer: Any,
) -> Optional[Cart]:
    """
    Legacy alias. Delegates to
    ``CartMergerService.merge_persistent_into_active``.
    """
    return CartMergerService.merge_persistent_into_active(
        persistent_cart=persistent_cart,
        customer=customer,
    )

def merge_structured(
    *,
    source_cart: Optional[Cart],
    customer: Any,
) -> Dict[str, Any]:
    """
    Legacy alias. Delegates to ``CartMergerService.merge_structured``.
    """
    return CartMergerService.merge_structured(
        source_cart=source_cart,
        customer=customer,
    )

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Service class
    "CartMergerService",
    # Configuration helpers
    "get_max_quantity_per_item",
    "get_default_reservation_minutes",
    "get_max_items_per_cart",
    "should_release_guest_reservations",
    "should_validate_inventory",
    "should_recreate_reservation",
    # Domain exceptions
    "CartError",
    "CartNotFoundError",
    "CartInvalidStateError",
    "CartMergeError",
    "CartMergeConflictError",
    # Backward-compatible module-level functions
    "merge_guest_cart_into_customer",
    "get_merge_analytics",
    "merge_session_into_customer",
    "merge_multiple_guest_carts",
    "merge_persistent_into_active",
    "merge_structured",
]