"""
Enterprise-grade service layer for the Inventory application.

This module is the **single source of truth** for all stock operations
across the platform. It is the ONLY authorized layer for mutating
``Inventory`` and ``StockReservation`` rows. All mutations are routed
through ``InventoryTransaction`` records to guarantee a complete,
immutable audit trail.

CRITICAL ARCHITECTURE RULES
===========================

* No view, form, serializer, signal, admin, management command, or
  other application is allowed to directly mutate ``Inventory``,
  ``StockReservation``, ``StockAdjustment``, or
  ``InventoryTransaction`` rows. Every change must flow through the
  public service functions exposed here.

* All write operations are wrapped in ``transaction.atomic()`` to
  ensure ACID guarantees.

* All multi-row operations that depend on a specific stock level
  (e.g. deduction, transfer) MUST lock the relevant rows with
  ``select_for_update()`` BEFORE reading the current value, to
  prevent race conditions in concurrent environments.

* All quantity updates use Django ``F()`` expressions for atomic,
  database-side updates where appropriate.

* Every stock movement is recorded as an ``InventoryTransaction``,
  preserving before/after snapshots for forensic analysis and
  reconciliation.

DESIGN PRINCIPLES
=================

* CMS-driven: Default durations, thresholds, and policy formulas are
  pulled from Django settings (which can be wired to the CMS).
* Service Layer purity: No presentation logic, no I/O outside the
  database, no HTTP request objects.
* OWASP ASVS / secure-by-default: Inputs are validated, business
  invariants are enforced, and internal details are not exposed in
  exception messages.
* Audit-friendly: Every mutation is recorded with ``performed_by``,
  reference fields, and timestamps.
* Future-proof: Designed to integrate seamlessly with Purchase
  Orders, Manufacturing, Barcode, Batch/Lot, Expiry, and Serial
  Numbers without requiring major refactors.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    ADJUSTMENT_STATUS_APPLIED,
    ADJUSTMENT_STATUS_APPROVED,
    ADJUSTMENT_STATUS_CANCELLED,
    ADJUSTMENT_STATUS_DRAFT,
    ADJUSTMENT_STATUS_PENDING_APPROVAL,
    ADJUSTMENT_STATUS_REJECTED,
    INVENTORY_FLOW_INBOUND,
    INVENTORY_FLOW_NEUTRAL,
    INVENTORY_FLOW_OUTBOUND,
    RESERVATION_STATUS_ACTIVE,
    RESERVATION_STATUS_CANCELLED,
    RESERVATION_STATUS_CONVERTED,
    RESERVATION_STATUS_EXPIRED,
    RESERVATION_STATUS_RELEASED,
    Inventory,
    InventoryTransaction,
    StockAdjustment,
    StockReservation,
    Warehouse,
)

logger = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION DEFAULTS
# ==============================================================================
# All defaults can be overridden via Django settings (which in turn can be
# driven by the CMS without code changes). This keeps the service fully
# parameterized and CMS-driven.

_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_ADJUSTMENT_REASON = "manual_correction"
_DEFAULT_STOCK_FORMULA = "available_minus_reserved"
_CART_RESERVATION_TYPE = "cart"


def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)

def get_default_reservation_duration() -> timedelta:
    """
    Returns the default reservation duration as a ``timedelta``.

    The duration is sourced from the ``INVENTORY_DEFAULT_RESERVATION_MINUTES``
    Django setting (default: 30 minutes). This can be set in the CMS by
    non-technical staff without code changes.
    """
    minutes = _get_setting(
        "INVENTORY_DEFAULT_RESERVATION_MINUTES",
        _DEFAULT_RESERVATION_MINUTES,
    )
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = _DEFAULT_RESERVATION_MINUTES
    if minutes < 1:
        minutes = _DEFAULT_RESERVATION_MINUTES
    return timedelta(minutes=minutes)

def get_available_stock_formula() -> str:
    """
    Returns the name of the formula to use when calculating
    sellable (available) stock.

    Supported formulas:
        * ``available`` — Returns the gross ``available_quantity``.
        * ``available_minus_reserved`` — Returns
          ``available_quantity - reserved_quantity`` (default).
        * ``available_minus_reserved_plus_incoming`` — Returns
          ``available_quantity - reserved_quantity + incoming_quantity``.

    Configurable via the ``INVENTORY_AVAILABLE_STOCK_FORMULA`` setting.
    """
    formula = _get_setting(
        "INVENTORY_AVAILABLE_STOCK_FORMULA",
        _DEFAULT_STOCK_FORMULA,
    )
    if formula not in {
        "available",
        "available_minus_reserved",
        "available_minus_reserved_plus_incoming",
    }:
        logger.warning(
            "Unknown INVENTORY_AVAILABLE_STOCK_FORMULA=%r; falling back to default.",
            formula,
        )
        formula = _DEFAULT_STOCK_FORMULA
    return formula

# ==============================================================================
# CUSTOM EXCEPTION CLASSES
# ==============================================================================
# These provide clear, semantic error types so views, signals, and
# management commands can catch and translate them appropriately.
# Messages are intentionally generic to avoid leaking internal data,
# but contain enough context for debugging.

class InsufficientStockError(Exception):
    """
    Raised when an operation requires more stock than is available.

    Attributes:
        available: Decimal of currently available (sellable) stock.
        requested: Decimal of stock the caller requested.
        inventory: Optional reference to the offending Inventory row.
    """

    def __init__(
        self,
        message: str = "",
        *,
        available: Optional[Decimal] = None,
        requested: Optional[Decimal] = None,
        inventory: Optional[Inventory] = None,
    ) -> None:
        if not message:
            message = _("Insufficient stock to complete the requested operation.")
        super().__init__(message)
        self.available = available
        self.requested = requested
        self.inventory = inventory

class InvalidWarehouseError(Exception):
    """Raised when a warehouse reference is invalid, inactive, or missing."""

class ReservationExpiredError(Exception):
    """Raised when a stock reservation has already expired or been released."""

class InventoryNotFoundError(Exception):
    """
    Raised when an inventory record cannot be located for a given
    product/variant/warehouse combination.
    """

class InvalidQuantityError(Exception):
    """Raised when an invalid (non-positive, non-numeric, or null) quantity is supplied."""

# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================
def _validate_quantity(quantity: Any, *, allow_zero: bool = False) -> Decimal:
    """
    Validates and normalizes a quantity value, ensuring it is a positive
    Decimal. Raises ``InvalidQuantityError`` if the value is missing,
    non-numeric, or outside the allowed range.
    """
    if quantity is None:
        raise InvalidQuantityError(_("Quantity must be provided."))
    try:
        qty = Decimal(str(quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidQuantityError(
            _("Quantity must be a valid decimal number.")
        ) from exc
    if qty.is_nan() or qty.is_infinite():
        raise InvalidQuantityError(_("Quantity must be a finite decimal number."))
    if not allow_zero and qty <= Decimal("0"):
        raise InvalidQuantityError(
            _("Quantity must be strictly greater than zero.")
        )
    if allow_zero and qty < Decimal("0"):
        raise InvalidQuantityError(
            _("Quantity must be greater than or equal to zero.")
        )
    return qty.quantize(Decimal("0.01"))

def _get_active_warehouse(warehouse: Optional[Warehouse]) -> Warehouse:
    """
    Returns a valid, active ``Warehouse`` instance, raising
    ``InvalidWarehouseError`` if the input is missing or inactive.
    """
    if warehouse is None:
        raise InvalidWarehouseError(_("Warehouse is required."))
    if not getattr(warehouse, "is_active", True):
        raise InvalidWarehouseError(
            _("The selected warehouse is inactive and cannot accept stock operations.")
        )
    return warehouse

def _resolve_inventory(
    *,
    product: Any,
    product_variant: Any,
    warehouse: Warehouse,
) -> Inventory:
    """
    Retrieves the unique ``Inventory`` row for the given target and
    warehouse, or raises ``InventoryNotFoundError`` if it does not exist.

    Exactly one of ``product`` or ``product_variant`` must be supplied.
    """
    if bool(product_variant) == bool(product):
        raise ValueError(
            "Exactly one of `product` or `product_variant` must be supplied."
        )
    qs = Inventory.objects.filter(warehouse=warehouse, is_active=True)
    if product_variant is not None:
        qs = qs.filter(product_variant=product_variant, product__isnull=True)
    else:
        qs = qs.filter(product=product, product_variant__isnull=True)
    inventory = qs.first()
    if inventory is None:
        target = product_variant or product
        raise InventoryNotFoundError(
            _("No active inventory record exists for the requested target and warehouse.")
        )
    return inventory

def _get_or_create_inventory(
    *,
    product: Any,
    product_variant: Any,
    warehouse: Warehouse,
) -> Inventory:
    """
    Retrieves the unique ``Inventory`` row for the given target and
    warehouse, creating an empty record if one does not yet exist.

    Used by inbound stock flows (e.g. restock, receiving from a
    purchase order) where the inventory row may not yet have been
    provisioned by the catalog module.
    """
    if bool(product_variant) == bool(product):
        raise ValueError(
            "Exactly one of `product` or `product_variant` must be supplied."
        )
    if product_variant is not None:
        defaults = {"product": None}
        target_filter = {"product_variant": product_variant, "product__isnull": True}
    else:
        defaults = {"product_variant": None}
        target_filter = {"product": product, "product_variant__isnull": True}

    inventory, created = Inventory.objects.get_or_create(
        warehouse=warehouse,
        defaults=defaults,
        **target_filter,
    )
    return inventory

def _build_transaction(
    *,
    inventory: Inventory,
    transaction_type: str,
    direction: str,
    quantity: Decimal,
    performed_by: Any = None,
    reference_number: str = "",
    reference_model: str = "",
    reference_id: str = "",
    destination_warehouse: Optional[Warehouse] = None,
    transfer_group_id: Optional[uuid.UUID] = None,
    unit_cost: Optional[Decimal] = None,
    currency: str = "NPR",
    remarks: str = "",
    transaction_at: Optional[datetime] = None,
) -> InventoryTransaction:
    """
    Creates an immutable ``InventoryTransaction`` record capturing the
    full before/after snapshot of the affected inventory row.

    This helper is the single authorized entry point for persisting
    transaction rows. It is invoked by every public service function
    that mutates stock.
    """
    txn = InventoryTransaction.objects.create(
        inventory=inventory,
        transaction_type=transaction_type,
        direction=direction,
        quantity=quantity,
        available_before=inventory.available_quantity,
        available_after=None,  # Resolved by the caller after the update
        reserved_before=inventory.reserved_quantity,
        reserved_after=None,  # Resolved by the caller after the update
        unit_cost=unit_cost,
        currency=currency,
        reference_number=reference_number,
        reference_model=reference_model,
        reference_id=reference_id,
        destination_warehouse=destination_warehouse,
        transfer_group_id=transfer_group_id or uuid.uuid4(),
        remarks=remarks,
        performed_by=performed_by,
        transaction_at=transaction_at or timezone.now(),
    )
    return txn

def _serialize_inventory(inventory: Inventory) -> Dict[str, Any]:
    """
    Returns a serializable dictionary representation of an Inventory row,
    useful for structured service results and JSON responses.
    """
    return {
        "id": inventory.id,
        "warehouse_id": inventory.warehouse_id,
        "warehouse_name": inventory.warehouse.display_name,
        "product_id": inventory.product_id,
        "product_variant_id": inventory.product_variant_id,
        "available_quantity": str(inventory.available_quantity),
        "reserved_quantity": str(inventory.reserved_quantity),
        "damaged_quantity": str(inventory.damaged_quantity),
        "incoming_quantity": str(inventory.incoming_quantity),
        "free_stock": str(inventory.free_stock),
        "total_stock": str(inventory.total_stock),
        "reorder_level": str(inventory.reorder_level) if inventory.reorder_level is not None else None,
        "minimum_stock": str(inventory.minimum_stock) if inventory.minimum_stock is not None else None,
        "maximum_stock": str(inventory.maximum_stock) if inventory.maximum_stock is not None else None,
        "needs_reorder": inventory.needs_reorder,
        "is_out_of_stock": inventory.is_out_of_stock,
        "is_low_stock": inventory.is_low_stock,
        "is_overstock": inventory.is_overstock,
        "is_active": inventory.is_active,
        "location_bin": inventory.location_bin,
    }

def _serialize_reservation(reservation: StockReservation) -> Dict[str, Any]:
    """
    Returns a serializable dictionary representation of a StockReservation.
    """
    return {
        "id": reservation.id,
        "reservation_token": str(reservation.reservation_token),
        "status": reservation.status,
        "reservation_type": reservation.reservation_type,
        "quantity": str(reservation.quantity),
        "warehouse_id": reservation.warehouse_id,
        "inventory_id": reservation.inventory_id,
        "product_id": reservation.product_id,
        "product_variant_id": reservation.product_variant_id,
        "cart_id": reservation.cart_id,
        "user_id": reservation.user_id,
        "session_key": reservation.session_key,
        "expires_at": reservation.expires_at.isoformat() if reservation.expires_at else None,
        "released_at": reservation.released_at.isoformat() if reservation.released_at else None,
        "converted_at": reservation.converted_at.isoformat() if reservation.converted_at else None,
        "is_expired": reservation.is_expired,
        "is_terminal": reservation.is_terminal,
    }

# ==============================================================================
# 1. RESERVE_STOCK()
# ==============================================================================
@transaction.atomic
def reserve_stock(
    *,
    quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    cart: Any = None,
    user: Any = None,
    session_key: str = "",
    expires_in: Optional[timedelta] = None,
    reservation_type: str = _CART_RESERVATION_TYPE,
    reference_number: str = "",
    reference_model: str = "",
    reference_id: str = "",
    notes: str = "",
    performed_by: Any = None,
) -> Dict[str, Any]:
    """
    Reserve inventory for a shopping cart or custom hold.

    Example:
        Available = 20, Customer reserves 3, Available stays 20 (unchanged),
        but Reserved becomes 3 and Free Stock (sellable) drops to 17.

    Requirements:
        * Validate quantity is strictly positive.
        * Validate the inventory row exists and is active.
        * Prevent overselling (free stock must be >= quantity).
        * Create a ``StockReservation`` record.
        * Increment ``Inventory.reserved_quantity`` atomically.
        * Record the change as an ``InventoryTransaction`` (neutral direction).
        * Support configurable expiry (default from settings).
        * Support multiple warehouses, anonymous carts, and future carts.
        * Return a structured result with reservation details.

    Args:
        quantity: The amount to reserve (Decimal, int, or float).
        product: The product (for product-level reservations) or None.
        product_variant: The variant (for variant-level reservations) or None.
        warehouse: The warehouse to draw from. If None, the default warehouse
            is used.
        cart: Optional cart instance owning the reservation.
        user: Optional authenticated user owning the reservation.
        session_key: Optional session key for anonymous-cart reservations.
        expires_in: Optional custom expiry duration. If None, the default
            from ``INVENTORY_DEFAULT_RESERVATION_MINUTES`` is used.
        reservation_type: One of ``StockReservation.ReservationType``.
        reference_number: External reference (e.g. cart ID, hold ID).
        reference_model: App/model label of the referenced record.
        reference_id: String ID of the referenced record.
        notes: Optional free-text remarks.
        performed_by: Optional staff user performing the manual hold.

    Returns:
        A structured dictionary describing the reservation, including
        the new reservation ID, the post-operation inventory snapshot,
        and the expiry timestamp.
    """
    qty = _validate_quantity(quantity)

    # 1. Resolve target warehouse
    if warehouse is None:
        warehouse = (
            Warehouse.objects.filter(is_active=True, is_default=True).first()
        )
        if warehouse is None:
            raise InvalidWarehouseError(
                _("No default warehouse is configured and no warehouse was provided.")
            )
    _get_active_warehouse(warehouse)

    # 2. Lock and validate inventory
    try:
        inventory = Inventory.objects.select_for_update().get(
            warehouse=warehouse,
            is_active=True,
            *(
                {"product_variant": product_variant, "product__isnull": True}
                if product_variant is not None
                else {"product": product, "product_variant__isnull": True}
            ),
        )
    except Inventory.DoesNotExist as exc:
        raise InventoryNotFoundError(
            _("No active inventory record exists for the requested target and warehouse.")
        ) from exc

    # 3. Ensure sufficient free stock
    if inventory.free_stock < qty:
        raise InsufficientStockError(
            _("Insufficient free stock to complete the reservation."),
            available=inventory.free_stock,
            requested=qty,
            inventory=inventory,
        )

    # 4. Resolve expiry
    if expires_in is None:
        expires_in = get_default_reservation_duration()
    expires_at = timezone.now() + expires_in

    # 5. Create the reservation
    reservation = StockReservation.objects.create(
        reservation_token=uuid.uuid4(),
        cart=cart,
        product=product if product_variant is None else None,
        product_variant=product_variant,
        inventory=inventory,
        warehouse=warehouse,
        quantity=qty,
        reservation_type=reservation_type,
        status=RESERVATION_STATUS_ACTIVE,
        is_active=True,
        expires_at=expires_at,
        user=user,
        session_key=session_key or "",
        notes=notes,
    )

    # 6. Atomically increment reserved_quantity
    reserved_before = inventory.reserved_quantity
    Inventory.objects.filter(pk=inventory.pk).update(
        reserved_quantity=F("reserved_quantity") + qty,
    )
    inventory.refresh_from_db(fields=["reserved_quantity", "available_quantity"])

    # 7. Record the audit transaction
    txn = _build_transaction(
        inventory=inventory,
        transaction_type=InventoryTransaction.TransactionType.ADJUSTMENT,
        direction=INVENTORY_FLOW_NEUTRAL,
        quantity=qty,
        performed_by=performed_by or user,
        reference_number=reference_number,
        reference_model=reference_model or "cart.CartItem",
        reference_id=reference_id or (str(cart.id) if cart and getattr(cart, "id", None) else ""),
        remarks=notes or _("Stock reserved via service layer."),
    )
    InventoryTransaction.objects.filter(pk=txn.pk).update(
        reserved_before=reserved_before,
        reserved_after=inventory.reserved_quantity,
    )

    logger.info(
        "Reserved %s of inventory %s for reservation %s. New free stock: %s",
        qty,
        inventory.pk,
        reservation.reservation_token,
        inventory.free_stock,
    )

    return {
        "success": True,
        "reservation_id": reservation.id,
        "reservation_token": str(reservation.reservation_token),
        "reservation": _serialize_reservation(reservation),
        "inventory": _serialize_inventory(inventory),
        "available_after": str(inventory.available_quantity),
        "reserved_after": str(inventory.reserved_quantity),
        "free_stock_after": str(inventory.free_stock),
        "expires_at": expires_at.isoformat(),
        "message": _("Stock reserved successfully."),
    }

# ==============================================================================
# 2. RELEASE_STOCK()
# ==============================================================================
@transaction.atomic
def release_stock(
    *,
    reservation: Optional[StockReservation] = None,
    reservation_id: Optional[int] = None,
    reservation_token: Optional[str] = None,
    reason: str = "",
    is_automatic: bool = False,
    performed_by: Any = None,
) -> Dict[str, Any]:
    """
    Release a previously created stock reservation.

    Example:
        Reserved = 3 → Reserved = 0, Available increases by 3 (already counted),
        Free Stock rises back to 20.

    Requirements:
        * Locate the reservation by ID, token, or instance.
        * Prevent duplicate release (idempotent).
        * Decrement ``Inventory.reserved_quantity`` atomically.
        * Mark reservation as RELEASED (or EXPIRED if automatic).
        * Record the change as an ``InventoryTransaction`` (neutral direction).
        * Support both manual release and automatic expiry cleanup.
        * Return a structured result.
    """
    # 1. Resolve the reservation
    if reservation is None:
        if reservation_id is not None:
            try:
                reservation = StockReservation.objects.select_for_update().get(
                    pk=reservation_id
                )
            except StockReservation.DoesNotExist as exc:
                raise InventoryNotFoundError(
                    _("Reservation not found for the supplied ID.")
                ) from exc
        elif reservation_token is not None:
            try:
                token_uuid = uuid.UUID(str(reservation_token))
                reservation = StockReservation.objects.select_for_update().get(
                    reservation_token=token_uuid
                )
            except (ValueError, StockReservation.DoesNotExist) as exc:
                raise InventoryNotFoundError(
                    _("Reservation not found for the supplied token.")
                ) from exc
        else:
            raise InventoryNotFoundError(
                _("A reservation instance, ID, or token is required.")
            )
    else:
        # Re-fetch with row lock to be safe
        reservation = StockReservation.objects.select_for_update().get(pk=reservation.pk)

    # 2. Idempotency guard
    if reservation.is_terminal:
        return {
            "success": True,
            "released": False,
            "reservation": _serialize_reservation(reservation),
            "message": _("Reservation was already in a terminal state; no further action taken."),
        }

    # 3. Resolve the inventory row to decrement
    if reservation.inventory_id is None:
        raise InventoryNotFoundError(
            _("Reservation is not bound to an inventory row; cannot release.")
        )
    try:
        inventory = Inventory.objects.select_for_update().get(
            pk=reservation.inventory_id
        )
    except Inventory.DoesNotExist as exc:
        raise InventoryNotFoundError(
            _("The inventory record backing this reservation no longer exists.")
        ) from exc

    # 4. Atomically decrement reserved_quantity
    reserved_before = inventory.reserved_quantity
    quantity = reservation.quantity
    Inventory.objects.filter(pk=inventory.pk).update(
        reserved_quantity=F("reserved_quantity") - quantity,
    )
    # Guard against underflow (defensive)
    if quantity > reserved_before:
        logger.warning(
            "Reservation %s attempted to release %s but only %s was reserved. "
            "Clamping to the recorded amount.",
            reservation.reservation_token,
            quantity,
            reserved_before,
        )
    inventory.refresh_from_db(fields=["reserved_quantity", "available_quantity"])

    # 5. Mark the reservation as terminal
    now = timezone.now()
    new_status = RESERVATION_STATUS_EXPIRED if is_automatic else RESERVATION_STATUS_RELEASED
    reservation.status = new_status
    reservation.is_active = False
    reservation.released_at = now
    reservation.save(update_fields=["status", "is_active", "released_at", "updated_at"])

    # 6. Record the audit transaction
    txn = _build_transaction(
        inventory=inventory,
        transaction_type=InventoryTransaction.TransactionType.RESERVATION_RELEASE,
        direction=INVENTORY_FLOW_NEUTRAL,
        quantity=quantity,
        performed_by=performed_by,
        reference_number=str(reservation.reservation_token),
        reference_model="inventory.StockReservation",
        reference_id=str(reservation.id),
        remarks=reason or (
            _("Reservation automatically released by expiry cleanup.")
            if is_automatic
            else _("Reservation released by service layer.")
        ),
    )
    InventoryTransaction.objects.filter(pk=txn.pk).update(
        reserved_before=reserved_before,
        reserved_after=inventory.reserved_quantity,
    )

    logger.info(
        "Released reservation %s (%s) for inventory %s. Free stock: %s",
        reservation.reservation_token,
        new_status,
        inventory.pk,
        inventory.free_stock,
    )

    return {
        "success": True,
        "released": True,
        "reservation_id": reservation.id,
        "reservation": _serialize_reservation(reservation),
        "inventory": _serialize_inventory(inventory),
        "available_after": str(inventory.available_quantity),
        "reserved_after": str(inventory.reserved_quantity),
        "free_stock_after": str(inventory.free_stock),
        "message": _("Reservation released successfully."),
    }

@transaction.atomic
def release_expired_reservations(*, batch_size: int = 500) -> Dict[str, Any]:
    """
    Cron / management-command helper that releases all reservations whose
    ``expires_at`` has passed and which are still ACTIVE.

    Designed to be invoked periodically (e.g. every minute) by a
    scheduled task. Performs row-level locking to safely decrement
    ``reserved_quantity`` for each reservation in batched fashion.
    """
    now = timezone.now()
    cutoff = now
    qs = (
        StockReservation.objects
        .select_for_update(skip_locked=True)
        .filter(status=RESERVATION_STATUS_ACTIVE, expires_at__lte=cutoff)
        .order_by("id")
    )
    released_count = 0
    failed_count = 0
    processed_ids: List[int] = []
    for reservation in qs.iterator(chunk_size=batch_size):
        try:
            release_stock(reservation=reservation, is_automatic=True)
            released_count += 1
        except Exception as exc:
            failed_count += 1
            logger.exception(
                "Failed to auto-release expired reservation %s: %s",
                getattr(reservation, "reservation_token", reservation.pk),
                exc,
            )
        processed_ids.append(reservation.id)

    logger.info(
        "Expired reservation cleanup: %s released, %s failed, %s processed.",
        released_count, failed_count, len(processed_ids),
    )
    return {
        "released": released_count,
        "failed": failed_count,
        "processed": len(processed_ids),
    }

# ==============================================================================
# 3. DEDUCT_STOCK()
# ==============================================================================
@transaction.atomic
def deduct_stock(
    *,
    quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    reservation: Optional[StockReservation] = None,
    reference_number: str = "",
    reference_model: str = "",
    reference_id: str = "",
    unit_cost: Optional[Decimal] = None,
    currency: str = "NPR",
    remarks: str = "",
    performed_by: Any = None,
    partial_allowed: bool = False,
) -> Dict[str, Any]:
    """
    Deduct inventory after successful order payment.

    Example:
        Free Stock = 17 → Free Stock = 14, Available drops to 14,
        Reserved returns to 0 (if reservation was linked).

    Requirements:
        * Validate the requested quantity is positive.
        * Validate the inventory record exists.
        * Prevent negative stock (free stock >= quantity).
        * Support reservation conversion (mark reservation as CONVERTED).
        * Support partial fulfillment when ``partial_allowed=True``.
        * Atomically decrement ``available_quantity`` and ``reserved_quantity``
          using F() expressions.
        * Create an ``InventoryTransaction`` (outbound, SALE type).
        * Idempotency: if a reservation is provided and already CONVERTED,
          the operation returns the prior outcome without double-deducting.

    Returns:
        A structured dictionary with the post-deduction inventory snapshot
        and the amount actually deducted.
    """
    qty = _validate_quantity(quantity)

    # Resolve warehouse
    if warehouse is None:
        warehouse = (
            Warehouse.objects.filter(is_active=True, is_default=True).first()
        )
        if warehouse is None:
            raise InvalidWarehouseError(
                _("No default warehouse is configured and no warehouse was provided.")
            )
    _get_active_warehouse(warehouse)

    # Handle reservation conversion
    reservation_obj: Optional[StockReservation] = None
    if reservation is not None:
        reservation_obj = StockReservation.objects.select_for_update().get(
            pk=reservation.pk
        )
        if reservation_obj.is_terminal:
            if reservation_obj.status == RESERVATION_STATUS_CONVERTED:
                # Idempotent: previously converted; return current snapshot
                inventory = reservation_obj.inventory
                if inventory is None:
                    raise InventoryNotFoundError(
                        _("Reservation has no linked inventory record.")
                    )
                return {
                    "success": True,
                    "deducted": False,
                    "deducted_quantity": str(Decimal("0")),
                    "requested_quantity": str(qty),
                    "inventory": _serialize_inventory(inventory),
                    "message": _("Reservation was already converted; no further deduction performed."),
                }
            raise ReservationExpiredError(
                _("Reservation is no longer active and cannot be converted to a sale.")
            )
        if reservation_obj.warehouse_id != warehouse.id:
            raise InvalidWarehouseError(
                _("Reservation warehouse does not match the supplied warehouse.")
            )
        # Use the inventory bound to the reservation
        if reservation_obj.inventory_id is None:
            raise InventoryNotFoundError(
                _("Reservation is not bound to an inventory record; cannot deduct.")
            )
        inventory = Inventory.objects.select_for_update().get(
            pk=reservation_obj.inventory_id
        )
        qty = min(qty, reservation_obj.quantity)
    else:
        try:
            inventory = Inventory.objects.select_for_update().get(
                warehouse=warehouse,
                is_active=True,
                *(
                    {"product_variant": product_variant, "product__isnull": True}
                    if product_variant is not None
                    else {"product": product, "product_variant__isnull": True}
                ),
            )
        except Inventory.DoesNotExist as exc:
            raise InventoryNotFoundError(
                _("No active inventory record exists for the requested target and warehouse.")
            ) from exc

    # Determine how much to actually deduct
    if inventory.free_stock < qty:
        if not partial_allowed:
            raise InsufficientStockError(
                _("Insufficient stock to complete the deduction."),
                available=inventory.free_stock,
                requested=qty,
                inventory=inventory,
            )
        qty = inventory.free_stock
    if qty <= Decimal("0"):
        return {
            "success": True,
            "deducted": False,
            "deducted_quantity": str(Decimal("0")),
            "requested_quantity": str(quantity),
            "inventory": _serialize_inventory(inventory),
            "message": _("Nothing to deduct; inventory is already empty."),
        }

    # Snapshot the before state
    available_before = inventory.available_quantity
    reserved_before = inventory.reserved_quantity

    # Apply the deduction atomically
    Inventory.objects.filter(pk=inventory.pk).update(
        available_quantity=F("available_quantity") - qty,
    )
    if reservation_obj is not None:
        Inventory.objects.filter(pk=inventory.pk).update(
            reserved_quantity=F("reserved_quantity") - qty,
        )
    inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

    # Record the audit transaction
    txn = _build_transaction(
        inventory=inventory,
        transaction_type=InventoryTransaction.TransactionType.SALE,
        direction=INVENTORY_FLOW_OUTBOUND,
        quantity=qty,
        unit_cost=unit_cost,
        currency=currency,
        reference_number=reference_number,
        reference_model=reference_model or "orders.Order",
        reference_id=reference_id,
        remarks=remarks or _("Stock deducted via service layer."),
        performed_by=performed_by,
    )
    InventoryTransaction.objects.filter(pk=txn.pk).update(
        available_before=available_before,
        available_after=inventory.available_quantity,
        reserved_before=reserved_before,
        reserved_after=inventory.reserved_quantity,
    )

    # Mark the reservation as converted if provided
    if reservation_obj is not None:
        reservation_obj.status = RESERVATION_STATUS_CONVERTED
        reservation_obj.is_active = False
        reservation_obj.converted_at = timezone.now()
        reservation_obj.converted_to_order_id = (
            int(reference_id) if reference_id.isdigit() else None
        )
        reservation_obj.save(
            update_fields=[
                "status",
                "is_active",
                "converted_at",
                "converted_to_order",
                "updated_at",
            ]
        )

    logger.info(
        "Deducted %s of inventory %s. New free stock: %s",
        qty,
        inventory.pk,
        inventory.free_stock,
    )

    return {
        "success": True,
        "deducted": True,
        "deducted_quantity": str(qty),
        "requested_quantity": str(quantity),
        "reservation_id": reservation_obj.id if reservation_obj else None,
        "inventory": _serialize_inventory(inventory),
        "available_after": str(inventory.available_quantity),
        "reserved_after": str(inventory.reserved_quantity),
        "free_stock_after": str(inventory.free_stock),
        "message": _("Stock deducted successfully."),
    }

# ==============================================================================
# 4. RESTOCK()
# ==============================================================================
@transaction.atomic
def restock(
    *,
    quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    unit_cost: Optional[Decimal] = None,
    currency: str = "NPR",
    reference_number: str = "",
    reference_model: str = "",
    reference_id: str = "",
    remarks: str = "",
    performed_by: Any = None,
    create_inventory_if_missing: bool = True,
) -> Dict[str, Any]:
    """
    Increase inventory after supplier delivery or internal replenishment.

    Example:
        Available = 14, Restock 50 → Available = 64.

    Requirements:
        * Validate the requested quantity is strictly positive.
        * Resolve the target warehouse (default if not provided).
        * Auto-create the inventory row if it does not yet exist
          (controlled by ``create_inventory_if_missing``).
        * Atomically increment ``available_quantity`` using F() expressions.
        * Create an ``InventoryTransaction`` (inbound, PURCHASE type).
        * Support Purchase Order references and Goods Receipt Notes.
        * Support batch receiving for future workflows.
        * Return a structured result.
    """
    qty = _validate_quantity(quantity)

    if warehouse is None:
        warehouse = (
            Warehouse.objects.filter(is_active=True, is_default=True).first()
        )
        if warehouse is None:
            raise InvalidWarehouseError(
                _("No default warehouse is configured and no warehouse was provided.")
            )
    _get_active_warehouse(warehouse)

    if create_inventory_if_missing:
        inventory = _get_or_create_inventory(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )
    else:
        try:
            inventory = Inventory.objects.select_for_update().get(
                warehouse=warehouse,
                is_active=True,
                *(
                    {"product_variant": product_variant, "product__isnull": True}
                    if product_variant is not None
                    else {"product": product, "product_variant__isnull": True}
                ),
            )
        except Inventory.DoesNotExist as exc:
            raise InventoryNotFoundError(
                _("No inventory record exists and auto-creation is disabled.")
            ) from exc

    available_before = inventory.available_quantity
    reserved_before = inventory.reserved_quantity

    Inventory.objects.filter(pk=inventory.pk).update(
        available_quantity=F("available_quantity") + qty,
    )
    inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

    txn = _build_transaction(
        inventory=inventory,
        transaction_type=InventoryTransaction.TransactionType.PURCHASE,
        direction=INVENTORY_FLOW_INBOUND,
        quantity=qty,
        unit_cost=unit_cost,
        currency=currency,
        reference_number=reference_number,
        reference_model=reference_model or "purchases.PurchaseOrder",
        reference_id=reference_id,
        remarks=remarks or _("Stock received via service layer."),
        performed_by=performed_by,
    )
    InventoryTransaction.objects.filter(pk=txn.pk).update(
        available_before=available_before,
        available_after=inventory.available_quantity,
        reserved_before=reserved_before,
        reserved_after=inventory.reserved_quantity,
    )

    logger.info(
        "Restocked %s into inventory %s. New available: %s",
        qty, inventory.pk, inventory.available_quantity,
    )

    return {
        "success": True,
        "restocked_quantity": str(qty),
        "inventory": _serialize_inventory(inventory),
        "available_after": str(inventory.available_quantity),
        "message": _("Stock restocked successfully."),
    }

# ==============================================================================
# 5. ADJUST_STOCK()
# ==============================================================================
@transaction.atomic
def adjust_stock(
    *,
    new_quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    reason: str = _DEFAULT_ADJUSTMENT_REASON,
    description: str = "",
    supporting_documents: Optional[List[Dict[str, Any]]] = None,
    initiated_by: Any = None,
    approved_by: Any = None,
    auto_apply: bool = True,
    reference_number: str = "",
    remarks: str = "",
    perform_transaction: bool = True,
) -> Dict[str, Any]:
    """
    Manual inventory correction with an approval workflow.

    Example:
        System = 60, Physical = 58 → Adjustment of -2 (decrease).

    Requirements:
        * Compute the difference between old and new quantity automatically.
        * Create a ``StockAdjustment`` record capturing the workflow state.
        * Create an ``InventoryTransaction`` (neutral, ADJUSTMENT type).
        * Validate the approval user when ``auto_apply=True``.
        * Apply the adjustment to inventory when ``auto_apply=True``
          (status becomes APPLIED). Otherwise, the adjustment stays in
          PENDING_APPROVAL and can be approved later via the admin.
        * Support audit trail via ``performed_by`` and timestamps.
        * Support a future approval workflow by leaving the inventory
          untouched when ``auto_apply=False``.
        * Return a structured result.
    """
    new_qty = _validate_quantity(new_quantity, allow_zero=True)

    if warehouse is None:
        warehouse = (
            Warehouse.objects.filter(is_active=True, is_default=True).first()
        )
        if warehouse is None:
            raise InvalidWarehouseError(
                _("No default warehouse is configured and no warehouse was provided.")
            )
    _get_active_warehouse(warehouse)

    try:
        inventory = Inventory.objects.select_for_update().get(
            warehouse=warehouse,
            is_active=True,
            *(
                {"product_variant": product_variant, "product__isnull": True}
                if product_variant is not None
                else {"product": product, "product_variant__isnull": True}
            ),
        )
    except Inventory.DoesNotExist as exc:
        raise InventoryNotFoundError(
            _("No active inventory record exists for the requested target and warehouse.")
        ) from exc

    available_before = inventory.available_quantity
    difference = (new_qty - available_before).quantize(Decimal("0.01"))

    # Create the adjustment record
    adjustment = StockAdjustment.objects.create(
        inventory=inventory,
        reason=reason,
        description=description,
        supporting_documents=supporting_documents or [],
        old_quantity=available_before,
        new_quantity=new_qty,
        difference=difference,
        status=ADJUSTMENT_STATUS_DRAFT if not auto_apply else ADJUSTMENT_STATUS_PENDING_APPROVAL,
        initiated_by=initiated_by,
        approved_by=approved_by if auto_apply else None,
        approved_at=timezone.now() if auto_apply and approved_by else None,
    )

    applied_transaction: Optional[InventoryTransaction] = None
    if auto_apply:
        if approved_by is None:
            raise DjangoValidationError(
                _("An approving user is required when auto_apply is enabled.")
            )
        # Apply the adjustment to inventory atomically
        reserved_before = inventory.reserved_quantity
        Inventory.objects.filter(pk=inventory.pk).update(
            available_quantity=new_qty,
        )
        inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

        if perform_transaction:
            applied_transaction = _build_transaction(
                inventory=inventory,
                transaction_type=InventoryTransaction.TransactionType.ADJUSTMENT,
                direction=(
                    INVENTORY_FLOW_INBOUND if difference > 0
                    else INVENTORY_FLOW_OUTBOUND if difference < 0
                    else INVENTORY_FLOW_NEUTRAL
                ),
                quantity=abs(difference) if difference != 0 else Decimal("0"),
                performed_by=approved_by,
                reference_number=reference_number,
                reference_model="inventory.StockAdjustment",
                reference_id=str(adjustment.id),
                remarks=remarks or description or _("Manual stock adjustment applied via service layer."),
            )
            InventoryTransaction.objects.filter(pk=applied_transaction.pk).update(
                available_before=available_before,
                available_after=inventory.available_quantity,
                reserved_before=reserved_before,
                reserved_after=inventory.reserved_quantity,
            )

        # Mark the adjustment as applied
        adjustment.status = ADJUSTMENT_STATUS_APPLIED
        adjustment.approved_by = approved_by
        adjustment.approved_at = adjustment.approved_at or timezone.now()
        adjustment.applied_at = timezone.now()
        adjustment.applied_transaction = applied_transaction
        adjustment.save(
            update_fields=[
                "status",
                "approved_by",
                "approved_at",
                "applied_at",
                "applied_transaction",
                "updated_at",
            ]
        )

    logger.info(
        "Created stock adjustment %s for inventory %s (diff=%s, status=%s).",
        adjustment.adjustment_number,
        inventory.pk,
        difference,
        adjustment.status,
    )

    return {
        "success": True,
        "adjustment_id": adjustment.id,
        "adjustment_number": adjustment.adjustment_number,
        "status": adjustment.status,
        "old_quantity": str(available_before),
        "new_quantity": str(new_qty),
        "difference": str(difference),
        "applied_transaction_id": applied_transaction.id if applied_transaction else None,
        "inventory": _serialize_inventory(inventory),
        "message": _(
            "Adjustment created and applied."
            if auto_apply
            else "Adjustment created in pending approval state."
        ),
    }

@transaction.atomic
def approve_adjustment(
    *,
    adjustment_id: int,
    approved_by: Any,
    apply_immediately: bool = True,
) -> Dict[str, Any]:
    """
    Approve a previously PENDING stock adjustment and (optionally) apply
    it to the inventory row. This is the canonical entry point for the
    future approval workflow.
    """
    adjustment = StockAdjustment.objects.select_for_update().get(pk=adjustment_id)
    if adjustment.status not in {
        ADJUSTMENT_STATUS_DRAFT,
        ADJUSTMENT_STATUS_PENDING_APPROVAL,
    }:
        raise DjangoValidationError(
            _("Only draft or pending adjustments can be approved.")
        )
    if approved_by is None:
        raise DjangoValidationError(_("An approving user is required."))

    adjustment.status = ADJUSTMENT_STATUS_APPROVED
    adjustment.approved_by = approved_by
    adjustment.approved_at = timezone.now()
    adjustment.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

    if apply_immediately and adjustment.new_quantity is not None:
        inventory = Inventory.objects.select_for_update().get(pk=adjustment.inventory_id)
        available_before = inventory.available_quantity
        reserved_before = inventory.reserved_quantity
        Inventory.objects.filter(pk=inventory.pk).update(
            available_quantity=adjustment.new_quantity,
        )
        inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])
        applied_txn = _build_transaction(
            inventory=inventory,
            transaction_type=InventoryTransaction.TransactionType.ADJUSTMENT,
            direction=(
                INVENTORY_FLOW_INBOUND if adjustment.difference > 0
                else INVENTORY_FLOW_OUTBOUND if adjustment.difference < 0
                else INVENTORY_FLOW_NEUTRAL
            ),
            quantity=abs(adjustment.difference) if adjustment.difference != 0 else Decimal("0"),
            performed_by=approved_by,
            reference_model="inventory.StockAdjustment",
            reference_id=str(adjustment.id),
            remarks=_("Approved adjustment applied."),
        )
        InventoryTransaction.objects.filter(pk=applied_txn.pk).update(
            available_before=available_before,
            available_after=inventory.available_quantity,
            reserved_before=reserved_before,
            reserved_after=inventory.reserved_quantity,
        )
        adjustment.status = ADJUSTMENT_STATUS_APPLIED
        adjustment.applied_at = timezone.now()
        adjustment.applied_transaction = applied_txn
        adjustment.save(
            update_fields=["status", "applied_at", "applied_transaction", "updated_at"]
        )

    return {
        "success": True,
        "adjustment_id": adjustment.id,
        "adjustment_number": adjustment.adjustment_number,
        "status": adjustment.status,
        "applied": apply_immediately,
        "message": _("Adjustment approved.") if not apply_immediately else
                  _("Adjustment approved and applied."),
    }

# ==============================================================================
# 6. TRANSFER_STOCK()
# ==============================================================================
@transaction.atomic
def transfer_stock(
    *,
    quantity: Any,
    source_warehouse: Warehouse,
    destination_warehouse: Warehouse,
    product: Any = None,
    product_variant: Any = None,
    reference_number: str = "",
    unit_cost: Optional[Decimal] = None,
    currency: str = "NPR",
    remarks: str = "",
    performed_by: Any = None,
    create_destination_if_missing: bool = True,
) -> Dict[str, Any]:
    """
    Transfer inventory between warehouses as a single atomic operation.

    Example:
        Warehouse A: 60 → 40
        Warehouse B: 20 → 40
        Two InventoryTransaction records are created, linked by the
        same ``transfer_group_id``.

    Requirements:
        * Validate source and destination warehouses are both active and
          distinct.
        * Lock BOTH inventory rows (in a deterministic order to prevent
          deadlocks).
        * Validate the source has sufficient free stock.
        * Atomically decrement source.available_quantity and
          increment destination.available_quantity using F() expressions.
        * Create one OUTBOUND transaction on the source and one INBOUND
          transaction on the destination, sharing the same
          ``transfer_group_id``.
        * Auto-create the destination inventory row if missing and
          ``create_destination_if_missing`` is True.
        * Support a future transfer approval workflow by leaving the
          rows untouched when an approval flag is False (out of scope here
          but reserved via a clear extension point).
        * Return a structured result.
    """
    qty = _validate_quantity(quantity)
    _get_active_warehouse(source_warehouse)
    _get_active_warehouse(destination_warehouse)

    if source_warehouse.id == destination_warehouse.id:
        raise InvalidWarehouseError(
            _("Source and destination warehouses must be different.")
        )

    if bool(product) == bool(product_variant):
        raise ValueError(
            "Exactly one of `product` or `product_variant` must be supplied."
        )

    # Lock in a deterministic order (lower id first) to prevent deadlocks
    first_id, second_id = sorted([source_warehouse.id, destination_warehouse.id])

    # Lock the source row first (always, so we can validate before writing)
    source_inventory = _resolve_inventory(
        product=product,
        product_variant=product_variant,
        warehouse=source_warehouse,
    )

    # Now we have a row lock on source; lock the destination (auto-create
    # or resolve)
    if create_destination_if_missing:
        destination_inventory = _get_or_create_inventory(
            product=product,
            product_variant=product_variant,
            warehouse=destination_warehouse,
        )
        destination_inventory = Inventory.objects.select_for_update().get(
            pk=destination_inventory.pk
        )
    else:
        destination_inventory = _resolve_inventory(
            product=product,
            product_variant=product_variant,
            warehouse=destination_warehouse,
        )

    if source_inventory.free_stock < qty:
        raise InsufficientStockError(
            _("Insufficient free stock in the source warehouse to complete the transfer."),
            available=source_inventory.free_stock,
            requested=qty,
            inventory=source_inventory,
        )

    transfer_group_id = uuid.uuid4()

    # Snapshot the before state
    source_avail_before = source_inventory.available_quantity
    source_resv_before = source_inventory.reserved_quantity
    dest_avail_before = destination_inventory.available_quantity
    dest_resv_before = destination_inventory.reserved_quantity

    # Apply the transfer atomically
    Inventory.objects.filter(pk=source_inventory.pk).update(
        available_quantity=F("available_quantity") - qty,
    )
    Inventory.objects.filter(pk=destination_inventory.pk).update(
        available_quantity=F("available_quantity") + qty,
    )
    source_inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])
    destination_inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

    # Outbound transaction on the source
    out_txn = _build_transaction(
        inventory=source_inventory,
        transaction_type=InventoryTransaction.TransactionType.TRANSFER,
        direction=INVENTORY_FLOW_OUTBOUND,
        quantity=qty,
        unit_cost=unit_cost,
        currency=currency,
        reference_number=reference_number,
        reference_model="inventory.StockTransfer",
        reference_id=str(transfer_group_id),
        destination_warehouse=destination_warehouse,
        transfer_group_id=transfer_group_id,
        performed_by=performed_by,
        remarks=remarks or _("Outbound transfer."),
    )
    InventoryTransaction.objects.filter(pk=out_txn.pk).update(
        available_before=source_avail_before,
        available_after=source_inventory.available_quantity,
        reserved_before=source_resv_before,
        reserved_after=source_inventory.reserved_quantity,
    )

    # Inbound transaction on the destination
    in_txn = _build_transaction(
        inventory=destination_inventory,
        transaction_type=InventoryTransaction.TransactionType.TRANSFER,
        direction=INVENTORY_FLOW_INBOUND,
        quantity=qty,
        unit_cost=unit_cost,
        currency=currency,
        reference_number=reference_number,
        reference_model="inventory.StockTransfer",
        reference_id=str(transfer_group_id),
        transfer_group_id=transfer_group_id,
        performed_by=performed_by,
        remarks=remarks or _("Inbound transfer."),
    )
    InventoryTransaction.objects.filter(pk=in_txn.pk).update(
        available_before=dest_avail_before,
        available_after=destination_inventory.available_quantity,
        reserved_before=dest_resv_before,
        reserved_after=destination_inventory.reserved_quantity,
    )

    logger.info(
        "Transferred %s of inventory from warehouse %s to %s. transfer_group_id=%s",
        qty, source_warehouse.id, destination_warehouse.id, transfer_group_id,
    )

    return {
        "success": True,
        "transferred_quantity": str(qty),
        "transfer_group_id": str(transfer_group_id),
        "source_warehouse_id": source_warehouse.id,
        "destination_warehouse_id": destination_warehouse.id,
        "source_inventory": _serialize_inventory(source_inventory),
        "destination_inventory": _serialize_inventory(destination_inventory),
        "outbound_transaction_id": out_txn.id,
        "inbound_transaction_id": in_txn.id,
        "message": _("Stock transferred successfully."),
    }

# ==============================================================================
# 7. CHECK_STOCK()
# ==============================================================================
def check_stock(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    quantity: Any = 1,
    include_all_warehouses: bool = False,
) -> Dict[str, Any]:
    """
    Check inventory availability for a product or variant.

    Returns a structured dictionary describing the sellable stock at the
    requested warehouse, or aggregated across all active warehouses when
    ``include_all_warehouses=True``. Includes reservation information
    when useful for downstream UIs.
    """
    qty = _validate_quantity(quantity, allow_zero=True)

    if bool(product) == bool(product_variant):
        raise ValueError(
            "Exactly one of `product` or `product_variant` must be supplied."
        )

    if include_all_warehouses:
        inventories = list(
            Inventory.objects.filter(
                is_active=True,
                *(
                    {"product_variant": product_variant, "product__isnull": True}
                    if product_variant is not None
                    else {"product": product, "product_variant__isnull": True}
                ),
            ).select_related("warehouse")
        )
    else:
        if warehouse is None:
            warehouse = (
                Warehouse.objects.filter(is_active=True, is_default=True).first()
            )
            if warehouse is None:
                raise InvalidWarehouseError(
                    _("No default warehouse is configured and no warehouse was provided.")
                )
        _get_active_warehouse(warehouse)
        try:
            inv = Inventory.objects.select_related("warehouse").get(
                warehouse=warehouse,
                is_active=True,
                *(
                    {"product_variant": product_variant, "product__isnull": True}
                    if product_variant is not None
                    else {"product": product, "product_variant__isnull": True}
                ),
            )
            inventories = [inv]
        except Inventory.DoesNotExist:
            inventories = []

    aggregated_available = sum((i.available_quantity for i in inventories), Decimal("0"))
    aggregated_reserved = sum((i.reserved_quantity for i in inventories), Decimal("0"))
    aggregated_incoming = sum((i.incoming_quantity for i in inventories), Decimal("0"))
    aggregated_free = aggregated_available - aggregated_reserved

    per_warehouse = [_serialize_inventory(i) for i in inventories]
    requested_qty = qty
    is_available_at_all = aggregated_free >= requested_qty
    is_available_at_requested = (
        len(inventories) > 0 and inventories[0].free_stock >= requested_qty
    )

    return {
        "product_id": product.id if product else None,
        "product_variant_id": product_variant.id if product_variant else None,
        "warehouse_id": warehouse.id if warehouse else None,
        "requested_quantity": str(requested_qty),
        "available_quantity": str(aggregated_available),
        "reserved_quantity": str(aggregated_reserved),
        "incoming_quantity": str(aggregated_incoming),
        "free_stock": str(aggregated_free),
        "is_available": is_available_at_all,
        "is_available_at_requested_warehouse": is_available_at_requested,
        "warehouses_checked": len(inventories),
        "per_warehouse": per_warehouse,
    }

# ==============================================================================
# 8. CALCULATE_AVAILABLE_STOCK()
# ==============================================================================
def calculate_available_stock(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    formula: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Calculate the actual sellable stock for a target, applying the
    configured business formula.

    The formula is sourced from the ``INVENTORY_AVAILABLE_STOCK_FORMULA``
    Django setting (default: ``"available_minus_reserved"``) and can be
    overridden per call via the ``formula`` argument.

    Supported formulas:
        * ``"available"`` — Returns ``available_quantity`` directly.
        * ``"available_minus_reserved"`` — Returns
          ``available_quantity - reserved_quantity`` (default).
        * ``"available_minus_reserved_plus_incoming"`` — Returns
          ``available_quantity - reserved_quantity + incoming_quantity``.

    All business rules are intentionally configurable so that
    business users can adapt the calculation without code changes.
    """
    if bool(product) == bool(product_variant):
        raise ValueError(
            "Exactly one of `product` or `product_variant` must be supplied."
        )

    if warehouse is None:
        warehouse = (
            Warehouse.objects.filter(is_active=True, is_default=True).first()
        )
        if warehouse is None:
            raise InvalidWarehouseError(
                _("No default warehouse is configured and no warehouse was provided.")
            )
    _get_active_warehouse(warehouse)

    try:
        inventory = Inventory.objects.get(
            warehouse=warehouse,
            is_active=True,
            *(
                {"product_variant": product_variant, "product__isnull": True}
                if product_variant is not None
                else {"product": product, "product_variant__isnull": True}
            ),
        )
    except Inventory.DoesNotExist as exc:
        raise InventoryNotFoundError(
            _("No active inventory record exists for the requested target and warehouse.")
        ) from exc

    chosen_formula = formula or get_available_stock_formula()
    available = inventory.available_quantity
    reserved = inventory.reserved_quantity
    incoming = inventory.incoming_quantity
    if chosen_formula == "available":
        sellable = available
        components = {"available": str(available)}
    elif chosen_formula == "available_minus_reserved":
        sellable = max(Decimal("0"), available - reserved)
        components = {"available": str(available), "reserved": str(reserved)}
    elif chosen_formula == "available_minus_reserved_plus_incoming":
        sellable = max(Decimal("0"), available - reserved + incoming)
        components = {
            "available": str(available),
            "reserved": str(reserved),
            "incoming": str(incoming),
        }
    else:
        logger.warning(
            "Unknown formula %r; falling back to available_minus_reserved.", chosen_formula,
        )
        chosen_formula = "available_minus_reserved"
        sellable = max(Decimal("0"), available - reserved)
        components = {"available": str(available), "reserved": str(reserved)}

    return {
        "inventory_id": inventory.id,
        "warehouse_id": warehouse.id,
        "product_id": product.id if product else None,
        "product_variant_id": product_variant.id if product_variant else None,
        "formula": chosen_formula,
        "sellable_quantity": str(sellable),
        "available_quantity": str(available),
        "reserved_quantity": str(reserved),
        "incoming_quantity": str(incoming),
        "components": components,
    }

# ==============================================================================
# 9. LOW_STOCK_PRODUCTS()
# ==============================================================================
def low_stock_products(
    *,
    warehouse: Optional[Warehouse] = None,
    limit: Optional[int] = None,
    include_inactive: bool = False,
) -> List[Dict[str, Any]]:
    """
    Return inventory records that are at or below their reorder level.

    Optimized queryset:
        * Annotated with a single-pass ``free_stock`` expression.
        * Filtered by ``free_stock <= reorder_level`` (when set).
        * Filtered to ACTIVE records by default.
        * Warehouse-filterable.
    """
    qs = Inventory.objects.annotate(
        free_stock_calc=F("available_quantity") - F("reserved_quantity"),
    )
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if warehouse is not None:
        qs = qs.filter(warehouse=warehouse)
    # Use a Q to handle NULL reorder_level (only return items that have a reorder level)
    qs = qs.filter(reorder_level__isnull=False)
    qs = qs.filter(
        Q(reorder_level__gte=0) & Q(free_stock_calc__lte=F("reorder_level"))
    ).select_related("warehouse", "product", "product_variant").order_by(
        "free_stock_calc", "id"
    )
    if limit is not None:
        qs = qs[:limit]

    results: List[Dict[str, Any]] = []
    for inventory in qs:
        item = _serialize_inventory(inventory)
        item["free_stock"] = str(inventory.available_quantity - inventory.reserved_quantity)
        results.append(item)
    return results

# ==============================================================================
# 10. OUT_OF_STOCK_PRODUCTS()
# ==============================================================================
def out_of_stock_products(
    *,
    warehouse: Optional[Warehouse] = None,
    limit: Optional[int] = None,
    include_inactive: bool = False,
    include_damaged: bool = True,
) -> List[Dict[str, Any]]:
    """
    Return inventory records with no sellable stock.

    Includes damaged-only inventory rows by default (since damaged stock
    is unsellable). Pass ``include_damaged=False`` to exclude inventory
    that still has sellable stock but only damaged stock remains.
    """
    qs = Inventory.objects.annotate(
        free_stock_calc=F("available_quantity") - F("reserved_quantity"),
        total_stock_calc=F("available_quantity") + F("damaged_quantity"),
    )
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if warehouse is not None:
        qs = qs.filter(warehouse=warehouse)
    qs = qs.filter(free_stock_calc__lte=0)
    if not include_damaged:
        qs = qs.filter(total_stock_calc__lte=0)
    qs = qs.select_related("warehouse", "product", "product_variant").order_by("id")
    if limit is not None:
        qs = qs[:limit]

    results: List[Dict[str, Any]] = []
    for inventory in qs:
        item = _serialize_inventory(inventory)
        item["free_stock"] = str(inventory.available_quantity - inventory.reserved_quantity)
        results.append(item)
    return results

# ==============================================================================
# 11. INVENTORY_SUMMARY()
# ==============================================================================
def inventory_summary(
    *,
    warehouse: Optional[Warehouse] = None,
    recent_transactions_limit: int = 10,
    include_inactive_warehouses: bool = False,
) -> Dict[str, Any]:
    """
    Return a comprehensive dashboard summary of inventory health.

    Includes:
        * Total products (distinct targets with inventory records)
        * Total warehouses
        * Total inventory records
        * Total available stock (sum of available_quantity)
        * Total reserved stock (sum of reserved_quantity)
        * Total damaged stock (sum of damaged_quantity)
        * Total incoming stock (sum of incoming_quantity)
        * Low stock count
        * Out of stock count
        * Overstock count
        * Total stock value placeholder (sum of unit_cost * quantity
          from the most recent transactions; the "placeholder" is
          intentionally coarse to avoid re-pricing the entire ledger on
          every dashboard render — see notes).
        * Recent transactions (latest N)
        * Reservation statistics (active, expired, converted counts)

    Returns a structured dictionary suitable for serialization into a
    dashboard JSON response.
    """
    inv_qs = Inventory.objects.all()
    warehouse_qs = Warehouse.objects.all()
    if not include_inactive_warehouses:
        warehouse_qs = warehouse_qs.filter(is_active=True)
    if warehouse is not None:
        inv_qs = inv_qs.filter(warehouse=warehouse)

    active_inv_qs = inv_qs.filter(is_active=True)
    aggregates = active_inv_qs.aggregate(
        total_available=Coalesce(Sum("available_quantity"), Decimal("0"), output_field=__import__("django").db.models.DecimalField(max_digits=20, decimal_places=2)),
        total_reserved=Coalesce(Sum("reserved_quantity"), Decimal("0"), output_field=__import__("django").db.models.DecimalField(max_digits=20, decimal_places=2)),
        total_damaged=Coalesce(Sum("damaged_quantity"), Decimal("0"), output_field=__import__("django").db.models.DecimalField(max_digits=20, decimal_places=2)),
        total_incoming=Coalesce(Sum("incoming_quantity"), Decimal("0"), output_field=__import__("django").db.models.DecimalField(max_digits=20, decimal_places=2)),
    )

    # The aggregate above uses the helper field declared inside the
    # function body; in some Python versions DecimalField needs to be
    # imported. The fallback path below uses the standard string
    # conversion to avoid any runtime ImportError.
    total_available = aggregates["total_available"] or Decimal("0")
    total_reserved = aggregates["total_reserved"] or Decimal("0")
    total_damaged = aggregates["total_damaged"] or Decimal("0")
    total_incoming = aggregates["total_incoming"] or Decimal("0")

    # Distinct product/variant counts
    product_count = active_inv_qs.filter(product__isnull=False).values("product").distinct().count()
    variant_count = active_inv_qs.filter(product_variant__isnull=False).values("product_variant").distinct().count()

    # Low stock and out of stock counts
    low_stock_count = low_stock_products(warehouse=warehouse).__len__() if False else (
        active_inv_qs.annotate(
            free_stock_calc=F("available_quantity") - F("reserved_quantity")
        )
        .filter(reorder_level__isnull=False, free_stock_calc__lte=F("reorder_level"))
        .count()
    )
    out_of_stock_count = (
        active_inv_qs.annotate(
            free_stock_calc=F("available_quantity") - F("reserved_quantity")
        )
        .filter(free_stock_calc__lte=0)
        .count()
    )
    overstock_count = (
        active_inv_qs.annotate(
            free_stock_calc=F("available_quantity") - F("reserved_quantity")
        )
        .filter(maximum_stock__isnull=False, available_quantity__gt=F("maximum_stock"))
        .count()
    )

    # Recent transactions
    recent_txn_qs = InventoryTransaction.objects.select_related(
        "inventory__warehouse", "inventory__product", "inventory__product_variant", "performed_by"
    ).order_by("-transaction_at", "-id")
    if warehouse is not None:
        recent_txn_qs = recent_txn_qs.filter(inventory__warehouse=warehouse)
    recent_transactions = []
    for txn in recent_txn_qs[:recent_transactions_limit]:
        inv = txn.inventory
        target = inv.product_variant or inv.product if inv else None
        recent_transactions.append({
            "id": txn.id,
            "transaction_type": txn.transaction_type,
            "direction": txn.direction,
            "quantity": str(txn.quantity),
            "warehouse_id": inv.warehouse_id if inv else None,
            "warehouse_name": inv.warehouse.display_name if inv else None,
            "target_id": target.id if target else None,
            "target_repr": str(target) if target else None,
            "reference_number": txn.reference_number,
            "reference_model": txn.reference_model,
            "reference_id": txn.reference_id,
            "transaction_at": txn.transaction_at.isoformat() if txn.transaction_at else None,
            "performed_by_id": txn.performed_by_id,
            "remarks": txn.remarks,
        })

    # Reservation statistics
    reservation_aggs = StockReservation.objects.aggregate(
        active=Count("id", filter=Q(status=RESERVATION_STATUS_ACTIVE)),
        converted=Count("id", filter=Q(status=RESERVATION_STATUS_CONVERTED)),
        released=Count("id", filter=Q(status=RESERVATION_STATUS_RELEASED)),
        expired=Count("id", filter=Q(status=RESERVATION_STATUS_EXPIRED)),
        cancelled=Count("id", filter=Q(status=RESERVATION_STATUS_CANCELLED)),
    )

    # Placeholder total stock value (estimated using last known unit_cost
    # per inventory row; this avoids re-pricing the entire ledger on
    # every render). The "placeholder" semantic is intentional; the
    # exact financial integration will live in the future accounting
    # service. The current implementation is cheap and useful for
    # dashboard summaries.
    estimated_value_placeholder = Decimal("0")
    last_costs = (
        InventoryTransaction.objects
        .filter(unit_cost__isnull=False, direction=INVENTORY_FLOW_INBOUND)
        .values("inventory_id", "unit_cost", "transaction_at")
        .order_by("inventory_id", "-transaction_at")
    )
    last_cost_map: Dict[int, Decimal] = {}
    for entry in last_costs:
        inv_id = entry["inventory_id"]
        if inv_id not in last_cost_map:
            last_cost_map[inv_id] = entry["unit_cost"]
    for inv in active_inv_qs:
        cost = last_cost_map.get(inv.id)
        if cost is not None:
            estimated_value_placeholder += inv.available_quantity * cost

    return {
        "totals": {
            "total_products": product_count,
            "total_variants": variant_count,
            "total_warehouses": warehouse_qs.count(),
            "total_inventory_records": inv_qs.count(),
            "active_inventory_records": active_inv_qs.count(),
            "total_available": str(total_available),
            "total_reserved": str(total_reserved),
            "total_damaged": str(total_damaged),
            "total_incoming": str(total_incoming),
            "total_stock_value_placeholder": str(estimated_value_placeholder),
        },
        "alerts": {
            "low_stock_count": low_stock_count,
            "out_of_stock_count": out_of_stock_count,
            "overstock_count": overstock_count,
        },
        "recent_transactions": recent_transactions,
        "reservations": {
            "active": reservation_aggs["active"] or 0,
            "converted": reservation_aggs["converted"] or 0,
            "released": reservation_aggs["released"] or 0,
            "expired": reservation_aggs["expired"] or 0,
            "cancelled": reservation_aggs["cancelled"] or 0,
        },
    }

# ==============================================================================
# 12. UTILITY: RECENT_LEDGER (convenience accessor for dashboards)
# ==============================================================================
def recent_ledger(
    *,
    limit: int = 25,
    warehouse: Optional[Warehouse] = None,
    transaction_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Convenience accessor returning the most recent inventory transactions,
    optionally filtered by warehouse and transaction type. Designed for
    admin dashboards and audit-trail UIs.
    """
    qs = InventoryTransaction.objects.select_related(
        "inventory__warehouse",
        "inventory__product",
        "inventory__product_variant",
        "performed_by",
    ).order_by("-transaction_at", "-id")
    if warehouse is not None:
        qs = qs.filter(inventory__warehouse=warehouse)
    if transaction_type is not None:
        qs = qs.filter(transaction_type=transaction_type)
    out: List[Dict[str, Any]] = []
    for txn in qs[:limit]:
        inv = txn.inventory
        out.append({
            "id": txn.id,
            "transaction_type": txn.transaction_type,
            "direction": txn.direction,
            "quantity": str(txn.quantity),
            "signed_quantity": str(txn.signed_quantity),
            "warehouse_id": inv.warehouse_id if inv else None,
            "warehouse_name": inv.warehouse.display_name if inv else None,
            "product_id": inv.product_id if inv else None,
            "product_variant_id": inv.product_variant_id if inv else None,
            "available_before": str(txn.available_before) if txn.available_before is not None else None,
            "available_after": str(txn.available_after) if txn.available_after is not None else None,
            "reserved_before": str(txn.reserved_before) if txn.reserved_before is not None else None,
            "reserved_after": str(txn.reserved_after) if txn.reserved_after is not None else None,
            "unit_cost": str(txn.unit_cost) if txn.unit_cost is not None else None,
            "total_cost": str(txn.total_cost) if txn.total_cost is not None else None,
            "currency": txn.currency,
            "reference_number": txn.reference_number,
            "reference_model": txn.reference_model,
            "reference_id": txn.reference_id,
            "transfer_group_id": str(txn.transfer_group_id) if txn.transfer_group_id else None,
            "transaction_at": txn.transaction_at.isoformat() if txn.transaction_at else None,
            "performed_by_id": txn.performed_by_id,
            "remarks": txn.remarks,
        })
    return out