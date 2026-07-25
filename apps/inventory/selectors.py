"""
Enterprise-grade selector layer for the Inventory application.

Single source of truth for all read-only database operations.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db.models import Count, F, Q, QuerySet, Sum
from django.utils import timezone

from .models import (
    ADJUSTMENT_STATUS_APPLIED,
    ADJUSTMENT_STATUS_DRAFT,
    ADJUSTMENT_STATUS_PENDING_APPROVAL,
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

_DEFAULT_RECENT_TRANSACTIONS_LIMIT = 25
_DEFAULT_DASHBOARD_RECENT_TRANSACTIONS = 10
_DEFAULT_LOW_STOCK_LIMIT = 100
_DEFAULT_OUT_OF_STOCK_LIMIT = 100
_DEFAULT_LEDGER_LIMIT = 25

def _normalize_limit(limit: Optional[int], default: int) -> int:
    if limit is None:
        return default
    try:
        val = int(limit)
        return max(1, min(val, 10000))
    except (TypeError, ValueError):
        return default

# ==============================================================================
# SERIALIZERS
# ==============================================================================
def _serialize_inventory(inventory: Inventory) -> Dict[str, Any]:
    return {
        "id": inventory.id,
        "warehouse_id": inventory.warehouse_id,
        "warehouse_name": inventory.warehouse.display_name if inventory.warehouse else None,
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

def _serialize_transaction(txn: InventoryTransaction) -> Dict[str, Any]:
    inv = txn.inventory
    return {
        "id": txn.id,
        "transaction_type": txn.transaction_type,
        "direction": txn.direction,
        "quantity": str(txn.quantity),
        "signed_quantity": str(txn.signed_quantity),
        "warehouse_id": inv.warehouse_id if inv else None,
        "warehouse_name": inv.warehouse.display_name if inv and inv.warehouse else None,
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
    }

# ==============================================================================
# SELECTORS
# ==============================================================================
def get_inventory(
    *,
    warehouse: Optional[Warehouse] = None,
    product: Any = None,
    product_variant: Any = None,
    category: Any = None,
    vendor: Any = None,
    active_only: bool = True,
    in_stock_only: bool = False,
    order_by: str = "warehouse__name",
) -> QuerySet:
    try:
        qs = Inventory.objects.select_related("warehouse", "product", "product_variant")
        if active_only:
            qs = qs.filter(is_active=True)
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)
        if product_variant is not None:
            qs = qs.filter(product_variant=product_variant)
        if product is not None:
            qs = qs.filter(product=product)
        if category is not None:
            qs = qs.filter(Q(product__category=category) | Q(product_variant__product__category=category))
        if vendor is not None:
            qs = qs.filter(Q(product__artisan=vendor) | Q(product_variant__product__artisan=vendor))
        if in_stock_only:
            qs = qs.filter(available_quantity__gt=0)

        allowed_orderings = {
            "warehouse__name": "warehouse__name",
            "-warehouse__name": "-warehouse__name",
            "available_quantity": "available_quantity",
            "-available_quantity": "-available_quantity",
            "updated_at": "updated_at",
            "-updated_at": "-updated_at",
            "id": "id",
            "-id": "-id",
        }
        return qs.order_by(allowed_orderings.get(order_by, "warehouse__name"))
    except Exception as exc:
        logger.exception("Failed to build inventory queryset: %s", exc)
        return Inventory.objects.none()

def get_inventory_by_variant(*, product_variant: Any, warehouse: Optional[Warehouse] = None, active_only: bool = True) -> QuerySet:
    if product_variant is None:
        return Inventory.objects.none()
    try:
        qs = Inventory.objects.filter(product_variant=product_variant).select_related("warehouse", "product_variant")
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)
        if active_only:
            qs = qs.filter(is_active=True)
        return qs.order_by("warehouse__name")
    except Exception as exc:
        logger.exception("Failed to fetch inventory by variant: %s", exc)
        return Inventory.objects.none()

def get_inventory_by_sku(*, sku: str, warehouse: Optional[Warehouse] = None, active_only: bool = True) -> QuerySet:
    if not sku or not isinstance(sku, str) or not sku.strip():
        return Inventory.objects.none()
    sku_clean = sku.strip()
    try:
        qs = Inventory.objects.filter(
            Q(product__sku__iexact=sku_clean) | Q(product_variant__sku__iexact=sku_clean)
        ).select_related("warehouse", "product", "product_variant")
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)
        if active_only:
            qs = qs.filter(is_active=True)
        return qs.order_by("warehouse__name")
    except Exception as exc:
        logger.exception("Failed to fetch inventory by SKU: %s", exc)
        return Inventory.objects.none()

def get_low_stock(*, warehouse: Optional[Warehouse] = None, threshold: Optional[Decimal] = None, limit: Optional[int] = None, active_only: bool = True) -> QuerySet:
    safe_limit = _normalize_limit(limit, _DEFAULT_LOW_STOCK_LIMIT)
    try:
        qs = Inventory.objects.annotate(free_stock_calc=F("available_quantity") - F("reserved_quantity"))
        if active_only:
            qs = qs.filter(is_active=True)
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        qs = qs.filter(reorder_level__isnull=False, reorder_level__gt=0)
        if threshold is None:
            qs = qs.filter(free_stock_calc__lte=F("reorder_level"))
        else:
            try:
                thresh_val = Decimal(str(threshold))
                qs = qs.filter(free_stock_calc__lte=thresh_val)
            except (InvalidOperation, TypeError, ValueError):
                qs = qs.filter(free_stock_calc__lte=F("reorder_level"))

        return qs.select_related("warehouse", "product", "product_variant").order_by("free_stock_calc", "id")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch low stock queryset: %s", exc)
        return Inventory.objects.none()

def get_out_of_stock(*, warehouse: Optional[Warehouse] = None, limit: Optional[int] = None, active_only: bool = True, include_damaged: bool = True) -> QuerySet:
    safe_limit = _normalize_limit(limit, _DEFAULT_OUT_OF_STOCK_LIMIT)
    try:
        qs = Inventory.objects.annotate(
            free_stock_calc=F("available_quantity") - F("reserved_quantity"),
            total_stock_calc=F("available_quantity") + F("damaged_quantity"),
        )
        if active_only:
            qs = qs.filter(is_active=True)
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        qs = qs.filter(free_stock_calc__lte=0)
        if not include_damaged:
            qs = qs.filter(total_stock_calc__lte=0)

        return qs.select_related("warehouse", "product", "product_variant").order_by("id")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch out-of-stock queryset: %s", exc)
        return Inventory.objects.none()

def get_transactions(
    *,
    warehouse: Optional[Warehouse] = None,
    product: Any = None,
    sku: Optional[str] = None,
    transaction_type: Optional[str] = None,
    direction: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    reference_number: Optional[str] = None,
    user: Any = None,
    order_by: str = "-transaction_at",
    limit: Optional[int] = None,
) -> QuerySet:
    safe_limit = _normalize_limit(limit, 1000)
    try:
        qs = InventoryTransaction.objects.select_related(
            "inventory__warehouse", "inventory__product", "inventory__product_variant", "performed_by", "destination_warehouse"
        )
        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)
        if product is not None:
            qs = qs.filter(Q(inventory__product=product) | Q(inventory__product_variant__product=product))
        if sku and isinstance(sku, str) and sku.strip():
            s_clean = sku.strip()
            qs = qs.filter(Q(inventory__product__sku__iexact=s_clean) | Q(inventory__product_variant__sku__iexact=s_clean))
        if transaction_type in {c[0] for c in InventoryTransaction.TransactionType.choices}:
            qs = qs.filter(transaction_type=transaction_type)
        if direction in {c[0] for c in InventoryTransaction.FlowDirection.choices}:
            qs = qs.filter(direction=direction)
        if reference_number and isinstance(reference_number, str) and reference_number.strip():
            qs = qs.filter(reference_number__icontains=reference_number.strip())
        if user is not None:
            qs = qs.filter(performed_by=user)
        if date_from:
            qs = qs.filter(transaction_at__gte=date_from)
        if date_to:
            qs = qs.filter(transaction_at__lte=date_to)

        allowed_orderings = {
            "transaction_at": "transaction_at",
            "-transaction_at": "-transaction_at",
            "created_at": "created_at",
            "-created_at": "-created_at",
            "quantity": "quantity",
            "-quantity": "-quantity",
            "id": "id",
            "-id": "-id",
        }
        return qs.order_by(allowed_orderings.get(order_by, "-transaction_at"))[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch transactions: %s", exc)
        return InventoryTransaction.objects.none()

def get_recent_transactions(*, limit: Optional[int] = None, warehouse: Optional[Warehouse] = None, transaction_type: Optional[str] = None) -> QuerySet:
    safe_limit = _normalize_limit(limit, _DEFAULT_RECENT_TRANSACTIONS_LIMIT)
    try:
        qs = InventoryTransaction.objects.select_related(
            "inventory__warehouse", "inventory__product", "inventory__product_variant", "performed_by"
        )
        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)
        if transaction_type in {c[0] for c in InventoryTransaction.TransactionType.choices}:
            qs = qs.filter(transaction_type=transaction_type)
        return qs.order_by("-transaction_at", "-id")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch recent transactions: %s", exc)
        return InventoryTransaction.objects.none()

def get_inventory_dashboard(*, warehouse: Optional[Warehouse] = None, recent_transactions_limit: Optional[int] = None, include_inactive_warehouses: bool = False) -> Dict[str, Any]:
    limit = _normalize_limit(recent_transactions_limit, _DEFAULT_DASHBOARD_RECENT_TRANSACTIONS)
    try:
        warehouse_qs = Warehouse.objects.all() if include_inactive_warehouses else Warehouse.objects.filter(is_active=True)
        inv_qs = Inventory.objects.all()
        if warehouse is not None:
            inv_qs = inv_qs.filter(warehouse=warehouse)

        active_inv_qs = inv_qs.filter(is_active=True)
        aggs = active_inv_qs.aggregate(
            total_available=Sum("available_quantity"),
            total_reserved=Sum("reserved_quantity"),
            total_damaged=Sum("damaged_quantity"),
            total_incoming=Sum("incoming_quantity"),
        )

        product_count = active_inv_qs.filter(product__isnull=False).values("product").distinct().count()
        variant_count = active_inv_qs.filter(product_variant__isnull=False).values("product_variant").distinct().count()

        annotated_inv = active_inv_qs.annotate(free_stock_calc=F("available_quantity") - F("reserved_quantity"))
        low_stock_count = annotated_inv.filter(reorder_level__isnull=False, free_stock_calc__lte=F("reorder_level")).count()
        out_of_stock_count = annotated_inv.filter(free_stock_calc__lte=0).count()
        overstock_count = annotated_inv.filter(maximum_stock__isnull=False, available_quantity__gt=F("maximum_stock")).count()

        recent_txns = [
            _serialize_transaction(t)
            for t in get_recent_transactions(limit=limit, warehouse=warehouse)
        ]

        reservation_aggs = StockReservation.objects.aggregate(
            active=Count("id", filter=Q(status=RESERVATION_STATUS_ACTIVE)),
            converted=Count("id", filter=Q(status=RESERVATION_STATUS_CONVERTED)),
            released=Count("id", filter=Q(status=RESERVATION_STATUS_RELEASED)),
            expired=Count("id", filter=Q(status=RESERVATION_STATUS_EXPIRED)),
            cancelled=Count("id", filter=Q(status=RESERVATION_STATUS_CANCELLED)),
        )

        return {
            "totals": {
                "total_products": product_count,
                "total_variants": variant_count,
                "total_warehouses": warehouse_qs.count(),
                "total_inventory_records": inv_qs.count(),
                "active_inventory_records": active_inv_qs.count(),
                "total_available": str(aggs["total_available"] or Decimal("0")),
                "total_reserved": str(aggs["total_reserved"] or Decimal("0")),
                "total_damaged": str(aggs["total_damaged"] or Decimal("0")),
                "total_incoming": str(aggs["total_incoming"] or Decimal("0")),
                "total_stock_value_placeholder": "0.00",
            },
            "alerts": {
                "low_stock_count": low_stock_count,
                "out_of_stock_count": out_of_stock_count,
                "overstock_count": overstock_count,
            },
            "recent_transactions": recent_txns,
            "reservations": {
                "active": reservation_aggs["active"] or 0,
                "converted": reservation_aggs["converted"] or 0,
                "released": reservation_aggs["released"] or 0,
                "expired": reservation_aggs["expired"] or 0,
                "cancelled": reservation_aggs["cancelled"] or 0,
            },
        }
    except Exception as exc:
        logger.exception("Failed to build inventory dashboard data: %s", exc)
        return {
            "totals": {"total_products": 0, "total_variants": 0, "total_warehouses": 0, "total_inventory_records": 0, "active_inventory_records": 0, "total_available": "0", "total_reserved": "0", "total_damaged": "0", "total_incoming": "0", "total_stock_value_placeholder": "0.00"},
            "alerts": {"low_stock_count": 0, "out_of_stock_count": 0, "overstock_count": 0},
            "recent_transactions": [],
            "reservations": {"active": 0, "converted": 0, "released": 0, "expired": 0, "cancelled": 0},
        }

def get_reservations(
    *,
    cart: Any = None,
    product_variant: Any = None,
    product: Any = None,
    warehouse: Optional[Warehouse] = None,
    active_only: bool = True,
    user: Any = None,
    expiry_status: Optional[str] = None,
    order_by: str = "-created_at",
    limit: Optional[int] = None,
) -> QuerySet:
    safe_limit = _normalize_limit(limit, 1000)
    try:
        qs = StockReservation.objects.select_related("warehouse", "inventory__product", "inventory__product_variant", "user", "cart")
        if active_only:
            qs = qs.filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)
        if cart is not None:
            qs = qs.filter(cart=cart)
        if product_variant is not None:
            qs = qs.filter(product_variant=product_variant)
        if product is not None:
            qs = qs.filter(product=product)
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)
        if user is not None:
            qs = qs.filter(user=user)

        now = timezone.now()
        if expiry_status == "expired":
            qs = qs.filter(expires_at__isnull=False, expires_at__lte=now)
        elif expiry_status == "active":
            qs = qs.filter(expires_at__gt=now)
        elif expiry_status == "no_expiry":
            qs = qs.filter(expires_at__isnull=True)

        allowed_orderings = {
            "created_at": "created_at",
            "-created_at": "-created_at",
            "expires_at": "expires_at",
            "-expires_at": "-expires_at",
            "quantity": "quantity",
            "-quantity": "-quantity",
            "id": "id",
            "-id": "-id",
        }
        return qs.order_by(allowed_orderings.get(order_by, "-created_at"))[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch reservations: %s", exc)
        return StockReservation.objects.none()

def get_expired_reservations(*, batch_size: int = 500, active_only: bool = True, warehouse: Optional[Warehouse] = None) -> QuerySet:
    try:
        qs = StockReservation.objects.select_related("warehouse", "inventory__product", "inventory__product_variant")
        if active_only:
            qs = qs.filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)
        return qs.filter(expires_at__isnull=False, expires_at__lte=timezone.now()).order_by("id")
    except Exception as exc:
        logger.exception("Failed to fetch expired reservations: %s", exc)
        return StockReservation.objects.none()

def get_stock_adjustments(*, inventory: Optional[Inventory] = None, status: Optional[str] = None, initiated_by: Any = None, approved_by: Any = None, order_by: str = "-created_at", limit: Optional[int] = None) -> QuerySet:
    safe_limit = _normalize_limit(limit, 1000)
    try:
        qs = StockAdjustment.objects.select_related(
            "inventory__warehouse", "inventory__product", "inventory__product_variant", "initiated_by", "approved_by", "applied_transaction"
        )
        if inventory is not None:
            qs = qs.filter(inventory=inventory)
        if status in {c[0] for c in StockAdjustment.AdjustmentStatus.choices}:
            qs = qs.filter(status=status)
        if initiated_by is not None:
            qs = qs.filter(initiated_by=initiated_by)
        if approved_by is not None:
            qs = qs.filter(approved_by=approved_by)

        allowed_orderings = {
            "created_at": "created_at",
            "-created_at": "-created_at",
            "approved_at": "approved_at",
            "-approved_at": "-approved_at",
            "applied_at": "applied_at",
            "-applied_at": "-applied_at",
            "id": "id",
            "-id": "-id",
        }
        return qs.order_by(allowed_orderings.get(order_by, "-created_at"))[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch stock adjustments: %s", exc)
        return StockAdjustment.objects.none()

def get_warehouses(*, active_only: bool = True, default_only: bool = False, order_by: str = "name") -> QuerySet:
    try:
        qs = Warehouse.objects.all()
        if active_only:
            qs = qs.filter(is_active=True)
        if default_only:
            qs = qs.filter(is_default=True, is_active=True)
        allowed_orderings = {
            "name": "name",
            "-name": "-name",
            "is_default": "is_default",
            "-is_default": "-is_default",
            "created_at": "created_at",
            "-created_at": "-created_at",
            "id": "id",
            "-id": "-id",
        }
        return qs.order_by(allowed_orderings.get(order_by, "name"))
    except Exception as exc:
        logger.exception("Failed to fetch warehouses: %s", exc)
        return Warehouse.objects.none()

def get_default_warehouse() -> Optional[Warehouse]:
    try:
        return Warehouse.objects.filter(is_default=True, is_active=True).order_by("id").first()
    except Exception as exc:
        logger.exception("Failed to fetch default warehouse: %s", exc)
        return None

def get_warehouse_by_id(warehouse_id: int) -> Optional[Warehouse]:
    if not isinstance(warehouse_id, (int, str)):
        return None
    try:
        return Warehouse.objects.filter(pk=int(warehouse_id)).first()
    except Exception as exc:
        logger.exception("Failed to fetch warehouse by ID %s: %s", warehouse_id, exc)
        return None

def get_adjustment_by_number(adjustment_number: str) -> Optional[StockAdjustment]:
    if not adjustment_number or not isinstance(adjustment_number, str) or not adjustment_number.strip():
        return None
    try:
        return (
            StockAdjustment.objects.select_related(
                "inventory__warehouse", "inventory__product", "initiated_by", "approved_by", "applied_transaction"
            )
            .filter(adjustment_number=adjustment_number.strip())
            .first()
        )
    except Exception as exc:
        logger.exception("Failed to fetch adjustment by number %s: %s", adjustment_number, exc)
        return None

def get_reservation_by_token(token: str) -> Optional[StockReservation]:
    if not token or not isinstance(token, (str, uuid.UUID)):
        return None
    try:
        token_uuid = uuid.UUID(str(token).strip())
        return (
            StockReservation.objects.select_related(
                "warehouse", "inventory__product", "inventory__product_variant", "user", "cart"
            )
            .filter(reservation_token=token_uuid)
            .first()
        )
    except (ValueError, TypeError):
        return None
    except Exception as exc:
        logger.exception("Failed to fetch reservation by token %s: %s", token, exc)
        return None

def get_reservations_by_cart(*, cart: Any, active_only: bool = True) -> QuerySet:
    if cart is None:
        return StockReservation.objects.none()
    try:
        qs = StockReservation.objects.select_related("warehouse", "inventory__product", "inventory__product_variant", "user", "cart").filter(cart=cart)
        if active_only:
            qs = qs.filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)
        return qs.order_by("-created_at")
    except Exception as exc:
        logger.exception("Failed to fetch reservations by cart %s: %s", cart, exc)
        return StockReservation.objects.none()

def get_reservations_by_session(*, session_key: str, active_only: bool = True) -> QuerySet:
    if not session_key or not isinstance(session_key, str):
        return StockReservation.objects.none()
    try:
        qs = StockReservation.objects.select_related("warehouse", "inventory__product", "inventory__product_variant", "user", "cart").filter(session_key=session_key)
        if active_only:
            qs = qs.filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)
        return qs.order_by("-created_at")
    except Exception as exc:
        logger.exception("Failed to fetch reservations by session %s: %s", session_key, exc)
        return StockReservation.objects.none()

def get_active_stock_reservation_for_target(*, cart: Any = None, user: Any = None, session_key: Optional[str] = None, product: Any = None, product_variant: Any = None) -> Optional[StockReservation]:
    if not (cart or user or session_key) or not (product or product_variant):
        return None
    try:
        qs = StockReservation.objects.select_related("warehouse", "inventory", "user", "cart").filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)
        if cart is not None:
            qs = qs.filter(cart=cart)
        if user is not None:
            qs = qs.filter(user=user)
        if session_key:
            qs = qs.filter(session_key=session_key)
        if product_variant is not None:
            qs = qs.filter(product_variant=product_variant)
        if product is not None:
            qs = qs.filter(product=product)
        return qs.order_by("-created_at").first()
    except Exception as exc:
        logger.exception("Failed to fetch active reservation: %s", exc)
        return None

def serialize_inventory_list(queryset: Optional[QuerySet] = None, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if queryset is None:
        return []
    safe_limit = _normalize_limit(limit, 1000)
    try:
        return [_serialize_inventory(inv) for inv in queryset[:safe_limit]]
    except Exception as exc:
        logger.exception("Failed to serialize inventory list: %s", exc)
        return []

def serialize_reservation_list(queryset: Optional[QuerySet] = None, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if queryset is None:
        return []
    safe_limit = _normalize_limit(limit, 1000)
    try:
        return [_serialize_reservation(res) for res in queryset[:safe_limit]]
    except Exception as exc:
        logger.exception("Failed to serialize reservation list: %s", exc)
        return []

def serialize_transaction_list(queryset: Optional[QuerySet] = None, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if queryset is None:
        return []
    safe_limit = _normalize_limit(limit, _DEFAULT_LEDGER_LIMIT)
    try:
        return [_serialize_transaction(txn) for txn in queryset[:safe_limit]]
    except Exception as exc:
        logger.exception("Failed to serialize transaction list: %s", exc)
        return []

def get_inventory_summary_for_target(*, product: Any = None, product_variant: Any = None, warehouse: Optional[Warehouse] = None) -> Dict[str, Any]:
    if not (product or product_variant):
        return {"exists": False, "free_stock": "0", "is_out_of_stock": True, "is_low_stock": False}
    try:
        qs = Inventory.objects.select_related("warehouse")
        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)
        if product_variant is not None:
            qs = qs.filter(product_variant=product_variant)
        if product is not None:
            qs = qs.filter(product=product)

        inv = qs.filter(is_active=True).first()
        if inv is None:
            return {
                "exists": False,
                "free_stock": "0",
                "is_out_of_stock": True,
                "is_low_stock": False,
                "warehouse_name": warehouse.display_name if warehouse else None,
            }

        return {
            "exists": True,
            "id": inv.id,
            "warehouse_id": inv.warehouse_id,
            "warehouse_name": inv.warehouse.display_name if inv.warehouse else None,
            "available_quantity": str(inv.available_quantity),
            "reserved_quantity": str(inv.reserved_quantity),
            "free_stock": str(inv.free_stock),
            "total_stock": str(inv.total_stock),
            "is_out_of_stock": inv.is_out_of_stock,
            "is_low_stock": inv.is_low_stock,
            "is_overstock": inv.is_overstock,
            "needs_reorder": inv.needs_reorder,
            "reorder_level": str(inv.reorder_level) if inv.reorder_level is not None else None,
            "is_active": inv.is_active,
        }
    except Exception as exc:
        logger.exception("Failed to build inventory summary for target: %s", exc)
        return {"exists": False, "free_stock": "0", "is_out_of_stock": True, "is_low_stock": False}

def get_pending_adjustments(*, warehouse: Optional[Warehouse] = None, limit: Optional[int] = None) -> QuerySet:
    safe_limit = _normalize_limit(limit, 1000)
    try:
        qs = StockAdjustment.objects.select_related("inventory__warehouse", "inventory__product", "initiated_by").filter(
            status__in=[ADJUSTMENT_STATUS_DRAFT, ADJUSTMENT_STATUS_PENDING_APPROVAL]
        )
        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)
        return qs.order_by("-created_at")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch pending adjustments: %s", exc)
        return StockAdjustment.objects.none()

def get_applied_adjustments(*, warehouse: Optional[Warehouse] = None, date_from: Optional[datetime] = None, date_to: Optional[datetime] = None, limit: Optional[int] = None) -> QuerySet:
    safe_limit = _normalize_limit(limit, 1000)
    try:
        qs = StockAdjustment.objects.select_related(
            "inventory__warehouse", "inventory__product", "initiated_by", "approved_by", "applied_transaction"
        ).filter(status=ADJUSTMENT_STATUS_APPLIED)
        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)
        if date_from:
            qs = qs.filter(applied_at__gte=date_from)
        if date_to:
            qs = qs.filter(applied_at__lte=date_to)
        return qs.order_by("-applied_at")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to fetch applied adjustments: %s", exc)
        return StockAdjustment.objects.none()

__all__ = [
    "get_inventory",
    "get_inventory_by_variant",
    "get_inventory_by_sku",
    "get_low_stock",
    "get_out_of_stock",
    "get_transactions",
    "get_recent_transactions",
    "get_inventory_dashboard",
    "get_reservations",
    "get_expired_reservations",
    "get_stock_adjustments",
    "get_warehouses",
    "get_default_warehouse",
    "get_warehouse_by_id",
    "get_adjustment_by_number",
    "get_reservation_by_token",
    "get_reservations_by_cart",
    "get_reservations_by_session",
    "get_active_stock_reservation_for_target",
    "serialize_inventory_list",
    "serialize_reservation_list",
    "serialize_transaction_list",
    "get_inventory_summary_for_target",
    "get_pending_adjustments",
    "get_applied_adjustments",
]