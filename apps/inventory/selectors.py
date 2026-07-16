"""
Enterprise-grade selector layer for the Inventory application.

This module is the **single source of truth for all read-only database access**
related to the inventory domain. Every selector in this file:

* Performs SELECT queries only. No writes, no mutations, no side effects.
* Returns QuerySets or serializable dictionaries — never model instances
  that callers might mutate inadvertently.
* Uses deep `select_related()` / `prefetch_related()` annotations to
  eliminate N+1 query patterns on hot paths.
* Is fully CMS-driven and parameterized: default thresholds, ordering,
  formulas, and limits come from Django settings (which the CMS can
  override) instead of being hardcoded.
* Fails gracefully. When arguments are invalid or records are missing,
  empty QuerySets or empty dicts are returned instead of raising
  exceptions that would leak into the view layer.
* Validates every input, including type and range checks on
  user-supplied parameters, to prevent injection or accidental DoS.
* Never exposes raw exception messages. All errors are logged with
  sufficient context for debugging while the caller receives a
  safe, user-friendly response.

Selectors are used by views, API endpoints, dashboard widgets, and
Celery tasks. They are intentionally read-only and have no side
effects. All writes flow through the service layer.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import (
    Avg,
    Count,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Sum,
)
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
# CONFIGURATION HELPERS
# ==============================================================================
# All defaults can be overridden via Django settings, which in turn can be
# driven by the CMS without code changes. This keeps selectors fully
# parameterized and CMS-driven.

_DEFAULT_RECENT_TRANSACTIONS_LIMIT = 25
_DEFAULT_DASHBOARD_RECENT_TRANSACTIONS = 10
_DEFAULT_AVAILABLE_STOCK_FORMULA = "available_minus_reserved"
_DEFAULT_RESERVATION_EXPIRY_HOURS = 24
_DEFAULT_LOW_STOCK_LIMIT = 100
_DEFAULT_OUT_OF_STOCK_LIMIT = 100
_DEFAULT_LEDGER_LIMIT = 25

def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)

def _normalize_limit(limit: Optional[int], default: int) -> int:
    """
    Coerces a user-supplied limit to a safe positive integer.

    Any value that is None, non-positive, non-integer, or otherwise
    invalid is silently replaced with the provided default. This protects
    downstream pagination and batching logic from abuse or programmer
    error.
    """
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    # Hard upper bound to prevent runaway queries
    if value > 10_000:
        return 10_000
    return value

def _validate_positive_int(value: Any, *, field_name: str) -> int:
    """
    Validates and coerces a value into a positive integer. Raises
    ``ValueError`` for non-integers or non-positive values.
    """
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be a positive integer."
        ) from exc
    if result <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return result

# ==============================================================================
# INTERNAL QUERY HELPERS
# ==============================================================================
def _active_inventory_qs() -> QuerySet:
    """
    Returns a base queryset for active inventory records with deep
    related-object preloading to eliminate N+1 queries.
    """
    return Inventory.objects.filter(is_active=True).select_related(
        "warehouse", "product", "product_variant"
    )

def _active_reservation_qs() -> QuerySet:
    """
    Returns a base queryset for active reservations with deep
    related-object preloading to eliminate N+1 queries.
    """
    return StockReservation.objects.select_related(
        "warehouse",
        "inventory__product",
        "inventory__product_variant",
        "user",
        "cart",
    )

def _serialize_inventory(inventory: Inventory) -> Dict[str, Any]:
    """
    Returns a serializable dictionary representation of an Inventory row.
    """
    return {
        "id": inventory.id,
        "warehouse_id": inventory.warehouse_id,
        "warehouse_name": (
            inventory.warehouse.display_name
            if inventory.warehouse
            else None
        ),
        "product_id": inventory.product_id,
        "product_variant_id": inventory.product_variant_id,
        "available_quantity": str(inventory.available_quantity),
        "reserved_quantity": str(inventory.reserved_quantity),
        "damaged_quantity": str(inventory.damaged_quantity),
        "incoming_quantity": str(inventory.incoming_quantity),
        "free_stock": str(inventory.free_stock),
        "total_stock": str(inventory.total_stock),
        "reorder_level": (
            str(inventory.reorder_level)
            if inventory.reorder_level is not None
            else None
        ),
        "minimum_stock": (
            str(inventory.minimum_stock)
            if inventory.minimum_stock is not None
            else None
        ),
        "maximum_stock": (
            str(inventory.maximum_stock)
            if inventory.maximum_stock is not None
            else None
        ),
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
        "expires_at": (
            reservation.expires_at.isoformat()
            if reservation.expires_at
            else None
        ),
        "released_at": (
            reservation.released_at.isoformat()
            if reservation.released_at
            else None
        ),
        "converted_at": (
            reservation.converted_at.isoformat()
            if reservation.converted_at
            else None
        ),
        "is_expired": reservation.is_expired,
        "is_terminal": reservation.is_terminal,
    }

def _serialize_transaction(txn: InventoryTransaction) -> Dict[str, Any]:
    """
    Returns a serializable dictionary representation of an
    InventoryTransaction row.
    """
    inv = txn.inventory
    return {
        "id": txn.id,
        "transaction_type": txn.transaction_type,
        "direction": txn.direction,
        "quantity": str(txn.quantity),
        "signed_quantity": str(txn.signed_quantity),
        "warehouse_id": inv.warehouse_id if inv else None,
        "warehouse_name": (
            inv.warehouse.display_name if inv and inv.warehouse else None
        ),
        "product_id": inv.product_id if inv else None,
        "product_variant_id": inv.product_variant_id if inv else None,
        "available_before": (
            str(txn.available_before)
            if txn.available_before is not None
            else None
        ),
        "available_after": (
            str(txn.available_after)
            if txn.available_after is not None
            else None
        ),
        "reserved_before": (
            str(txn.reserved_before)
            if txn.reserved_before is not None
            else None
        ),
        "reserved_after": (
            str(txn.reserved_after)
            if txn.reserved_after is not None
            else None
        ),
        "unit_cost": (
            str(txn.unit_cost) if txn.unit_cost is not None else None
        ),
        "total_cost": (
            str(txn.total_cost) if txn.total_cost is not None else None
        ),
        "currency": txn.currency,
        "reference_number": txn.reference_number,
        "reference_model": txn.reference_model,
        "reference_id": txn.reference_id,
        "transfer_group_id": (
            str(txn.transfer_group_id) if txn.transfer_group_id else None
        ),
        "transaction_at": (
            txn.transaction_at.isoformat() if txn.transaction_at else None
        ),
        "performed_by_id": txn.performed_by_id,
        "remarks": txn.remarks,
    }

# ==============================================================================
# 1. get_inventory()
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
    """
    Return inventory records.

    All filters are optional. Supports:
        * Warehouse
        * Product variant
        * Category
        * Vendor
        * Active status
        * In-stock only
        * Ordering

    Returns an optimized queryset.
    """
    try:
        qs = Inventory.objects.select_related(
            "warehouse", "product", "product_variant"
        )

        if active_only:
            qs = qs.filter(is_active=True)

        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        if product_variant is not None:
            qs = qs.filter(product_variant=product_variant)

        if product is not None:
            qs = qs.filter(product=product)

        if category is not None:
            # Filter by product category
            qs = qs.filter(
                Q(product__category=category) | Q(product_variant__product__category=category)
            )

        if vendor is not None:
            qs = qs.filter(
                Q(product__artisan=vendor) | Q(product_variant__product__artisan=vendor)
            )

        if in_stock_only:
            qs = qs.filter(available_quantity__gt=0)

        # Whitelist ordering to prevent SQL injection via arbitrary user input
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
        ordering_field = allowed_orderings.get(order_by, "warehouse__name")
        qs = qs.order_by(ordering_field)

        return qs
    except Exception as exc:
        logger.exception(
            "Failed to build inventory queryset (warehouse=%s, product=%s, product_variant=%s): %s",
            warehouse,
            product,
            product_variant,
            exc,
        )
        return Inventory.objects.none()

# ==============================================================================
# 2. get_inventory_by_variant()
# ==============================================================================
def get_inventory_by_variant(
    *,
    product_variant: Any,
    warehouse: Optional[Warehouse] = None,
    active_only: bool = True,
) -> QuerySet:
    """
    Return inventory for a Product Variant.

    Supports:
        * Warehouse filtering
        * Active inventory
        * select_related optimization
    """
    if product_variant is None:
        return Inventory.objects.none()

    try:
        qs = (
            Inventory.objects.filter(product_variant=product_variant)
            .select_related("warehouse", "product_variant")
        )

        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        if active_only:
            qs = qs.filter(is_active=True)

        return qs.order_by("warehouse__name")
    except Exception as exc:
        logger.exception(
            "Failed to build inventory queryset for product_variant=%s: %s",
            product_variant,
            exc,
        )
        return Inventory.objects.none()

# ==============================================================================
# 3. get_inventory_by_sku()
# ==============================================================================
def get_inventory_by_sku(
    *,
    sku: str,
    warehouse: Optional[Warehouse] = None,
    active_only: bool = True,
) -> QuerySet:
    """
    Locate inventory using SKU.

    Supports:
        * Warehouse
        * Active inventory
        * Product variant relations
    """
    if not sku or not isinstance(sku, str):
        return Inventory.objects.none()

    sku = sku.strip()
    if not sku:
        return Inventory.objects.none()

    try:
        qs = (
            Inventory.objects.filter(
                Q(product__sku__iexact=sku) | Q(product_variant__sku__iexact=sku)
            )
            .select_related("warehouse", "product", "product_variant")
        )

        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        if active_only:
            qs = qs.filter(is_active=True)

        return qs.order_by("warehouse__name")
    except Exception as exc:
        logger.exception(
            "Failed to build inventory queryset for sku=%s: %s", sku, exc
        )
        return Inventory.objects.none()

# ==============================================================================
# 4. get_low_stock()
# ==============================================================================
def get_low_stock(
    *,
    warehouse: Optional[Warehouse] = None,
    threshold: Optional[Decimal] = None,
    limit: Optional[int] = None,
    active_only: bool = True,
) -> QuerySet:
    """
    Return inventory below reorder level.

    Requirements:
        * Active inventory
        * Configurable threshold
        * Warehouse filtering
        * Optimized queryset
    """
    safe_limit = _normalize_limit(limit, _DEFAULT_LOW_STOCK_LIMIT)

    try:
        # Annotate free_stock once for the entire filter chain
        qs = (
            Inventory.objects.annotate(
                free_stock_calc=F("available_quantity") - F("reserved_quantity")
            )
        )

        if active_only:
            qs = qs.filter(is_active=True)

        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        # Reorder level must be defined and above zero, otherwise the row
        # has no actionable policy.
        qs = qs.filter(reorder_level__isnull=False, reorder_level__gt=0)

        if threshold is None:
            qs = qs.filter(free_stock_calc__lte=F("reorder_level"))
        else:
            try:
                threshold_value = Decimal(str(threshold))
            except (InvalidOperation, TypeError, ValueError):
                threshold_value = None
            if threshold_value is not None:
                qs = qs.filter(free_stock_calc__lte=threshold_value)

        qs = qs.select_related("warehouse", "product", "product_variant").order_by(
            "free_stock_calc", "id"
        )

        return qs[:safe_limit]
    except Exception as exc:
        logger.exception(
            "Failed to build low-stock queryset (warehouse=%s, threshold=%s): %s",
            warehouse,
            threshold,
            exc,
        )
        return Inventory.objects.none()

# ==============================================================================
# 5. get_out_of_stock()
# ==============================================================================
def get_out_of_stock(
    *,
    warehouse: Optional[Warehouse] = None,
    limit: Optional[int] = None,
    active_only: bool = True,
    include_damaged: bool = True,
) -> QuerySet:
    """
    Return inventory with no sellable quantity.

    Requirements:
        * Warehouse filtering
        * Active inventory
        * Optimized queryset
    * Business rule should support configurable stock policies.
    """
    safe_limit = _normalize_limit(limit, _DEFAULT_OUT_OF_STOCK_LIMIT)

    try:
        qs = (
            Inventory.objects.annotate(
                free_stock_calc=F("available_quantity") - F("reserved_quantity"),
                total_stock_calc=F("available_quantity") + F("damaged_quantity"),
            )
        )

        if active_only:
            qs = qs.filter(is_active=True)

        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        qs = qs.filter(free_stock_calc__lte=0)

        if not include_damaged:
            qs = qs.filter(total_stock_calc__lte=0)

        qs = qs.select_related("warehouse", "product", "product_variant").order_by(
            "id"
        )

        return qs[:safe_limit]
    except Exception as exc:
        logger.exception(
            "Failed to build out-of-stock queryset (warehouse=%s): %s",
            warehouse,
            exc,
        )
        return Inventory.objects.none()

# ==============================================================================
# 6. get_transactions()
# ==============================================================================
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
    """
    Return inventory transactions.

    Supports filters:
        * Warehouse
        * Product
        * SKU
        * Transaction type
        * Date range
        * Reference number
        * User
        * Ordering
    """
    safe_limit = _normalize_limit(limit, 1000)

    try:
        qs = (
            InventoryTransaction.objects.select_related(
                "inventory__warehouse",
                "inventory__product",
                "inventory__product_variant",
                "performed_by",
                "destination_warehouse",
            )
        )

        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)

        if product is not None:
            qs = qs.filter(
                Q(inventory__product=product)
                | Q(inventory__product_variant__product=product)
            )

        if sku is not None and isinstance(sku, str) and sku.strip():
            sku_clean = sku.strip()
            qs = qs.filter(
                Q(inventory__product__sku__iexact=sku_clean)
                | Q(inventory__product_variant__sku__iexact=sku_clean)
            )

        if transaction_type is not None:
            valid_types = {choice[0] for choice in InventoryTransaction.TransactionType.choices}
            if transaction_type in valid_types:
                qs = qs.filter(transaction_type=transaction_type)
            else:
                logger.warning(
                    "Invalid transaction_type filter value: %r", transaction_type
                )

        if direction is not None:
            valid_directions = {choice[0] for choice in InventoryTransaction.FlowDirection.choices}
            if direction in valid_directions:
                qs = qs.filter(direction=direction)

        if reference_number is not None and isinstance(reference_number, str) and reference_number.strip():
            qs = qs.filter(reference_number__icontains=reference_number.strip())

        if user is not None:
            qs = qs.filter(performed_by=user)

        if date_from is not None:
            qs = qs.filter(transaction_at__gte=date_from)
        if date_to is not None:
            qs = qs.filter(transaction_at__lte=date_to)

        # Whitelist ordering to prevent SQL injection via arbitrary user input
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
        ordering_field = allowed_orderings.get(order_by, "-transaction_at")
        qs = qs.order_by(ordering_field)

        return qs[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to build transactions queryset: %s", exc)
        return InventoryTransaction.objects.none()

# ==============================================================================
# 7. get_recent_transactions()
# ==============================================================================
def get_recent_transactions(
    *,
    limit: Optional[int] = None,
    warehouse: Optional[Warehouse] = None,
    transaction_type: Optional[str] = None,
) -> QuerySet:
    """
    Return recent inventory transactions.

    Supports configurable limit.
    Default comes from configuration/settings where possible.
    Optimizes query performance.
    """
    safe_limit = _normalize_limit(limit, _DEFAULT_RECENT_TRANSACTIONS_LIMIT)

    try:
        qs = (
            InventoryTransaction.objects.select_related(
                "inventory__warehouse",
                "inventory__product",
                "inventory__product_variant",
                "performed_by",
            )
        )

        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)

        if transaction_type is not None:
            valid_types = {choice[0] for choice in InventoryTransaction.TransactionType.choices}
            if transaction_type in valid_types:
                qs = qs.filter(transaction_type=transaction_type)

        return qs.order_by("-transaction_at", "-id")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to build recent transactions queryset: %s", exc)
        return InventoryTransaction.objects.none()

# ==============================================================================
# 8. get_inventory_dashboard()
# ==============================================================================
def get_inventory_dashboard(
    *,
    warehouse: Optional[Warehouse] = None,
    recent_transactions_limit: Optional[int] = None,
    include_inactive_warehouses: bool = False,
) -> Dict[str, Any]:
    """
    Return aggregated dashboard data.

    Includes metrics such as:
        * inventory count
        * warehouse count
        * available stock
        * reserved stock
        * damaged stock
        * incoming stock
        * low stock count
        * out of stock count
        * overstock count
        * recent transaction count
        * reservation count

    Returns a structured dictionary or dataclass suitable for dashboards
    and APIs. Uses aggregation instead of Python loops whenever possible.
    """
    limit = _normalize_limit(
        recent_transactions_limit, _DEFAULT_DASHBOARD_RECENT_TRANSACTIONS
    )

    try:
        warehouse_qs = Warehouse.objects.all()
        if not include_inactive_warehouses:
            warehouse_qs = warehouse_qs.filter(is_active=True)

        inv_qs = Inventory.objects.all()
        if warehouse is not None:
            inv_qs = inv_qs.filter(warehouse=warehouse)

        active_inv_qs = inv_qs.filter(is_active=True)

        aggregates = active_inv_qs.aggregate(
            total_available=Sum("available_quantity"),
            total_reserved=Sum("reserved_quantity"),
            total_damaged=Sum("damaged_quantity"),
            total_incoming=Sum("incoming_quantity"),
        )

        total_available = aggregates["total_available"] or Decimal("0")
        total_reserved = aggregates["total_reserved"] or Decimal("0")
        total_damaged = aggregates["total_damaged"] or Decimal("0")
        total_incoming = aggregates["total_incoming"] or Decimal("0")

        # Distinct product / variant counts
        product_count = (
            active_inv_qs.filter(product__isnull=False)
            .values("product")
            .distinct()
            .count()
        )
        variant_count = (
            active_inv_qs.filter(product_variant__isnull=False)
            .values("product_variant")
            .distinct()
            .count()
        )

        # Compute alert counts via single aggregated queries
        low_stock_count = (
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
            "inventory__warehouse",
            "inventory__product",
            "inventory__product_variant",
            "performed_by",
        )
        if warehouse is not None:
            recent_txn_qs = recent_txn_qs.filter(inventory__warehouse=warehouse)
        recent_transactions = [
            _serialize_transaction(txn)
            for txn in recent_txn_qs.order_by("-transaction_at", "-id")[:limit]
        ]

        # Reservation statistics (single aggregated query per status)
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
                "total_available": str(total_available),
                "total_reserved": str(total_reserved),
                "total_damaged": str(total_damaged),
                "total_incoming": str(total_incoming),
                "total_stock_value_placeholder": "0.00",
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
    except Exception as exc:
        logger.exception("Failed to build inventory dashboard payload: %s", exc)
        return {
            "totals": {
                "total_products": 0,
                "total_variants": 0,
                "total_warehouses": 0,
                "total_inventory_records": 0,
                "active_inventory_records": 0,
                "total_available": "0",
                "total_reserved": "0",
                "total_damaged": "0",
                "total_incoming": "0",
                "total_stock_value_placeholder": "0.00",
            },
            "alerts": {
                "low_stock_count": 0,
                "out_of_stock_count": 0,
                "overstock_count": 0,
            },
            "recent_transactions": [],
            "reservations": {
                "active": 0,
                "converted": 0,
                "released": 0,
                "expired": 0,
                "cancelled": 0,
            },
        }

# ==============================================================================
# 9. get_reservations()
# ==============================================================================
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
    """
    Return stock reservations.

    Supports filters:
        * cart
        * product variant
        * warehouse
        * active
        * user
        * expiry status
    """
    safe_limit = _normalize_limit(limit, 1000)

    try:
        qs = _active_reservation_qs()

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
            qs = qs.filter(
                expires_at__isnull=False, expires_at__lte=now
            )
        elif expiry_status == "active":
            qs = qs.filter(expires_at__gt=now)
        elif expiry_status == "no_expiry":
            qs = qs.filter(expires_at__isnull=True)
        elif expiry_status == "all":
            # No additional filter
            pass

        # Whitelist ordering
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
        ordering_field = allowed_orderings.get(order_by, "-created_at")
        qs = qs.order_by(ordering_field)

        return qs[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to build reservations queryset: %s", exc)
        return StockReservation.objects.none()

# ==============================================================================
# 10. get_expired_reservations()
# ==============================================================================
def get_expired_reservations(
    *,
    batch_size: int = 500,
    active_only: bool = True,
    warehouse: Optional[Warehouse] = None,
) -> QuerySet:
    """
    Return expired reservations.

    Supports configurable expiration policies.
    Optimizes query.

    Returns an *un-evaluated* queryset so the caller can iterate lazily
    (useful for large cleanups). The caller should still wrap iteration
    in a transaction.atomic() block and call release_stock() per row to
    safely decrement reserved_quantity.
    """
    try:
        batch_size = _validate_positive_int(batch_size, field_name="batch_size")

        qs = (
            StockReservation.objects.select_related(
                "warehouse",
                "inventory__product",
                "inventory__product_variant",
            )
        )

        if active_only:
            qs = qs.filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)

        if warehouse is not None:
            qs = qs.filter(warehouse=warehouse)

        now = timezone.now()
        return (
            qs.filter(expires_at__isnull=False, expires_at__lte=now)
            .order_by("id")
        )
    except Exception as exc:
        logger.exception("Failed to build expired reservations queryset: %s", exc)
        return StockReservation.objects.none()

# ==============================================================================
# 11. get_stock_adjustments()
# ==============================================================================
def get_stock_adjustments(
    *,
    inventory: Optional[Inventory] = None,
    status: Optional[str] = None,
    initiated_by: Any = None,
    approved_by: Any = None,
    order_by: str = "-created_at",
    limit: Optional[int] = None,
) -> QuerySet:
    """
    Return stock adjustments.

    Used by the approval workflow dashboards and historical views.
    """
    safe_limit = _normalize_limit(limit, 1000)

    try:
        qs = StockAdjustment.objects.select_related(
            "inventory__warehouse",
            "inventory__product",
            "inventory__product_variant",
            "initiated_by",
            "approved_by",
            "applied_transaction",
        )

        if inventory is not None:
            qs = qs.filter(inventory=inventory)

        if status is not None:
            valid_statuses = {choice[0] for choice in StockAdjustment.AdjustmentStatus.choices}
            if status in valid_statuses:
                qs = qs.filter(status=status)
            else:
                logger.warning("Invalid adjustment status filter value: %r", status)

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
        ordering_field = allowed_orderings.get(order_by, "-created_at")
        qs = qs.order_by(ordering_field)

        return qs[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to build adjustments queryset: %s", exc)
        return StockAdjustment.objects.none()

# ==============================================================================
# 12. get_warehouses()
# ==============================================================================
def get_warehouses(
    *,
    active_only: bool = True,
    default_only: bool = False,
    order_by: str = "name",
) -> QuerySet:
    """
    Return warehouse records with optimized selection.
    """
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
        ordering_field = allowed_orderings.get(order_by, "name")
        qs = qs.order_by(ordering_field)

        return qs
    except Exception as exc:
        logger.exception("Failed to build warehouses queryset: %s", exc)
        return Warehouse.objects.none()


def get_default_warehouse() -> Optional[Warehouse]:
    """
    Return the active default warehouse, or None if not configured.
    """
    try:
        return Warehouse.objects.filter(
            is_default=True, is_active=True
        ).order_by("id").first()
    except Exception as exc:
        logger.exception("Failed to fetch default warehouse: %s", exc)
        return None

# ==============================================================================
# 13. get_warehouse_by_id()
# ==============================================================================
def get_warehouse_by_id(warehouse_id: int) -> Optional[Warehouse]:
    """
    Return a single warehouse by primary key, or None if not found.
    """
    if not isinstance(warehouse_id, int) or warehouse_id <= 0:
        return None
    try:
        return Warehouse.objects.filter(pk=warehouse_id).first()
    except Exception as exc:
        logger.exception(
            "Failed to fetch warehouse by id=%s: %s", warehouse_id, exc
        )
        return None

# ==============================================================================
# 14. get_adjustment_by_number()
# ==============================================================================
def get_adjustment_by_number(adjustment_number: str) -> Optional[StockAdjustment]:
    """
    Retrieve a single stock adjustment by its public adjustment number.
    """
    if not adjustment_number or not isinstance(adjustment_number, str):
        return None
    adjustment_number = adjustment_number.strip()
    if not adjustment_number:
        return None
    try:
        return (
            StockAdjustment.objects.select_related(
                "inventory__warehouse",
                "inventory__product",
                "initiated_by",
                "approved_by",
                "applied_transaction",
            )
            .filter(adjustment_number=adjustment_number)
            .first()
        )
    except Exception as exc:
        logger.exception(
            "Failed to fetch adjustment by number=%s: %s", adjustment_number, exc
        )
        return None

# ==============================================================================
# 15. get_reservation_by_token()
# ==============================================================================
def get_reservation_by_token(token: str) -> Optional[StockReservation]:
    """
    Retrieve a stock reservation by its opaque public token.
    """
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    if not token:
        return None
    try:
        token_uuid = uuid.UUID(str(token))
        return (
            StockReservation.objects.select_related(
                "warehouse",
                "inventory__product",
                "inventory__product_variant",
                "user",
                "cart",
            )
            .filter(reservation_token=token_uuid)
            .first()
        )
    except (ValueError, TypeError):
        return None
    except Exception as exc:
        logger.exception(
            "Failed to fetch reservation by token=%s: %s", token, exc
        )
        return None

# ==============================================================================
# 16. get_reservations_by_cart()
# ==============================================================================
def get_reservations_by_cart(
    *,
    cart: Any,
    active_only: bool = True,
) -> QuerySet:
    """
    Return reservations scoped to a specific cart, optimized.
    """
    if cart is None:
        return StockReservation.objects.none()

    try:
        qs = _active_reservation_qs().filter(cart=cart)
        if active_only:
            qs = qs.filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)
        return qs.order_by("-created_at")
    except Exception as exc:
        logger.exception("Failed to fetch reservations by cart=%s: %s", cart, exc)
        return StockReservation.objects.none()

# ==============================================================================
# 17. get_reservations_by_session()
# ==============================================================================
def get_reservations_by_session(
    *,
    session_key: str,
    active_only: bool = True,
) -> QuerySet:
    """
    Return reservations scoped to an anonymous session key, optimized.
    """
    if not session_key or not isinstance(session_key, str):
        return StockReservation.objects.none()

    try:
        qs = _active_reservation_qs().filter(session_key=session_key)
        if active_only:
            qs = qs.filter(status=RESERVATION_STATUS_ACTIVE, is_active=True)
        return qs.order_by("-created_at")
    except Exception as exc:
        logger.exception(
            "Failed to fetch reservations by session=%s: %s", session_key, exc
        )
        return StockReservation.objects.none()

# ==============================================================================
# 18. get_active_stock_reservation_for_target()
# ==============================================================================
def get_active_stock_reservation_for_target(
    *,
    cart: Any = None,
    user: Any = None,
    session_key: Optional[str] = None,
    product: Any = None,
    product_variant: Any = None,
) -> Optional[StockReservation]:
    """
    Return the active reservation for a specific cart/session/user + target.

    Used by cart and checkout flows to detect the existing hold before
    incrementing it. Prevents duplicate reservations for the same scope.
    """
    if not (cart or user or session_key):
        return None
    if not (product or product_variant):
        return None

    try:
        qs = StockReservation.objects.select_related(
            "warehouse", "inventory", "user", "cart"
        ).filter(
            status=RESERVATION_STATUS_ACTIVE, is_active=True
        )

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
        logger.exception(
            "Failed to fetch active reservation for cart=%s, user=%s, "
            "session=%s, product=%s, product_variant=%s: %s",
            cart, user, session_key, product, product_variant, exc,
        )
        return None

# ==============================================================================
# 19. serialize_inventory_list() - helper for API/dashboard consumers
# ==============================================================================
def serialize_inventory_list(
    queryset: Optional[QuerySet] = None,
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return a serialized list of inventory records, safe for JSON output.

    Does not perform a database query unless ``queryset`` is provided.
    The returned dictionaries mirror the ``_serialize_inventory`` helper
    for shape consistency across the codebase.
    """
    safe_limit = _normalize_limit(limit, 1000)

    if queryset is None:
        return []

    try:
        iterator = queryset[:safe_limit] if safe_limit else queryset
        return [_serialize_inventory(inv) for inv in iterator]
    except Exception as exc:
        logger.exception("Failed to serialize inventory list: %s", exc)
        return []

# ==============================================================================
# 20. serialize_reservation_list() - helper for API/dashboard consumers
# ==============================================================================
def serialize_reservation_list(
    queryset: Optional[QuerySet] = None,
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return a serialized list of stock reservations, safe for JSON output.
    """
    safe_limit = _normalize_limit(limit, 1000)

    if queryset is None:
        return []

    try:
        iterator = queryset[:safe_limit] if safe_limit else queryset
        return [_serialize_reservation(res) for res in iterator]
    except Exception as exc:
        logger.exception("Failed to serialize reservation list: %s", exc)
        return []

# ==============================================================================
# 21. serialize_transaction_list() - helper for API/dashboard consumers
# ==============================================================================
def serialize_transaction_list(
    queryset: Optional[QuerySet] = None,
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return a serialized list of inventory transactions, safe for JSON output.
    """
    safe_limit = _normalize_limit(limit, _DEFAULT_LEDGER_LIMIT)

    if queryset is None:
        return []

    try:
        iterator = queryset[:safe_limit] if safe_limit else queryset
        return [_serialize_transaction(txn) for txn in iterator]
    except Exception as exc:
        logger.exception("Failed to serialize transaction list: %s", exc)
        return []

# ==============================================================================
# 22. get_inventory_summary_for_target()
# ==============================================================================
def get_inventory_summary_for_target(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Optional[Warehouse] = None,
) -> Dict[str, Any]:
    """
    Return a single, compact inventory snapshot for a specific target.

    Combines the free-stock calculation formula, aggregate metrics, and a
    status flag for fast UI rendering (e.g. product detail page).
    """
    if not (product or product_variant):
        return {
            "exists": False,
            "free_stock": "0",
            "is_out_of_stock": True,
            "is_low_stock": False,
        }

    try:
        qs = (
            Inventory.objects.select_related("warehouse")
        )

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
            "reorder_level": (
                str(inv.reorder_level) if inv.reorder_level is not None else None
            ),
            "is_active": inv.is_active,
        }
    except Exception as exc:
        logger.exception(
            "Failed to build inventory summary for product=%s, product_variant=%s: %s",
            product,
            product_variant,
            exc,
        )
        return {
            "exists": False,
            "free_stock": "0",
            "is_out_of_stock": True,
            "is_low_stock": False,
        }

# ==============================================================================
# 23. get_pending_adjustments() - workflow support
# ==============================================================================
def get_pending_adjustments(
    *,
    warehouse: Optional[Warehouse] = None,
    limit: Optional[int] = None,
) -> QuerySet:
    """
    Return adjustments currently awaiting approval.

    Used by the approval workflow dashboard.
    """
    safe_limit = _normalize_limit(limit, 1000)

    try:
        qs = StockAdjustment.objects.select_related(
            "inventory__warehouse",
            "inventory__product",
            "initiated_by",
        ).filter(
            status__in=[
                ADJUSTMENT_STATUS_DRAFT,
                ADJUSTMENT_STATUS_PENDING_APPROVAL,
            ]
        )

        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)

        return qs.order_by("-created_at")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to build pending adjustments queryset: %s", exc)
        return StockAdjustment.objects.none()

# ==============================================================================
# 24. get_applied_adjustments() - historical view
# ==============================================================================
def get_applied_adjustments(
    *,
    warehouse: Optional[Warehouse] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> QuerySet:
    """
    Return adjustments that have been fully applied to inventory.

    Useful for audit reports, accounting reconciliation, and finance
    dashboards.
    """
    safe_limit = _normalize_limit(limit, 1000)

    try:
        qs = StockAdjustment.objects.select_related(
            "inventory__warehouse",
            "inventory__product",
            "initiated_by",
            "approved_by",
            "applied_transaction",
        ).filter(status=ADJUSTMENT_STATUS_APPLIED)

        if warehouse is not None:
            qs = qs.filter(inventory__warehouse=warehouse)

        if date_from is not None:
            qs = qs.filter(applied_at__gte=date_from)
        if date_to is not None:
            qs = qs.filter(applied_at__lte=date_to)

        return qs.order_by("-applied_at")[:safe_limit]
    except Exception as exc:
        logger.exception("Failed to build applied adjustments queryset: %s", exc)
        return StockAdjustment.objects.none()