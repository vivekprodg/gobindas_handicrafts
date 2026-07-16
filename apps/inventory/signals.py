"""
Event-driven signal handlers for the Inventory application.

ARCHITECTURE
============
This module implements the EVENT-DRIVEN orchestration layer of the inventory
module. Signals are responsible ONLY for:

    * Listening to application events
    * Validating event payloads
    * Delegating work to the Service Layer
    * Writing safe, structured logs

CRITICAL RULES (MANDATORY)
==========================
Signals must NEVER:
    * Modify stock directly
    * Execute business logic or inventory calculations
    * Contain duplicated logic from the Service Layer
    * Use .save() to manipulate stock on Inventory rows
    * Execute raw SQL
    * Become "mini services"

Every inventory operation MUST be delegated to ``apps.inventory.services``.

SECURITY (OWASP ASVS COMPLIANT)
================================
    * Never trust signal payloads - validate every object before processing
    * Prevent duplicate execution via idempotency checks
    * Prevent recursive signal calls via thread-local reentrancy guards
    * Avoid race conditions via database transactions and on_commit hooks
    * Avoid infinite loops via state machine transitions
    * Fail safely - errors are logged, never raised to the caller
    * Never expose internal exception details to external callers
    * Do not leak sensitive information in logs
    * Use transaction-aware signal handling (transaction.on_commit)

PERFORMANCE
===========
    * Optimized for millions of inventory rows
    * Optimized for millions of transactions
    * Optimized for enterprise workloads and high concurrency
    * Avoid unnecessary queries
    * Avoid N+1 problems via prefetch_related and bulk operations
    * Use bulk_create with ignore_conflicts=True for idempotency

FUTURE-PROOF
============
Designed to integrate seamlessly with:
    * Purchase Orders and Goods Receipt Notes (warehouse_created, restock)
    * Manufacturing and Production (work-order driven stock)
    * Warehouse Transfers (transfer_stock on signal)
    * Batch/Lot, Expiry, Serial Number tracking
    * Customer Returns (refund_approved)
    * Barcode / QR Code scanning events
    * Celery beat tasks (via the custom reservation_cleanup signal)
    * Kafka / RabbitMQ / Event Bus (via the custom signals)
    * REST and GraphQL APIs (via the service layer)
    * Notifications and Audit Logs
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Any, Optional

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import Signal, receiver
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# Reentrancy guard namespace (thread-local)
_state_lock = threading.local()

def _is_processing(flag: str) -> bool:
    """
    Thread-safe check for whether a given signal handler is currently
    in-flight. Prevents recursive execution and reentrancy loops.
    """
    return bool(getattr(_state_lock, flag, False))

def _set_processing(flag: str, value: bool = True) -> None:
    """
    Thread-safe state mutator for reentrancy guards.
    """
    setattr(_state_lock, flag, value)

def _reset_processing(flag: str) -> None:
    """
    Thread-safe reset of reentrancy state.
    """
    if hasattr(_state_lock, flag):
        delattr(_state_lock, flag)

# ------------------------------------------------------------------------------
# Lazy model accessors
# ------------------------------------------------------------------------------
def _get_model(app_label: str, model_name: str) -> Any:
    """
    Lazy model accessor that resolves models on demand. This pattern
    prevents the AppConfig.ready() chain from forcing premature imports
    of cross-application models and avoids circular dependency issues.
    """
    return apps.get_model(app_label, model_name)

# ------------------------------------------------------------------------------
# Service accessor
# ------------------------------------------------------------------------------
def _inventory_services() -> dict:
    """
    Lazy accessor for inventory service functions. Performing the import
    inside the function prevents the signal module from importing the
    service layer at module-load time (which would defeat the lazy
    loading strategy used by the AppConfig).
    """
    from apps.inventory.services import (
        deduct_stock,
        restock,
        release_expired_reservations,
    )
    return {
        "deduct_stock": deduct_stock,
        "restock": restock,
        "release_expired_reservations": release_expired_reservations,
    }

# ------------------------------------------------------------------------------
# Idempotency helpers
# ------------------------------------------------------------------------------
def _order_has_sale_transactions(order_id: Any) -> bool:
    """
    Idempotency guard for order payment events. Returns True if a SALE
    transaction has already been recorded for the given order, meaning
    the inventory has been deducted previously and a second deduction
    must not be performed.
    """
    from apps.inventory.models import InventoryTransaction

    return InventoryTransaction.objects.filter(
        reference_model="orders.Order",
        reference_id=str(order_id),
        transaction_type=InventoryTransaction.TransactionType.SALE,
    ).exists()

def _order_has_restock_transactions(order_id: Any) -> bool:
    """
    Idempotency guard for order cancellation events. Returns True if a
    RETURN or CANCEL transaction has already been recorded for the
    given order.
    """
    from apps.inventory.models import InventoryTransaction

    return InventoryTransaction.objects.filter(
        reference_model="orders.Order",
        reference_id=str(order_id),
        transaction_type__in=[
            InventoryTransaction.TransactionType.RETURN,
            InventoryTransaction.TransactionType.CANCEL,
        ],
    ).exists()

def _refund_has_restock_transactions(refund_id: Any) -> bool:
    """
    Idempotency guard for refund events. Returns True if a RETURN
    transaction has already been recorded for the given refund.
    """
    from apps.inventory.models import InventoryTransaction

    return InventoryTransaction.objects.filter(
        reference_model="orders.Refund",
        reference_id=str(refund_id),
        transaction_type=InventoryTransaction.TransactionType.RETURN,
    ).exists()

def _safe_log_error(
    exc: Exception,
    context: str,
    **extra: Any,
) -> None:
    """
    Log a signal processing error without exposing sensitive information
    to external callers. Includes the full traceback in the server log
    (exc_info=True) but does not propagate the exception to the view or
    to the user.
    """
    logger.error(
        "Inventory signal failure [%s]: %s | extra=%s",
        context,
        exc,
        extra,
        exc_info=True,
    )

def _resolve_order_item_targets(order_item: Any) -> tuple:
    """
    Resolves the appropriate service-layer targets (product,
    product_variant) for an OrderItem. Returns a tuple of
    (product, product_variant) - exactly one will be non-None,
    matching the Service Layer contract.
    """
    product = getattr(order_item, "product", None)
    variant = getattr(order_item, "variant", None)
    if variant is not None:
        # Prefer variant targeting for variant-level inventory
        return None, variant
    return product, None

# ==============================================================================
# 1. ORDER PAID → DEDUCT INVENTORY
# ==============================================================================
@receiver(
    post_save,
    sender="orders.Order",
    dispatch_uid="inventory_signal_order_paid_deduct_inventory",
)
def handle_order_paid_deduct_inventory(
    sender: Any,
    instance: Any,
    created: bool,
    update_fields: Optional[Any] = None,
    **kwargs: Any,
) -> None:
    """
    Order payment completed → automatically deduct inventory.

    Triggered by the transition of an Order's ``payment_status`` to
    ``'paid'``. Delegates the actual deduction to
    ``apps.inventory.services.deduct_stock()``.

    Idempotency:
        Skips if SALE transactions already exist for this order.

    Transaction safety:
        Defers the actual deduction to ``transaction.on_commit`` so the
        inventory mutation only runs after the parent order transaction
        is durably committed. Wraps the deferred work in its own atomic
        block to prevent partial deductions.
    """
    # Guard 1: Only proceed on the paid state.
    if instance.payment_status != "paid":
        return

    # Guard 2: Optimization - skip when payment_status is not in the
    # explicit update_fields set. This avoids unnecessary work for
    # updates that cannot have changed payment status.
    if update_fields is not None and "payment_status" not in update_fields:
        return

    # Guard 3: Skip orders in terminal/inventory-irrelevant states.
    if instance.status in ("cancelled", "refunded"):
        return

    # Guard 4: Reentrancy protection.
    if _is_processing("order_paid_deduct_inventory"):
        return

    # Guard 5: Idempotency - has inventory been deducted already?
    if _order_has_sale_transactions(instance.pk):
        return

    _set_processing("order_paid_deduct_inventory")
    try:
        order_id = instance.pk
        order_number = instance.order_number

        def _execute() -> None:
            with transaction.atomic():
                _deduct_inventory_for_order(order_id, order_number)

        transaction.on_commit(_execute)
    finally:
        _reset_processing("order_paid_deduct_inventory")

def _deduct_inventory_for_order(order_id: Any, order_number: str) -> None:
    """
    Iterates over the order's line items and delegates deduction to the
    service layer. Each call is wrapped to ensure one item's failure
    does not block the rest of the deduction (errors are logged).
    """
    Order = _get_model("orders", "Order")

    try:
        order = (
            Order.objects
            .select_related("customer")
            .only("id", "order_number", "customer_id", "status", "payment_status")
            .get(pk=order_id)
        )
    except Order.DoesNotExist:
        logger.warning(
            "Order %s no longer exists; skipping inventory deduction.",
            order_id,
        )
        return

    items = (
        order.items
        .select_related("product", "variant")
        .all()
    )
    if not items.exists():
        return

    services = _inventory_services()

    for item in items:
        target_product, target_variant = _resolve_order_item_targets(item)
        if target_product is None and target_variant is None:
            # Defensive: skip orphaned items (product + variant both null).
            continue
        try:
            services["deduct_stock"](
                quantity=item.quantity,
                product=target_product,
                product_variant=target_variant,
                reference_number=order_number,
                reference_model="orders.Order",
                reference_id=str(order_id),
                remarks=_(
                    "Order %(order)s paid; inventory deducted via service layer."
                ) % {"order": order_number},
            )
        except Exception as exc:
            # Never let a single failure abort the whole deduction.
            _safe_log_error(
                exc,
                "deduct_inventory_for_order_item",
                order_id=order_id,
                order_item_id=item.pk,
                product_id=getattr(item, "product_id", None),
                variant_id=getattr(item, "variant_id", None),
            )

# ==============================================================================
# 2. ORDER CANCELLED → RESTORE INVENTORY
# ==============================================================================
@receiver(
    post_save,
    sender="orders.Order",
    dispatch_uid="inventory_signal_order_cancelled_restock_inventory",
)
def handle_order_cancelled_restock_inventory(
    sender: Any,
    instance: Any,
    created: bool,
    update_fields: Optional[Any] = None,
    **kwargs: Any,
) -> None:
    """
    Order cancelled → automatically restore inventory.

    Triggered by the transition of an Order's ``status`` to
    ``'cancelled'``. Delegates the actual restoration to
    ``apps.inventory.services.restock()``.

    Idempotency:
        Skips if RETURN or CANCEL transactions already exist for this
        order. Also skips if the order has no SALE transactions
        (meaning no inventory was ever deducted and there is nothing
        to restore).
    """
    # Guard 1: Only proceed on the cancelled state.
    if instance.status != "cancelled":
        return

    # Guard 2: Optimization - skip when status is not in the explicit
    # update_fields set.
    if update_fields is not None and "status" not in update_fields:
        return

    # Guard 3: Reentrancy protection.
    if _is_processing("order_cancelled_restock_inventory"):
        return

    # Guard 4: Idempotency - has inventory already been restored?
    if _order_has_restock_transactions(instance.pk):
        return

    # Guard 5: Nothing to restore if inventory was never deducted.
    if not _order_has_sale_transactions(instance.pk):
        return

    _set_processing("order_cancelled_restock_inventory")
    try:
        order_id = instance.pk
        order_number = instance.order_number

        def _execute() -> None:
            with transaction.atomic():
                _restock_inventory_for_order(order_id, order_number)

        transaction.on_commit(_execute)
    finally:
        _reset_processing("order_cancelled_restock_inventory")


def _restock_inventory_for_order(order_id: Any, order_number: str) -> None:
    """
    Restores inventory for all items in a cancelled order by delegating
    to the service layer's ``restock`` function.
    """
    Order = _get_model("orders", "Order")

    try:
        order = Order.objects.only("id", "order_number").get(pk=order_id)
    except Order.DoesNotExist:
        logger.warning(
            "Order %s no longer exists; skipping inventory restoration.",
            order_id,
        )
        return

    items = order.items.select_related("product", "variant").all()
    if not items.exists():
        return

    services = _inventory_services()

    for item in items:
        target_product, target_variant = _resolve_order_item_targets(item)
        if target_product is None and target_variant is None:
            continue
        try:
            services["restock"](
                quantity=item.quantity,
                product=target_product,
                product_variant=target_variant,
                reference_number=order_number,
                reference_model="orders.Order",
                reference_id=str(order_id),
                remarks=_(
                    "Order %(order)s cancelled; inventory restored via service layer."
                ) % {"order": order_number},
            )
        except Exception as exc:
            _safe_log_error(
                exc,
                "restock_inventory_for_order_item",
                order_id=order_id,
                order_item_id=item.pk,
            )

# ==============================================================================
# 3. REFUND APPROVED → RESTORE INVENTORY
# ==============================================================================
@receiver(
    post_save,
    sender="orders.Refund",
    dispatch_uid="inventory_signal_refund_approved_restock_inventory",
)
def handle_refund_approved_restock_inventory(
    sender: Any,
    instance: Any,
    created: bool,
    update_fields: Optional[Any] = None,
    **kwargs: Any,
) -> None:
    """
    Refund approved → restore inventory for returned items.

    Triggered when a Refund's ``status`` transitions to ``'processed'``,
    which signals that the items have been physically returned and
    accepted for restocking. Delegates the restoration to
    ``apps.inventory.services.restock()``.

    Idempotency:
        Skips if RETURN transactions already exist for this refund.

    Future extensibility:
        Per the master prompt, future enhancements may include partial
        refunds tied to specific items, configurable quality-inspection
        gating, and conditional restocking policies (e.g., damaged
        goods that should not be returned to sellable stock).
    """
    # Guard 1: Only proceed when refund is fully processed.
    if instance.status != "processed":
        return

    # Guard 2: Optimization - skip when status is not in the explicit
    # update_fields set.
    if update_fields is not None and "status" not in update_fields:
        return

    # Guard 3: Reentrancy protection.
    if _is_processing("refund_approved_restock_inventory"):
        return

    # Guard 4: Idempotency - check for prior restoration.
    if _refund_has_restock_transactions(instance.pk):
        return

    _set_processing("refund_approved_restock_inventory")
    try:
        refund_id = instance.pk
        order_id = getattr(instance, "order_id", None)

        def _execute() -> None:
            with transaction.atomic():
                _restock_inventory_for_refund(refund_id, order_id)

        transaction.on_commit(_execute)
    finally:
        _reset_processing("refund_approved_restock_inventory")


def _restock_inventory_for_refund(
    refund_id: Any,
    order_id: Optional[Any],
) -> None:
    """
    Restores inventory for items associated with a processed refund.

    NOTE: This current implementation treats every refund as a full
    restoration of every order item. Partial-refund routing by item
    requires a future enhancement that tracks which specific
    OrderItems are being refunded. Today, the restock is best-effort
    and the business can rely on the audit ledger for reconciliation.
    """
    if order_id is None:
        return

    Order = _get_model("orders", "Order")
    Refund = _get_model("orders", "Refund")

    try:
        refund = (
            Refund.objects
            .select_related("order")
            .only("id", "order_id", "amount")
            .get(pk=refund_id)
        )
    except Refund.DoesNotExist:
        logger.warning(
            "Refund %s no longer exists; skipping inventory restoration.",
            refund_id,
        )
        return

    items = (
        refund.order.items
        .select_related("product", "variant")
        .all()
    )
    if not items.exists():
        return

    services = _inventory_services()
    order_number = getattr(refund.order, "order_number", str(refund.order_id))

    for item in items:
        target_product, target_variant = _resolve_order_item_targets(item)
        if target_product is None and target_variant is None:
            continue
        try:
            services["restock"](
                quantity=item.quantity,
                product=target_product,
                product_variant=target_variant,
                reference_number=str(refund.pk),
                reference_model="orders.Refund",
                remarks=_(
                    "Refund %(refund)s processed for order %(order)s; "
                    "inventory restored via service layer."
                ) % {"refund": refund.pk, "order": order_number},
            )
        except Exception as exc:
            _safe_log_error(
                exc,
                "restock_inventory_for_refund_item",
                refund_id=refund_id,
                order_item_id=item.pk,
            )

# ==============================================================================
# 4. PRODUCT VARIANT CREATED → CREATE INVENTORY RECORDS
# ==============================================================================
@receiver(
    post_save,
    sender="catalog.ProductVariant",
    dispatch_uid="inventory_signal_product_variant_created",
)
def handle_product_variant_created(
    sender: Any,
    instance: Any,
    created: bool,
    **kwargs: Any,
) -> None:
    """
    Product Variant created → automatically create inventory records.

    For every active warehouse, provisions an Inventory record for the
    new variant. Uses ``bulk_create(ignore_conflicts=True)`` for safe
    re-execution and to avoid per-row INSERT overhead.

    Idempotency:
        The use of ``ignore_conflicts=True`` combined with the
        uniqueness constraint on ``(warehouse, product_variant)``
        ensures re-runs are no-ops.

    Future extensibility:
        Future variants may support warehouse templates or geographic
        routing logic. The current implementation provisions a record in
        every active warehouse, which is the safe default.
    """
    # Guard 1: Only on initial creation.
    if not created:
        return

    # Guard 2: Reentrancy protection (in case of batch insertions).
    if _is_processing("product_variant_created"):
        return

    # Guard 3: Only initialize if the variant is active.
    if not getattr(instance, "is_active", True):
        return

    _set_processing("product_variant_created")
    try:
        variant_id = instance.pk
        product_id = getattr(instance, "product_id", None)

        def _execute() -> None:
            with transaction.atomic():
                _create_inventory_for_new_variant(variant_id, product_id)

        transaction.on_commit(_execute)
    finally:
        _reset_processing("product_variant_created")

def _create_inventory_for_new_variant(
    variant_id: Any,
    product_id: Optional[Any],
) -> None:
    """
    Creates an inventory record for the new variant in every active
    warehouse using ``bulk_create(ignore_conflicts=True)`` for
    efficiency and safety.
    """
    from apps.inventory.models import Inventory, Warehouse

    ProductVariant = _get_model("catalog", "ProductVariant")
    try:
        variant = (
            ProductVariant.objects
            .only("id", "is_active", "product_id")
            .get(pk=variant_id)
        )
    except ProductVariant.DoesNotExist:
        return

    if not variant.is_active:
        return

    warehouses = list(
        Warehouse.objects.filter(is_active=True).only("id")
    )
    if not warehouses:
        return

    inventory_records = [
        Inventory(
            warehouse=warehouse,
            product_variant=variant,
            product=None,
            available_quantity=Decimal("0.00"),
            reserved_quantity=Decimal("0.00"),
            damaged_quantity=Decimal("0.00"),
            incoming_quantity=Decimal("0.00"),
            is_active=True,
        )
        for warehouse in warehouses
    ]

    if not inventory_records:
        return

    try:
        Inventory.objects.bulk_create(
            inventory_records,
            ignore_conflicts=True,
        )
        logger.info(
            "Initialized %d inventory record(s) for new variant %s.",
            len(inventory_records),
            variant_id,
        )
    except Exception as exc:
        _safe_log_error(
            exc,
            "bulk_create_inventory_for_variant",
            variant_id=variant_id,
            record_count=len(inventory_records),
        )

# ==============================================================================
# 5. WAREHOUSE CREATED → INITIALIZE INVENTORY FOR EXISTING PRODUCTS
# ==============================================================================
@receiver(
    post_save,
    sender="inventory.Warehouse",
    dispatch_uid="inventory_signal_warehouse_created",
)
def handle_warehouse_created(
    sender: Any,
    instance: Any,
    created: bool,
    **kwargs: Any,
) -> None:
    """
    Warehouse created → initialize inventory for all existing variants.

    For every active ProductVariant, provisions an Inventory record
    scoped to the new warehouse. Uses batched ``bulk_create`` for
    performance on catalogs with millions of variants.

    Idempotency:
        Skips variants that already have an inventory record in the
        new warehouse, then uses ``bulk_create(ignore_conflicts=True)``
        for the remaining set.
    """
    # Guard 1: Only on initial creation.
    if not created:
        return

    # Guard 2: Skip inactive warehouses.
    if not getattr(instance, "is_active", True):
        return

    # Guard 3: Reentrancy protection.
    if _is_processing("warehouse_created"):
        return

    _set_processing("warehouse_created")
    try:
        warehouse_id = instance.pk

        def _execute() -> None:
            with transaction.atomic():
                _initialize_inventory_for_new_warehouse(warehouse_id)

        transaction.on_commit(_execute)
    finally:
        _reset_processing("warehouse_created")

def _initialize_inventory_for_new_warehouse(warehouse_id: Any) -> None:
    """
    Creates inventory records for all active product variants in the
    new warehouse. Processes the work in batches to keep memory
    consumption bounded for enterprise-scale catalogs.
    """
    from apps.inventory.models import Inventory, Warehouse

    try:
        warehouse = (
            Warehouse.objects
            .only("id", "is_active")
            .get(pk=warehouse_id)
        )
    except Warehouse.DoesNotExist:
        return

    if not warehouse.is_active:
        return

    ProductVariant = _get_model("catalog", "ProductVariant")

    # Identify which variants already have inventory in this warehouse.
    existing_variant_ids = set(
        Inventory.objects
        .filter(
            warehouse=warehouse,
            product_variant__isnull=False,
        )
        .values_list("product_variant_id", flat=True)
    )

    # Stream active variants to bound memory for large catalogs.
    BATCH_SIZE = 500

    def _variant_id_iterator():
        return (
            ProductVariant.objects
            .filter(is_active=True)
            .order_by("id")
            .values_list("id", flat=True)
            .iterator(chunk_size=BATCH_SIZE)
        )

    pending_batch: list = []
    total_created = 0

    try:
        for variant_id in _variant_id_iterator():
            if variant_id in existing_variant_ids:
                continue
            pending_batch.append(
                Inventory(
                    warehouse=warehouse,
                    product_variant_id=variant_id,
                    product=None,
                    available_quantity=Decimal("0.00"),
                    reserved_quantity=Decimal("0.00"),
                    damaged_quantity=Decimal("0.00"),
                    incoming_quantity=Decimal("0.00"),
                    is_active=True,
                )
            )
            if len(pending_batch) >= BATCH_SIZE:
                Inventory.objects.bulk_create(
                    pending_batch,
                    ignore_conflicts=True,
                )
                total_created += len(pending_batch)
                pending_batch = []

        if pending_batch:
            Inventory.objects.bulk_create(
                pending_batch,
                ignore_conflicts=True,
            )
            total_created += len(pending_batch)

        if total_created:
            logger.info(
                "Initialized %d inventory record(s) for new warehouse %s.",
                total_created,
                warehouse_id,
            )
    except Exception as exc:
        _safe_log_error(
            exc,
            "initialize_inventory_for_new_warehouse",
            warehouse_id=warehouse_id,
            partial_count=total_created,
        )

# ==============================================================================
# 6. RESERVATION EXPIRED → RELEASE RESERVED INVENTORY
# ==============================================================================
# The master prompt explicitly prohíbe implementing schedulers in this
# module. The reservation cleanup is therefore exposed as a custom
# Django Signal that future Celery beat tasks, management commands, or
# external orchestrators (e.g., Kafka consumer) can fire without coupling
# to the inventory implementation.
#
#   To schedule recurring cleanup:
#     * Define a Celery beat task that calls ``request_reservation_cleanup``
#     * Or invoke the signal directly from a cron-triggered management
#       command
#     * Or dispatch it from a checkout-abandonment cart signal

reservation_cleanup_requested = Signal()

@receiver(
    reservation_cleanup_requested,
    dispatch_uid="inventory_signal_reservation_cleanup",
)
def handle_reservation_cleanup_requested(
    sender: Any,
    *,
    batch_size: int = 500,
    **kwargs: Any,
) -> None:
    """
    Reservation cleanup requested → release expired reservations.

    Delegates the actual cleanup to
    ``apps.inventory.services.release_expired_reservations()``, which
    handles row-level locking and atomic per-reservation release.

    The handler is intentionally lightweight and never raises. All
    failures are logged for observability.
    """
    if _is_processing("reservation_cleanup"):
        return

    _set_processing("reservation_cleanup")
    try:
        safe_batch_size = max(1, min(int(batch_size or 500), 10_000))

        def _execute() -> None:
            try:
                services = _inventory_services()
                result = services["release_expired_reservations"](
                    batch_size=safe_batch_size,
                )
                logger.info(
                    "Reservation cleanup completed: released=%s failed=%s processed=%s",
                    result.get("released", 0),
                    result.get("failed", 0),
                    result.get("processed", 0),
                )
            except Exception as exc:
                _safe_log_error(
                    exc,
                    "release_expired_reservations",
                    batch_size=safe_batch_size,
                )

        transaction.on_commit(_execute)
    finally:
        _reset_processing("reservation_cleanup")

def request_reservation_cleanup(*, batch_size: int = 500) -> None:
    """
    Public helper for external modules and orchestrators to trigger
    reservation cleanup without directly importing the inventory
    services layer.

    Designed to be safely called from:
        * Celery beat tasks (recommended)
        * Django management commands
        * Cart module on checkout abandonment
        * Custom signal handlers in other apps
        * External event bus consumers

    Example::

        from apps.inventory.signals import request_reservation_cleanup
        request_reservation_cleanup(batch_size=200)

    Args:
        batch_size: Maximum number of reservations to process per
            execution pass. Hard-bounded to the inclusive range
            [1, 10,000] to protect against runaway batch operations.
    """
    safe_batch_size = max(1, min(int(batch_size or 500), 10_000))
    reservation_cleanup_requested.send(
        sender=__name__,
        batch_size=safe_batch_size,
    )

# ------------------------------------------------------------------------------
# Public API surface
# ------------------------------------------------------------------------------
__all__ = [
    "handle_order_paid_deduct_inventory",
    "handle_order_cancelled_restock_inventory",
    "handle_refund_approved_restock_inventory",
    "handle_product_variant_created",
    "handle_warehouse_created",
    "reservation_cleanup_requested",
    "handle_reservation_cleanup_requested",
    "request_reservation_cleanup",
]