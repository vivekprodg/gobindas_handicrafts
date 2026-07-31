"""
Enterprise-grade service layer for the Inventory application.

Single source of truth for all inventory mutations and reservations.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models, transaction
from django.db.models import Case, F, Q, Sum, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    ADJUSTMENT_STATUS_APPLIED,
    ADJUSTMENT_STATUS_APPROVED,
    ADJUSTMENT_STATUS_DRAFT,
    ADJUSTMENT_STATUS_PENDING_APPROVAL,
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

_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_ADJUSTMENT_REASON = "manual_correction"
_DEFAULT_STOCK_FORMULA = "available_minus_reserved"
_CART_RESERVATION_TYPE = "cart"


# ==============================================================================
# EXCEPTIONS
# ==============================================================================
class InsufficientStockError(Exception):
    def __init__(
        self,
        message: str = "",
        *,
        available: Optional[Decimal] = None,
        requested: Optional[Decimal] = None,
        inventory: Optional[Inventory] = None,
    ) -> None:
        super().__init__(message or _("Insufficient stock to complete the requested operation."))
        self.available = available
        self.requested = requested
        self.inventory = inventory


class InvalidWarehouseError(Exception):
    pass


class ReservationExpiredError(Exception):
    pass


class InventoryNotFoundError(Exception):
    pass


class InvalidQuantityError(Exception):
    pass


# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================
def get_default_reservation_duration() -> timedelta:
    minutes = getattr(settings, "INVENTORY_DEFAULT_RESERVATION_MINUTES", _DEFAULT_RESERVATION_MINUTES)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = _DEFAULT_RESERVATION_MINUTES
    return timedelta(minutes=max(1, minutes))


def get_available_stock_formula() -> str:
    formula = getattr(settings, "INVENTORY_AVAILABLE_STOCK_FORMULA", _DEFAULT_STOCK_FORMULA)
    if formula not in {"available", "available_minus_reserved", "available_minus_reserved_plus_incoming"}:
        formula = _DEFAULT_STOCK_FORMULA
    return formula


def _validate_quantity(quantity: Any, *, allow_zero: bool = False) -> Decimal:
    if quantity is None:
        raise InvalidQuantityError(_("Quantity must be provided."))
    try:
        qty = Decimal(str(quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidQuantityError(_("Quantity must be a valid decimal number.")) from exc
    if qty.is_nan() or qty.is_infinite():
        raise InvalidQuantityError(_("Quantity must be a finite decimal number."))
    if not allow_zero and qty <= Decimal("0"):
        raise InvalidQuantityError(_("Quantity must be strictly greater than zero."))
    if allow_zero and qty < Decimal("0"):
        raise InvalidQuantityError(_("Quantity must be greater than or equal to zero."))
    return qty.quantize(Decimal("0.01"))


def _get_active_warehouse(warehouse: Optional[Warehouse]) -> Warehouse:
    if warehouse is None:
        raise InvalidWarehouseError(_("Warehouse is required."))
    if not getattr(warehouse, "is_active", True):
        raise InvalidWarehouseError(_("The selected warehouse is inactive."))
    return warehouse


def _resolve_inventory(*, product: Any, product_variant: Any, warehouse: Warehouse) -> Inventory:
    if bool(product_variant) == bool(product):
        raise ValueError("Exactly one of product or product_variant must be supplied.")
    qs = Inventory.objects.filter(warehouse=warehouse, is_active=True)
    if product_variant is not None:
        qs = qs.filter(product_variant=product_variant, product__isnull=True)
    else:
        qs = qs.filter(product=product, product_variant__isnull=True)
    inventory = qs.first()
    if inventory is None:
        raise InventoryNotFoundError(_("No active inventory record exists for the target and warehouse."))
    return inventory


def _get_or_create_inventory(*, product: Any, product_variant: Any, warehouse: Warehouse) -> Inventory:
    if bool(product_variant) == bool(product):
        raise ValueError("Exactly one of product or product_variant must be supplied.")
    if product_variant is not None:
        defaults = {"product": None}
        target_filter = {"product_variant": product_variant, "product__isnull": True}
    else:
        defaults = {"product_variant": None}
        target_filter = {"product": product, "product_variant__isnull": True}

    inventory, _ = Inventory.objects.get_or_create(
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
    currency: str = "USD",
    remarks: str = "",
    transaction_at: Optional[datetime] = None,
) -> InventoryTransaction:
    return InventoryTransaction.objects.create(
        inventory=inventory,
        transaction_type=transaction_type,
        direction=direction,
        quantity=quantity,
        available_before=inventory.available_quantity,
        available_after=None,
        reserved_before=inventory.reserved_quantity,
        reserved_after=None,
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


def _serialize_inventory(inventory: Inventory) -> Dict[str, Any]:
    from .selectors import _serialize_inventory as sel_serialize
    return sel_serialize(inventory)


def _serialize_reservation(reservation: StockReservation) -> Dict[str, Any]:
    from .selectors import _serialize_reservation as sel_serialize
    return sel_serialize(reservation)


# ==============================================================================
# SERVICE API
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
    qty = _validate_quantity(quantity)

    if warehouse is None:
        warehouse = Warehouse.objects.filter(is_active=True, is_default=True).first()
        if warehouse is None:
            raise InvalidWarehouseError(_("No default warehouse configured."))
    _get_active_warehouse(warehouse)

    target_filter = {"product_variant": product_variant, "product__isnull": True} if product_variant else {"product": product, "product_variant__isnull": True}
    try:
        inventory = Inventory.objects.select_for_update().get(warehouse=warehouse, is_active=True, **target_filter)
    except Inventory.DoesNotExist as exc:
        raise InventoryNotFoundError(_("No active inventory record exists for target.")) from exc

    if inventory.free_stock < qty:
        raise InsufficientStockError(
            _("Insufficient free stock for reservation."),
            available=inventory.free_stock,
            requested=qty,
            inventory=inventory,
        )

    if expires_in is None:
        expires_in = get_default_reservation_duration()
    expires_at = timezone.now() + expires_in

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

    reserved_before = inventory.reserved_quantity
    Inventory.objects.filter(pk=inventory.pk).update(reserved_quantity=F("reserved_quantity") + qty)
    inventory.refresh_from_db(fields=["reserved_quantity", "available_quantity"])

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
    if reservation is None:
        if reservation_id is not None:
            try:
                reservation = StockReservation.objects.select_for_update().get(pk=reservation_id)
            except StockReservation.DoesNotExist as exc:
                raise InventoryNotFoundError(_("Reservation not found for ID.")) from exc
        elif reservation_token is not None:
            try:
                token_uuid = uuid.UUID(str(reservation_token))
                reservation = StockReservation.objects.select_for_update().get(reservation_token=token_uuid)
            except (ValueError, StockReservation.DoesNotExist) as exc:
                raise InventoryNotFoundError(_("Reservation not found for token.")) from exc
        else:
            raise InventoryNotFoundError(_("Reservation identifier required."))
    else:
        reservation = StockReservation.objects.select_for_update().get(pk=reservation.pk)

    if reservation.is_terminal:
        return {
            "success": True,
            "released": False,
            "reservation": _serialize_reservation(reservation),
            "message": _("Reservation already terminal."),
        }

    if reservation.inventory_id is None:
        raise InventoryNotFoundError(_("Reservation not bound to an inventory row."))

    inventory = Inventory.objects.select_for_update().get(pk=reservation.inventory_id)
    reserved_before = inventory.reserved_quantity
    quantity = reservation.quantity

    Inventory.objects.filter(pk=inventory.pk).update(
        reserved_quantity=Case(
            When(reserved_quantity__gte=quantity, then=F("reserved_quantity") - quantity),
            default=Decimal("0.00"),
        )
    )
    inventory.refresh_from_db(fields=["reserved_quantity", "available_quantity"])

    now = timezone.now()
    new_status = RESERVATION_STATUS_EXPIRED if is_automatic else RESERVATION_STATUS_RELEASED
    reservation.status = new_status
    reservation.is_active = False
    reservation.released_at = now
    reservation.save(update_fields=["status", "is_active", "released_at", "updated_at"])

    txn = _build_transaction(
        inventory=inventory,
        transaction_type=InventoryTransaction.TransactionType.RESERVATION_RELEASE,
        direction=INVENTORY_FLOW_NEUTRAL,
        quantity=quantity,
        performed_by=performed_by,
        reference_number=str(reservation.reservation_token),
        reference_model="inventory.StockReservation",
        reference_id=str(reservation.id),
        remarks=reason or (_("Auto-released on expiry.") if is_automatic else _("Released via service layer.")),
    )
    InventoryTransaction.objects.filter(pk=txn.pk).update(
        reserved_before=reserved_before,
        reserved_after=inventory.reserved_quantity,
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
    qs = (
        StockReservation.objects.select_for_update(skip_locked=True)
        .filter(status=RESERVATION_STATUS_ACTIVE, expires_at__lte=timezone.now())
        .order_by("id")
    )
    released_count, failed_count, processed_ids = 0, 0, []
    for res in qs.iterator(chunk_size=batch_size):
        try:
            release_stock(reservation=res, is_automatic=True)
            released_count += 1
        except Exception as exc:
            failed_count += 1
            logger.exception("Failed to release reservation %s: %s", res.pk, exc)
        processed_ids.append(res.id)

    return {"released": released_count, "failed": failed_count, "processed": len(processed_ids)}


@transaction.atomic
def deduct_stock(
    *,
    quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    reservation: Optional[StockReservation] = None,
    reservation_id: Optional[int] = None,
    reference_number: str = "",
    reference_model: str = "",
    reference_id: str = "",
    unit_cost: Optional[Decimal] = None,
    currency: str = "USD",
    remarks: str = "",
    performed_by: Any = None,
    partial_allowed: bool = False,
) -> Dict[str, Any]:
    qty = _validate_quantity(quantity)

    if warehouse is None:
        warehouse = Warehouse.objects.filter(is_active=True, is_default=True).first()
        if warehouse is None:
            raise InvalidWarehouseError(_("No default warehouse configured."))
    _get_active_warehouse(warehouse)

    reservation_obj: Optional[StockReservation] = None
    res_target_id = reservation.pk if reservation else reservation_id
    if res_target_id:
        reservation_obj = StockReservation.objects.select_for_update().get(pk=res_target_id)
        if reservation_obj.is_terminal:
            if reservation_obj.status == RESERVATION_STATUS_CONVERTED:
                inventory = reservation_obj.inventory
                if inventory is None:
                    raise InventoryNotFoundError(_("Reservation has no linked inventory record."))
                return {
                    "success": True,
                    "deducted": False,
                    "deducted_quantity": "0.00",
                    "requested_quantity": str(qty),
                    "inventory": _serialize_inventory(inventory),
                    "message": _("Reservation already converted."),
                }
            raise ReservationExpiredError(_("Reservation is no longer active."))

        if reservation_obj.inventory_id is None:
            raise InventoryNotFoundError(_("Reservation has no bound inventory."))
        inventory = Inventory.objects.select_for_update().get(pk=reservation_obj.inventory_id)
        qty = min(qty, reservation_obj.quantity)
    else:
        target_filter = {"product_variant": product_variant, "product__isnull": True} if product_variant else {"product": product, "product_variant__isnull": True}
        try:
            inventory = Inventory.objects.select_for_update().get(warehouse=warehouse, is_active=True, **target_filter)
        except Inventory.DoesNotExist as exc:
            raise InventoryNotFoundError(_("No active inventory record found.")) from exc

    if inventory.free_stock < qty:
        if not partial_allowed:
            raise InsufficientStockError(_("Insufficient stock for deduction."), available=inventory.free_stock, requested=qty, inventory=inventory)
        qty = inventory.free_stock

    if qty <= Decimal("0"):
        return {
            "success": True,
            "deducted": False,
            "deducted_quantity": "0.00",
            "requested_quantity": str(quantity),
            "inventory": _serialize_inventory(inventory),
            "message": _("Nothing to deduct."),
        }

    available_before = inventory.available_quantity
    reserved_before = inventory.reserved_quantity

    Inventory.objects.filter(pk=inventory.pk).update(available_quantity=F("available_quantity") - qty)
    if reservation_obj is not None:
        Inventory.objects.filter(pk=inventory.pk).update(reserved_quantity=F("reserved_quantity") - qty)
    inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

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

    if reservation_obj is not None:
        reservation_obj.status = RESERVATION_STATUS_CONVERTED
        reservation_obj.is_active = False
        reservation_obj.converted_at = timezone.now()
        if reference_id and str(reference_id).isdigit():
            reservation_obj.converted_to_order_id = int(reference_id)
        reservation_obj.save(update_fields=["status", "is_active", "converted_at", "converted_to_order", "updated_at"])

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


@transaction.atomic
def restock(
    *,
    quantity: Any,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
    unit_cost: Optional[Decimal] = None,
    currency: str = "USD",
    reference_number: str = "",
    reference_model: str = "",
    reference_id: str = "",
    remarks: str = "",
    performed_by: Any = None,
    create_inventory_if_missing: bool = True,
) -> Dict[str, Any]:
    qty = _validate_quantity(quantity)

    if warehouse is None:
        warehouse = Warehouse.objects.filter(is_active=True, is_default=True).first()
        if warehouse is None:
            raise InvalidWarehouseError(_("No default warehouse configured."))
    _get_active_warehouse(warehouse)

    if create_inventory_if_missing:
        inventory = _get_or_create_inventory(product=product, product_variant=product_variant, warehouse=warehouse)
    else:
        inventory = _resolve_inventory(product=product, product_variant=product_variant, warehouse=warehouse)

    available_before = inventory.available_quantity
    reserved_before = inventory.reserved_quantity

    Inventory.objects.filter(pk=inventory.pk).update(available_quantity=F("available_quantity") + qty)
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

    return {
        "success": True,
        "restocked_quantity": str(qty),
        "inventory": _serialize_inventory(inventory),
        "available_after": str(inventory.available_quantity),
        "message": _("Stock restocked successfully."),
    }


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
    new_qty = _validate_quantity(new_quantity, allow_zero=True)

    if warehouse is None:
        warehouse = Warehouse.objects.filter(is_active=True, is_default=True).first()
        if warehouse is None:
            raise InvalidWarehouseError(_("No default warehouse configured."))
    _get_active_warehouse(warehouse)

    inventory = _resolve_inventory(product=product, product_variant=product_variant, warehouse=warehouse)
    inventory = Inventory.objects.select_for_update().get(pk=inventory.pk)

    available_before = inventory.available_quantity
    difference = (new_qty - available_before).quantize(Decimal("0.01"))

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
            raise DjangoValidationError(_("An approving user is required when auto_apply is True."))

        reserved_before = inventory.reserved_quantity
        Inventory.objects.filter(pk=inventory.pk).update(available_quantity=new_qty)
        inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

        if perform_transaction:
            direction = INVENTORY_FLOW_INBOUND if difference > 0 else (INVENTORY_FLOW_OUTBOUND if difference < 0 else INVENTORY_FLOW_NEUTRAL)
            applied_transaction = _build_transaction(
                inventory=inventory,
                transaction_type=InventoryTransaction.TransactionType.ADJUSTMENT,
                direction=direction,
                quantity=abs(difference),
                performed_by=approved_by,
                reference_number=reference_number,
                reference_model="inventory.StockAdjustment",
                reference_id=str(adjustment.id),
                remarks=remarks or description or _("Manual adjustment applied."),
            )
            InventoryTransaction.objects.filter(pk=applied_transaction.pk).update(
                available_before=available_before,
                available_after=inventory.available_quantity,
                reserved_before=reserved_before,
                reserved_after=inventory.reserved_quantity,
            )

        adjustment.status = ADJUSTMENT_STATUS_APPLIED
        adjustment.approved_by = approved_by
        adjustment.approved_at = adjustment.approved_at or timezone.now()
        adjustment.applied_at = timezone.now()
        adjustment.applied_transaction = applied_transaction
        adjustment.save(update_fields=["status", "approved_by", "approved_at", "applied_at", "applied_transaction", "updated_at"])

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
        "message": _("Adjustment applied.") if auto_apply else _("Adjustment pending approval."),
    }


@transaction.atomic
def approve_adjustment(*, adjustment_id: int, approved_by: Any, apply_immediately: bool = True) -> Dict[str, Any]:
    adjustment = StockAdjustment.objects.select_for_update().get(pk=adjustment_id)
    if adjustment.status not in {ADJUSTMENT_STATUS_DRAFT, ADJUSTMENT_STATUS_PENDING_APPROVAL}:
        raise DjangoValidationError(_("Only draft or pending adjustments can be approved."))
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

        Inventory.objects.filter(pk=inventory.pk).update(available_quantity=adjustment.new_quantity)
        inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

        direction = INVENTORY_FLOW_INBOUND if adjustment.difference > 0 else (INVENTORY_FLOW_OUTBOUND if adjustment.difference < 0 else INVENTORY_FLOW_NEUTRAL)
        applied_txn = _build_transaction(
            inventory=inventory,
            transaction_type=InventoryTransaction.TransactionType.ADJUSTMENT,
            direction=direction,
            quantity=abs(adjustment.difference or Decimal("0")),
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
        adjustment.save(update_fields=["status", "applied_at", "applied_transaction", "updated_at"])

    return {
        "success": True,
        "adjustment_id": adjustment.id,
        "adjustment_number": adjustment.adjustment_number,
        "status": adjustment.status,
        "applied": apply_immediately,
        "message": _("Adjustment approved and applied.") if apply_immediately else _("Adjustment approved."),
    }


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
    currency: str = "USD",
    remarks: str = "",
    performed_by: Any = None,
    create_destination_if_missing: bool = True,
) -> Dict[str, Any]:
    qty = _validate_quantity(quantity)
    _get_active_warehouse(source_warehouse)
    _get_active_warehouse(destination_warehouse)

    if source_warehouse.id == destination_warehouse.id:
        raise InvalidWarehouseError(_("Source and destination warehouses must be different."))

    source_inventory = Inventory.objects.select_for_update().get(
        pk=_resolve_inventory(product=product, product_variant=product_variant, warehouse=source_warehouse).pk
    )

    if create_destination_if_missing:
        dest_inv_obj = _get_or_create_inventory(product=product, product_variant=product_variant, warehouse=destination_warehouse)
    else:
        dest_inv_obj = _resolve_inventory(product=product, product_variant=product_variant, warehouse=destination_warehouse)

    destination_inventory = Inventory.objects.select_for_update().get(pk=dest_inv_obj.pk)

    if source_inventory.free_stock < qty:
        raise InsufficientStockError(_("Insufficient stock in source warehouse."), available=source_inventory.free_stock, requested=qty, inventory=source_inventory)

    transfer_group_id = uuid.uuid4()
    s_avail_before, s_resv_before = source_inventory.available_quantity, source_inventory.reserved_quantity
    d_avail_before, d_resv_before = destination_inventory.available_quantity, destination_inventory.reserved_quantity

    Inventory.objects.filter(pk=source_inventory.pk).update(available_quantity=F("available_quantity") - qty)
    Inventory.objects.filter(pk=destination_inventory.pk).update(available_quantity=F("available_quantity") + qty)

    source_inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])
    destination_inventory.refresh_from_db(fields=["available_quantity", "reserved_quantity"])

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
        available_before=s_avail_before, available_after=source_inventory.available_quantity,
        reserved_before=s_resv_before, reserved_after=source_inventory.reserved_quantity
    )

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
        available_before=d_avail_before, available_after=destination_inventory.available_quantity,
        reserved_before=d_resv_before, reserved_after=destination_inventory.reserved_quantity
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


def check_stock(*, product: Any = None, product_variant: Any = None, warehouse: Optional[Warehouse] = None, quantity: Any = 1, include_all_warehouses: bool = False) -> Dict[str, Any]:
    qty = _validate_quantity(quantity, allow_zero=True)
    if bool(product) == bool(product_variant):
        raise ValueError("Exactly one of product or product_variant must be supplied.")

    target_filter = {"product_variant": product_variant, "product__isnull": True} if product_variant else {"product": product, "product_variant__isnull": True}

    if include_all_warehouses:
        inventories = list(Inventory.objects.filter(is_active=True, **target_filter).select_related("warehouse"))
    else:
        if warehouse is None:
            warehouse = Warehouse.objects.filter(is_active=True, is_default=True).first()
            if warehouse is None:
                raise InvalidWarehouseError(_("No default warehouse configured."))
        _get_active_warehouse(warehouse)
        try:
            inv = Inventory.objects.select_related("warehouse").get(warehouse=warehouse, is_active=True, **target_filter)
            inventories = [inv]
        except Inventory.DoesNotExist:
            inventories = []

    agg_avail = sum((i.available_quantity for i in inventories), Decimal("0"))
    agg_resv = sum((i.reserved_quantity for i in inventories), Decimal("0"))
    agg_incom = sum((i.incoming_quantity for i in inventories), Decimal("0"))
    agg_free = agg_avail - agg_resv

    return {
        "product_id": product.id if product else None,
        "product_variant_id": product_variant.id if product_variant else None,
        "warehouse_id": warehouse.id if warehouse else None,
        "requested_quantity": str(qty),
        "available_quantity": str(agg_avail),
        "reserved_quantity": str(agg_resv),
        "incoming_quantity": str(agg_incom),
        "free_stock": str(agg_free),
        "is_available": agg_free >= qty,
        "is_available_at_requested_warehouse": len(inventories) > 0 and inventories[0].free_stock >= qty,
        "warehouses_checked": len(inventories),
        "per_warehouse": [_serialize_inventory(i) for i in inventories],
    }


def calculate_available_stock(*, product: Any = None, product_variant: Any = None, warehouse: Optional[Warehouse] = None, formula: Optional[str] = None) -> Dict[str, Any]:
    if bool(product) == bool(product_variant):
        raise ValueError("Exactly one of product or product_variant must be supplied.")

    if warehouse is None:
        warehouse = Warehouse.objects.filter(is_active=True, is_default=True).first()
        if warehouse is None:
            raise InvalidWarehouseError(_("No default warehouse configured."))
    _get_active_warehouse(warehouse)

    inventory = _resolve_inventory(product=product, product_variant=product_variant, warehouse=warehouse)
    chosen_formula = formula or get_available_stock_formula()

    avail = inventory.available_quantity
    resv = inventory.reserved_quantity
    incom = inventory.incoming_quantity

    if chosen_formula == "available":
        sellable = avail
    elif chosen_formula == "available_minus_reserved_plus_incoming":
        sellable = max(Decimal("0"), avail - resv + incom)
    else:
        chosen_formula = "available_minus_reserved"
        sellable = max(Decimal("0"), avail - resv)

    return {
        "inventory_id": inventory.id,
        "warehouse_id": warehouse.id,
        "product_id": product.id if product else None,
        "product_variant_id": product_variant.id if product_variant else None,
        "formula": chosen_formula,
        "sellable_quantity": str(sellable),
        "available_quantity": str(avail),
        "reserved_quantity": str(resv),
        "incoming_quantity": str(incom),
    }


def low_stock_products(*, warehouse: Optional[Warehouse] = None, limit: Optional[int] = None, include_inactive: bool = False) -> List[Dict[str, Any]]:
    from .selectors import get_low_stock, serialize_inventory_list
    qs = get_low_stock(warehouse=warehouse, limit=limit, active_only=not include_inactive)
    return serialize_inventory_list(qs, limit=limit)


def out_of_stock_products(*, warehouse: Optional[Warehouse] = None, limit: Optional[int] = None, include_inactive: bool = False, include_damaged: bool = True) -> List[Dict[str, Any]]:
    from .selectors import get_out_of_stock, serialize_inventory_list
    qs = get_out_of_stock(warehouse=warehouse, limit=limit, active_only=not include_inactive, include_damaged=include_damaged)
    return serialize_inventory_list(qs, limit=limit)


def inventory_summary(*, warehouse: Optional[Warehouse] = None, recent_transactions_limit: int = 10, include_inactive_warehouses: bool = False) -> Dict[str, Any]:
    from .selectors import get_inventory_dashboard
    return get_inventory_dashboard(
        warehouse=warehouse,
        recent_transactions_limit=recent_transactions_limit,
        include_inactive_warehouses=include_inactive_warehouses,
    )


def recent_ledger(*, limit: int = 25, warehouse: Optional[Warehouse] = None, transaction_type: Optional[str] = None) -> List[Dict[str, Any]]:
    from .selectors import get_recent_transactions, serialize_transaction_list
    qs = get_recent_transactions(limit=limit, warehouse=warehouse, transaction_type=transaction_type)
    return serialize_transaction_list(qs, limit=limit)


__all__ = [
    "InsufficientStockError",
    "InvalidWarehouseError",
    "ReservationExpiredError",
    "InventoryNotFoundError",
    "InvalidQuantityError",
    "get_default_reservation_duration",
    "get_available_stock_formula",
    "reserve_stock",
    "release_stock",
    "release_expired_reservations",
    "deduct_stock",
    "restock",
    "adjust_stock",
    "approve_adjustment",
    "transfer_stock",
    "check_stock",
    "calculate_available_stock",
    "low_stock_products",
    "out_of_stock_products",
    "inventory_summary",
    "recent_ledger",
]