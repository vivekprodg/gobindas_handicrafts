"""
Event-driven signal handlers for the Inventory application.

Coordinates event reactions across orders, refunds, catalog variants,
warehouses, and reservation cleanup tasks safely.
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

_state_lock = threading.local()


def _is_processing(flag: str) -> bool:
    return bool(getattr(_state_lock, flag, False))

def _set_processing(flag: str, value: bool = True) -> None:
    setattr(_state_lock, flag, value)

def _reset_processing(flag: str) -> None:
    if hasattr(_state_lock, flag):
        delattr(_state_lock, flag)

def _get_model(app_label: str, model_name: str) -> Any:
    return apps.get_model(app_label, model_name)

def _inventory_services() -> dict:
    from apps.inventory.services import deduct_stock, release_expired_reservations, restock
    return {
        "deduct_stock": deduct_stock,
        "restock": restock,
        "release_expired_reservations": release_expired_reservations,
    }

# ==============================================================================
# IDEMPOTENCY GUARDS
# ==============================================================================
def _order_has_sale_transactions(order_id: Any) -> bool:
    from apps.inventory.models import InventoryTransaction
    return InventoryTransaction.objects.filter(
        reference_model="orders.Order",
        reference_id=str(order_id),
        transaction_type=InventoryTransaction.TransactionType.SALE,
    ).exists()

def _order_has_restock_transactions(order_id: Any) -> bool:
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
    from apps.inventory.models import InventoryTransaction
    return InventoryTransaction.objects.filter(
        reference_model="orders.Refund",
        reference_id=str(refund_id),
        transaction_type=InventoryTransaction.TransactionType.RETURN,
    ).exists()

def _resolve_order_item_targets(order_item: Any) -> tuple:
    product = getattr(order_item, "product", None)
    variant = getattr(order_item, "variant", None)
    return (None, variant) if variant is not None else (product, None)

# ==============================================================================
# RECEIVERS
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
    if instance.payment_status != "paid":
        return
    if update_fields is not None and "payment_status" not in update_fields:
        return
    if instance.status in ("cancelled", "refunded"):
        return
    if _is_processing("order_paid_deduct_inventory"):
        return
    if _order_has_sale_transactions(instance.pk):
        return

    _set_processing("order_paid_deduct_inventory")
    try:
        order_id, order_number = instance.pk, instance.order_number
        def _execute() -> None:
            with transaction.atomic():
                _deduct_inventory_for_order(order_id, order_number)
        transaction.on_commit(_execute)
    finally:
        _reset_processing("order_paid_deduct_inventory")

def _deduct_inventory_for_order(order_id: Any, order_number: str) -> None:
    Order = _get_model("orders", "Order")
    try:
        order = Order.objects.select_related("customer").only("id", "order_number", "customer_id", "status", "payment_status").get(pk=order_id)
    except Order.DoesNotExist:
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
            services["deduct_stock"](
                quantity=item.quantity,
                product=target_product,
                product_variant=target_variant,
                reference_number=order_number,
                reference_model="orders.Order",
                reference_id=str(order_id),
                remarks=_("Order %(order)s paid; stock deducted.") % {"order": order_number},
            )
        except Exception as exc:
            logger.error("Failed to deduct stock for order item %s: %s", item.pk, exc, exc_info=True)

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
    if instance.status != "cancelled":
        return
    if update_fields is not None and "status" not in update_fields:
        return
    if _is_processing("order_cancelled_restock_inventory"):
        return
    if _order_has_restock_transactions(instance.pk) or not _order_has_sale_transactions(instance.pk):
        return

    _set_processing("order_cancelled_restock_inventory")
    try:
        order_id, order_number = instance.pk, instance.order_number
        def _execute() -> None:
            with transaction.atomic():
                _restock_inventory_for_order(order_id, order_number)
        transaction.on_commit(_execute)
    finally:
        _reset_processing("order_cancelled_restock_inventory")

def _restock_inventory_for_order(order_id: Any, order_number: str) -> None:
    Order = _get_model("orders", "Order")
    try:
        order = Order.objects.only("id", "order_number").get(pk=order_id)
    except Order.DoesNotExist:
        return

    items = order.items.select_related("product", "variant").all()
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
                remarks=_("Order %(order)s cancelled; stock restored.") % {"order": order_number},
            )
        except Exception as exc:
            logger.error("Failed to restock for order item %s: %s", item.pk, exc, exc_info=True)

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
    if instance.status != "processed":
        return
    if update_fields is not None and "status" not in update_fields:
        return
    if _is_processing("refund_approved_restock_inventory"):
        return
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

def _restock_inventory_for_refund(refund_id: Any, order_id: Optional[Any]) -> None:
    if order_id is None:
        return
    Refund = _get_model("orders", "Refund")
    try:
        refund = Refund.objects.select_related("order").get(pk=refund_id)
    except Refund.DoesNotExist:
        return

    items = refund.order.items.select_related("product", "variant").all()
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
                remarks=_("Refund %(refund)s processed for order %(order)s; stock restored.") % {
                    "refund": refund.pk, "order": order_number
                },
            )
        except Exception as exc:
            logger.error("Failed to restock for refund item %s: %s", item.pk, exc, exc_info=True)

@receiver(
    post_save,
    sender="catalog.ProductVariant",
    dispatch_uid="inventory_signal_product_variant_created",
)
def handle_product_variant_created(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    if not created or _is_processing("product_variant_created") or not getattr(instance, "is_active", True):
        return

    _set_processing("product_variant_created")
    try:
        variant_id = instance.pk
        def _execute() -> None:
            with transaction.atomic():
                _create_inventory_for_new_variant(variant_id)
        transaction.on_commit(_execute)
    finally:
        _reset_processing("product_variant_created")

def _create_inventory_for_new_variant(variant_id: Any) -> None:
    from apps.inventory.models import Inventory, Warehouse
    ProductVariant = _get_model("catalog", "ProductVariant")
    try:
        variant = ProductVariant.objects.only("id", "is_active").get(pk=variant_id)
    except ProductVariant.DoesNotExist:
        return

    if not variant.is_active:
        return

    warehouses = list(Warehouse.objects.filter(is_active=True).only("id"))
    records = [
        Inventory(
            warehouse=wh,
            product_variant=variant,
            product=None,
            available_quantity=Decimal("0.00"),
            reserved_quantity=Decimal("0.00"),
            damaged_quantity=Decimal("0.00"),
            incoming_quantity=Decimal("0.00"),
            is_active=True,
        )
        for wh in warehouses
    ]
    if records:
        Inventory.objects.bulk_create(records, ignore_conflicts=True)

@receiver(
    post_save,
    sender="inventory.Warehouse",
    dispatch_uid="inventory_signal_warehouse_created",
)
def handle_warehouse_created(sender: Any, instance: Any, created: bool, **kwargs: Any) -> None:
    if not created or not getattr(instance, "is_active", True) or _is_processing("warehouse_created"):
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
    from apps.inventory.models import Inventory, Warehouse
    try:
        warehouse = Warehouse.objects.only("id", "is_active").get(pk=warehouse_id)
    except Warehouse.DoesNotExist:
        return

    if not warehouse.is_active:
        return

    ProductVariant = _get_model("catalog", "ProductVariant")
    existing_vids = set(
        Inventory.objects.filter(warehouse=warehouse, product_variant__isnull=False).values_list("product_variant_id", flat=True)
    )

    batch_size = 500
    pending_batch = []
    variant_ids = ProductVariant.objects.filter(is_active=True).order_by("id").values_list("id", flat=True)

    for vid in variant_ids.iterator(chunk_size=batch_size):
        if vid in existing_vids:
            continue
        pending_batch.append(
            Inventory(
                warehouse=warehouse,
                product_variant_id=vid,
                product=None,
                available_quantity=Decimal("0.00"),
                reserved_quantity=Decimal("0.00"),
                damaged_quantity=Decimal("0.00"),
                incoming_quantity=Decimal("0.00"),
                is_active=True,
            )
        )
        if len(pending_batch) >= batch_size:
            Inventory.objects.bulk_create(pending_batch, ignore_conflicts=True)
            pending_batch = []

    if pending_batch:
        Inventory.objects.bulk_create(pending_batch, ignore_conflicts=True)

reservation_cleanup_requested = Signal()

@receiver(reservation_cleanup_requested, dispatch_uid="inventory_signal_reservation_cleanup")
def handle_reservation_cleanup_requested(sender: Any, *, batch_size: int = 500, **kwargs: Any) -> None:
    if _is_processing("reservation_cleanup"):
        return

    _set_processing("reservation_cleanup")
    try:
        safe_batch_size = max(1, min(int(batch_size or 500), 10000))
        def _execute() -> None:
            try:
                services = _inventory_services()
                services["release_expired_reservations"](batch_size=safe_batch_size)
            except Exception as exc:
                logger.error("Error during reservation cleanup: %s", exc, exc_info=True)
        transaction.on_commit(_execute)
    finally:
        _reset_processing("reservation_cleanup")

def request_reservation_cleanup(*, batch_size: int = 500) -> None:
    safe_batch_size = max(1, min(int(batch_size or 500), 10000))
    reservation_cleanup_requested.send(sender=__name__, batch_size=safe_batch_size)

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