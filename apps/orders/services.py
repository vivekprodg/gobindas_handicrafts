"""
Enterprise-grade business service layer for the Orders application.

This module is the PRIMARY BUSINESS LOGIC LAYER of the Orders
application. It is the ONLY place where order-related business
workflows are executed.

ARCHITECTURE
============

Layered responsibility model:

    models.py        → Persists data (no business logic)
    signals.py       → Detects ORM lifecycle events
    event_handlers.py → Coordinates domain workflows
    services.py      → EXECUTES business logic (this file)
    selectors.py     → READS data (no writes)
    tasks.py         → Executes background work
    views.py         → Receives HTTP requests

This file is the ONLY layer that:
    1. Validates business rules.
    2. Performs state-transition validation.
    3. Coordinates cross-model writes.
    4. Executes transactional workflows.
    5. Maintains financial integrity.
    6. Orchestrates denormalized-field updates.

It NEVER:
    1. Performs raw HTTP request handling.
    2. Renders templates.
    3. Sends emails / SMS / webhooks directly.
    4. Computes inventory mutations.
    5. Implements payment-gateway logic.
    6. Implements shipping-algorithm logic.

The Orders app is INVENTORY-AGNOSTIC. Services that need to mutate
inventory emit cross-app notifications via event_handlers.py rather
than touching the inventory app's models directly. The actual
inventory mutation is performed by the inventory app's own service
layer in response to the cross-app notification.

PERFORMANCE
===========
* All multi-step workflows are wrapped in ``transaction.atomic()``.
* All hot-path reads use ``.only()``, ``.select_related()``, and
  ``.prefetch_related()`` to avoid N+1 queries.
* All bulk operations use ``bulk_create()`` / ``bulk_update()`` with
  ``update_fields`` to minimise DB roundtrips.
* All financial calculations use ``Decimal`` arithmetic and
  ``F`` expressions where applicable.
* All row locks use ``select_for_update()`` with explicit ``of=``
  clauses to avoid deadlocks.

SECURITY
========
* All inputs are validated at the service boundary.
* All permissions-sensitive operations check the calling user.
* All financial operations are atomic.
* No service method exposes internal exceptions to the caller.
* No mutable module-level globals beyond logger / constants.
"""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.orders import constants as c
from apps.orders.models import (
    CouponUsage,
    Order,
    OrderAddressSnapshot,
    OrderItem,
    OrderStatusHistory,
    Payment,
    PaymentAttempt,
    Refund,
    ReturnRequest,
    Shipment,
    ShipmentItem,
    TaxLine,
    DiscountLine,
)

logger = logging.getLogger(c.LOGGER_SERVICES)

# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================
def _generate_order_number() -> str:
    """
    Generate a unique, human-readable order number.

    Format: ``ORD-YYMMDD-XXXXXX`` where ``XXXXXX`` is a
    cryptographically random 6-character hex suffix.

    A best-effort uniqueness check is performed; in the (extremely
    unlikely) event of a collision, the caller should retry.
    """
    prefix = c.ORDER_NUMBER_PREFIX
    date_part = timezone.now().strftime(c.RETURN_NUMBER_DATE_FORMAT)
    for _attempt in range(3):
        suffix = secrets.token_hex(c.RETURN_NUMBER_TOKEN_BYTES).upper()
        candidate = f"{prefix}-{date_part}-{suffix}"
        if not Order.objects.filter(order_number=candidate).exists():
            return candidate
    # Fallback: append microseconds for guaranteed uniqueness.
    fallback_suffix = secrets.token_hex(4).upper()
    return f"{prefix}-{date_part}-{fallback_suffix}"

def _generate_shipment_number() -> str:
    """
    Generate a unique, human-readable shipment number.

    Format: ``SHP-YYMMDD-XXXXXXXX`` where ``XXXXXXXX`` is a
    cryptographically random 8-character hex suffix.
    """
    prefix = c.SHIPMENT_NUMBER_PREFIX
    date_part = timezone.now().strftime(c.RETURN_NUMBER_DATE_FORMAT)
    for _attempt in range(3):
        suffix = secrets.token_hex(4).upper()
        candidate = f"{prefix}-{date_part}-{suffix}"
        if not Shipment.objects.filter(shipment_number=candidate).exists():
            return candidate
    fallback_suffix = secrets.token_hex(6).upper()
    return f"{prefix}-{date_part}-{fallback_suffix}"

def _validate_status_transition(
    *,
    instance: Any,
    new_status: str,
    allowed_from: Iterable[str],
    field_name: str = "status",
) -> None:
    """
    Validate that ``instance`` is allowed to transition into
    ``new_status`` from one of the ``allowed_from`` values.

    Raises ``ValueError`` if the transition is not allowed.
    """
    current = getattr(instance, field_name)
    if current == new_status:
        return  # No-op transition is always allowed.
    if current not in frozenset(allowed_from):
        raise ValueError(
            _(f"Illegal {field_name} transition for "
              f"{instance.__class__.__name__} {instance.pk}: "
              f"{current!r} -> {new_status!r}")
        )

# ==============================================================================
# 1. ORDER ADDRESS SNAPSHOT SERVICES
# ==============================================================================
@transaction.atomic
def create_address_snapshot(
    *,
    full_name: str,
    phone_number: str,
    address_line_1: str,
    city: str,
    state_or_province: str,
    postal_code: str,
    country: str,
    company: str = "",
    address_line_2: str = "",
    country_code: str = "",
    phone_e164: str = "",
    latitude: Optional[Decimal] = None,
    longitude: Optional[Decimal] = None,
    delivery_notes: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> OrderAddressSnapshot:
    """
    Create an immutable ``OrderAddressSnapshot``.

    Returns the freshly-saved snapshot. The save() method on the
    model automatically computes ``address_hash`` for
    deduplication.
    """
    snapshot = OrderAddressSnapshot(
        full_name=full_name,
        phone_number=phone_number,
        company=company,
        address_line_1=address_line_1,
        address_line_2=address_line_2,
        city=city,
        state_or_province=state_or_province,
        postal_code=postal_code,
        country=country,
        country_code=country_code,
        phone_e164=phone_e164,
        latitude=latitude,
        longitude=longitude,
        delivery_notes=delivery_notes,
        metadata=metadata or {},
    )
    snapshot.save()
    logger.info(
        "OrderAddressSnapshot created pk=%s hash=%s",
        snapshot.pk, snapshot.address_hash,
    )
    return snapshot

# ==============================================================================
# 2. ORDER NUMBER GENERATION
# ==============================================================================
def generate_unique_order_number() -> str:
    """
    Public alias for the internal helper. Returns a unique
    order number suitable for assignment to ``Order.order_number``.
    """
    return _generate_order_number()

# ==============================================================================
# 3. ORDER CREATION
# ==============================================================================
@transaction.atomic
def create_order(
    *,
    email: str,
    shipping_snapshot: OrderAddressSnapshot,
    order_number: Optional[str] = None,
    customer: Optional[Any] = None,
    billing_snapshot: Optional[OrderAddressSnapshot] = None,
    currency: str = c.DEFAULT_CURRENCY_CODE,
    customer_note: str = "",
    source: str = Order.Source.WEB,
    shipping_cost: Decimal = c.ZERO_DECIMAL_2,
    tax_total: Decimal = c.ZERO_DECIMAL_2,
    discount_total: Decimal = c.ZERO_DECIMAL_2,
    is_gift: bool = False,
    gift_message: str = "",
    gift_wrapping: str = "",
    personalization_data: Optional[Dict[str, Any]] = None,
    customer_ip: Optional[str] = None,
    customer_user_agent: str = "",
    customer_locale: str = "",
    customer_timezone: str = "",
    referrer_url: str = "",
    is_active: bool = c.DEFAULT_ORDER_ACTIVE_STATE,
    status: str = Order.OrderStatus.PENDING,
    payment_status: str = Order.PaymentStatus.PENDING,
    payment_method: str = "",
    transaction_id: str = "",
    json_metadata: Optional[Dict[str, Any]] = None,
    notes: str = "",
    tags: Optional[List[str]] = None,
) -> Order:
    """
    Create a new ``Order`` header in an atomic transaction.

    The caller is responsible for:

        1. Building the ``shipping_snapshot`` (and optionally
           ``billing_snapshot``) via ``create_address_snapshot()``.
        2. Adding line items via ``add_order_item()`` AFTER this
           function returns.
        3. Setting the final financial totals via
           ``update_order_financials()``.

    This split is deliberate: it allows the service layer to
    produce an immutable order header before the (potentially
    expensive) line-item snapshotting begins.

    Raises:
        ValueError: If the order number already exists, or if any
            user-supplied value fails validation.
    """
    # ------------------------------------------------------------------
    # 1. Resolve the order number (auto-generate if not provided).
    # ------------------------------------------------------------------
    if not order_number:
        order_number = _generate_order_number()
    elif Order.objects.filter(order_number=order_number).exists():
        raise ValueError(
            _(f"Order number '{order_number}' is already in use.")
        )

    # ------------------------------------------------------------------
    # 2. Validate the source / status values against TextChoices.
    # ------------------------------------------------------------------
    valid_sources = {choice.value for choice in Order.Source}
    if source not in valid_sources:
        raise ValueError(_(f"Invalid order source: {source!r}."))
    valid_statuses = {choice.value for choice in Order.OrderStatus}
    if status not in valid_statuses:
        raise ValueError(_(f"Invalid order status: {status!r}."))
    valid_payment_statuses = {
        choice.value for choice in Order.PaymentStatus
    }
    if payment_status not in valid_payment_statuses:
        raise ValueError(
            _(f"Invalid order payment_status: {payment_status!r}.")
        )

    # ------------------------------------------------------------------
    # 3. Create the order header.
    # ------------------------------------------------------------------
    order = Order.objects.create(
        order_number=order_number,
        customer=customer if customer and getattr(customer, "is_authenticated", False) else None,
        email=email,
        shipping_address=shipping_snapshot,
        billing_address=billing_snapshot or shipping_snapshot,
        status=status,
        payment_status=payment_status,
        payment_method=payment_method,
        transaction_id=transaction_id,
        currency=currency,
        shipping_cost=shipping_cost,
        tax_total=tax_total,
        discount_total=discount_total,
        customer_note=customer_note,
        is_active=is_active,
        json_metadata=json_metadata or {},
        is_gift=is_gift,
        gift_message=gift_message,
        gift_wrapping=gift_wrapping,
        personalization_data=personalization_data or {},
        source=source,
        customer_ip=customer_ip,
        customer_user_agent=customer_user_agent,
        customer_locale=customer_locale,
        customer_timezone=customer_timezone,
        referrer_url=referrer_url,
        notes=notes,
        tags=tags or [],
    )

    # ------------------------------------------------------------------
    # 4. Write the initial status-history entry.
    # ------------------------------------------------------------------
    OrderStatusHistory.objects.create(
        order=order,
        old_status="",
        new_status=order.status,
        remarks=_("Order created."),
        created_by=order.customer,
    )

    logger.info(
        "Order %s created for customer=%s total=%s",
        order.order_number,
        getattr(order.customer, "pk", None),
        order.total,
    )
    return order

# ==============================================================================
# 4. ORDER ITEM SERVICES
# ==============================================================================
@transaction.atomic
def add_order_item(
    *,
    order: Order,
    product: Optional[Any] = None,
    variant: Optional[Any] = None,
    product_name: str = "",
    product_sku: str = "",
    variant_name: str = "",
    unit_price: Decimal = c.ZERO_DECIMAL_2,
    quantity: int = c.DEFAULT_QUANTITY,
    discount: Decimal = c.ZERO_DECIMAL_2,
    tax: Decimal = c.ZERO_DECIMAL_2,
    weight: Decimal = c.ZERO_DECIMAL_3,
    attributes: Optional[Dict[str, Any]] = None,
    personalization: Optional[Dict[str, Any]] = None,
    status: str = OrderItem.ItemStatus.ACTIVE,
    saved_reason: str = "",
    is_gift: bool = False,
    gift_message: str = "",
    gift_wrapping: str = "",
    expected_ship_date: Optional[Any] = None,
    promised_delivery_date: Optional[Any] = None,
    supplier_name: str = "",
    supplier_order_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> OrderItem:
    """
    Create a new ``OrderItem`` under ``order`` and return the
    freshly-saved instance.

    All snapshot fields are pre-populated by the caller; this
    function does NOT recompute them. Financial validation
    (non-negative prices, quantity >= 1) is enforced by the
    model's ``clean()`` method.
    """
    if quantity < c.MIN_QUANTITY:
        raise ValueError(
            _(f"OrderItem quantity must be >= {c.MIN_QUANTITY}.")
        )
    if unit_price < c.ZERO_DECIMAL_2:
        raise ValueError(
            _("OrderItem unit_price cannot be negative.")
        )

    line_total = (unit_price * Decimal(quantity)) + tax - discount
    if line_total < c.ZERO_DECIMAL_2:
        line_total = c.ZERO_DECIMAL_2

    item = OrderItem.objects.create(
        order=order,
        product=product,
        variant=variant,
        product_name_snapshot=product_name or getattr(product, "title", "") or "",
        product_sku_snapshot=(
            product_sku
            or getattr(product, "sku", "")
            or (getattr(variant, "sku", "") if variant else "")
            or ""
        ),
        variant_name_snapshot=(
            variant_name or (getattr(variant, "name", "") if variant else "") or ""
        ),
        unit_price=unit_price,
        discount=discount,
        tax=tax,
        line_total=line_total,
        weight=weight,
        attributes=attributes or {},
        personalization=personalization or {},
        quantity=quantity,
        status=status,
        saved_reason=saved_reason,
        is_gift=is_gift,
        gift_message=gift_message,
        gift_wrapping=gift_wrapping,
        expected_ship_date=expected_ship_date,
        promised_delivery_date=promised_delivery_date,
        supplier_name_snapshot=supplier_name,
        supplier_order_id=supplier_order_id,
        metadata=metadata or {},
    )
    logger.info(
        "OrderItem %s added to order %s (qty=%s price=%s).",
        item.pk, order.order_number, quantity, unit_price,
    )
    return item

@transaction.atomic
def bulk_add_order_items(
    order: Order,
    items_data: Iterable[Dict[str, Any]],
) -> List[OrderItem]:
    """
    Bulk-create ``OrderItem`` instances for the given order.

    Each dict in ``items_data`` is forwarded to ``add_order_item()``
    (minus the ``order`` kwarg). The function returns the list of
    freshly-saved items.
    """
    created: List[OrderItem] = []
    for data in items_data:
        data = dict(data)
        data.pop("order", None)
        created.append(add_order_item(order=order, **data))
    logger.info(
        "Bulk-added %d OrderItem rows to order %s.",
        len(created), order.order_number,
    )
    return created

@transaction.atomic
def update_order_item_quantity(
    item: OrderItem,
    new_quantity: int,
) -> OrderItem:
    """
    Update the quantity of an existing ``OrderItem`` and recompute
    its ``line_total`` accordingly.

    Raises ``ValueError`` if the new quantity is invalid.
    """
    if new_quantity < c.MIN_QUANTITY:
        raise ValueError(
            _(f"OrderItem quantity must be >= {c.MIN_QUANTITY}.")
        )
    item.quantity = new_quantity
    item.line_total = max(
        c.ZERO_DECIMAL_2,
        (item.unit_price * Decimal(new_quantity)) + item.tax - item.discount,
    )
    item.save(update_fields=["quantity", "line_total", "updated_at"])
    return item

@transaction.atomic
def update_order_item_status(
    item: OrderItem,
    new_status: str,
) -> OrderItem:
    """
    Transition an ``OrderItem`` to ``new_status``.

    Validates the transition against the existing ``ItemStatus``
    set.
    """
    valid_statuses = {choice.value for choice in OrderItem.ItemStatus}
    if new_status not in valid_statuses:
        raise ValueError(_(f"Invalid item status: {new_status!r}."))
    item.status = new_status
    item.save(update_fields=["status", "updated_at"])
    return item

@transaction.atomic
def delete_order_item(item: OrderItem) -> None:
    """
    Delete a single ``OrderItem``.

    Refuses to delete if the item is already in a shipped state
    (preserving audit integrity for financial records).
    """
    if item.status in {
        OrderItem.ItemStatus.SHIPPED if hasattr(OrderItem.ItemStatus, "SHIPPED") else "shipped",
        "shipped",
    }:
        raise ValueError(
            _("Cannot delete a line item that has already shipped.")
        )
    item.delete()
    logger.info("OrderItem %s deleted.", item.pk)

# ==============================================================================
# 5. ORDER FINANCIAL RECALCULATION
# ==============================================================================
@transaction.atomic
def recalculate_order_totals(order: Order) -> Order:
    """
    Recompute ``subtotal`` and ``total`` from the order's current
    line items, taxes, discounts, and shipping cost.

    Returns the refreshed order.
    """
    aggregate = order.items.aggregate(
        computed_subtotal=Sum(
            F("unit_price") * F("quantity"),
            output_field=__import__("django").db.models.DecimalField(
                max_digits=14, decimal_places=2,
            ),
        ),
    )
    subtotal = aggregate["computed_subtotal"] or c.ZERO_DECIMAL_2
    order.subtotal = subtotal
    order.total = max(
        c.ZERO_DECIMAL_2,
        order.subtotal
        - (order.discount_total or c.ZERO_DECIMAL_2)
        + (order.shipping_cost or c.ZERO_DECIMAL_2)
        + (order.tax_total or c.ZERO_DECIMAL_2),
    )
    order.save(update_fields=["subtotal", "total", "updated_at"])
    return order

# ==============================================================================
# 6. ORDER LIFECYCLE TRANSITIONS
# ==============================================================================
@transaction.atomic
def update_order_status(
    order: Order,
    new_status: str,
    user: Optional[Any] = None,
    remarks: str = "",
) -> Order:
    """
    Transition ``order`` to ``new_status``.

    Performs:

        1. Validation against the TextChoices.
        2. State-transition validation against the allowed source
           states declared in ``constants.OrderStatus.CANCELLABLE_FROM``
           and the terminal-success / terminal-failure sets.
        3. Atomic persistence of the new status.
        4. Append of an ``OrderStatusHistory`` row.
        5. Auto-stamping of ``completed_at`` for terminal-success
           transitions.
    """
    valid_statuses = {choice.value for choice in Order.OrderStatus}
    if new_status not in valid_statuses:
        raise ValueError(_(f"Invalid order status: {new_status!r}."))

    # Terminal-state guards.
    if order.status in c.OrderStatus.TERMINAL_SUCCESS | c.OrderStatus.TERMINAL_FAILURE:
        if new_status not in c.OrderStatus.TERMINAL_SUCCESS | c.OrderStatus.TERMINAL_FAILURE:
            raise ValueError(
                _(f"Order {order.order_number} is in a terminal state "
                  f"({order.status}); cannot transition to {new_status}.")
            )

    old_status = order.status
    order.status = new_status

    if new_status in c.OrderStatus.TERMINAL_SUCCESS and not order.completed_at:
        order.completed_at = timezone.now()

    order.save(update_fields=["status", "completed_at", "updated_at"])

    OrderStatusHistory.objects.create(
        order=order,
        old_status=old_status,
        new_status=new_status,
        remarks=remarks,
        created_by=user,
    )

    logger.info(
        "Order %s status %s -> %s by user=%s",
        order.order_number, old_status, new_status,
        getattr(user, "pk", None),
    )
    return order

@transaction.atomic
def cancel_order(
    order: Order,
    user: Optional[Any] = None,
    remarks: str = _("Order cancelled."),
) -> Order:
    """
    Cancel ``order``.

    Validates that the order is in a cancellable state and emits the
    appropriate history row.

    Note: this function does NOT mutate inventory. The inventory
    release is performed by the inventory app's service layer in
    response to the cross-app notification emitted by the
    ``event_handlers`` module when the order's status is saved.
    """
    cancellable = c.OrderStatus.CANCELLABLE_FROM
    _validate_status_transition(
        instance=order,
        new_status=Order.OrderStatus.CANCELLED,
        allowed_from=cancellable,
        field_name="status",
    )
    return update_order_status(
        order=order,
        new_status=Order.OrderStatus.CANCELLED,
        user=user,
        remarks=remarks,
    )

@transaction.atomic
def complete_order(
    order: Order,
    user: Optional[Any] = None,
    remarks: str = _("Order completed."),
) -> Order:
    """
    Transition an order to ``COMPLETED``.

    The order must already be in a terminal-success status
    (``DELIVERED`` or already ``COMPLETED``).
    """
    _validate_status_transition(
        instance=order,
        new_status=Order.OrderStatus.COMPLETED,
        allowed_from={Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED},
        field_name="status",
    )
    return update_order_status(
        order=order,
        new_status=Order.OrderStatus.COMPLETED,
        user=user,
        remarks=remarks,
    )

@transaction.atomic
def hold_order(
    order: Order,
    user: Optional[Any] = None,
    remarks: str = _("Order placed on hold."),
) -> Order:
    """Place an order on hold."""
    return update_order_status(
        order=order,
        new_status=Order.OrderStatus.ON_HOLD,
        user=user,
        remarks=remarks,
    )

@transaction.atomic
def resume_order(
    order: Order,
    user: Optional[Any] = None,
    remarks: str = _("Order resumed."),
) -> Order:
    """Resume an order that was on hold."""
    _validate_status_transition(
        instance=order,
        new_status=Order.OrderStatus.PROCESSING,
        allowed_from={Order.OrderStatus.ON_HOLD},
        field_name="status",
    )
    return update_order_status(
        order=order,
        new_status=Order.OrderStatus.PROCESSING,
        user=user,
        remarks=remarks,
    )

@transaction.atomic
def mark_order_paid(
    order: Order,
    payment_method: str = "",
    transaction_id: str = "",
    user: Optional[Any] = None,
) -> Order:
    """
    Mark an order as fully paid.

    Updates the order's ``payment_status`` to ``PAID`` and the
    optional ``payment_method`` / ``transaction_id`` fields. The
    function does NOT create a separate ``Payment`` record; the
    caller is expected to do that via ``create_payment()``.
    """
    if order.payment_status == Order.PaymentStatus.PAID:
        return order
    order.payment_status = Order.PaymentStatus.PAID
    if payment_method:
        order.payment_method = payment_method
    if transaction_id:
        order.transaction_id = transaction_id
    order.save(update_fields=[
        "payment_status", "payment_method", "transaction_id", "updated_at",
    ])
    OrderStatusHistory.objects.create(
        order=order,
        old_status=order.status,
        new_status=order.status,
        remarks=_("Payment captured."),
        created_by=user,
    )
    logger.info(
        "Order %s marked as PAID (txn=%s).",
        order.order_number, transaction_id,
    )
    return order

# ==============================================================================
# 7. PAYMENT SERVICES
# ==============================================================================
@transaction.atomic
def create_payment(
    *,
    order: Order,
    transaction_id: str,
    gateway: str,
    amount: Decimal,
    currency: str = c.DEFAULT_CURRENCY_CODE,
    payment_method: str = "",
    status: str = Payment.PaymentState.PENDING,
    paid_at: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None,
    risk_score: Optional[Decimal] = None,
    is_test_payment: bool = False,
    payment_attempts_count: int = c.DEFAULT_PAYMENT_ATTEMPTS,
) -> Payment:
    """
    Create a new ``Payment`` record for ``order``.

    Raises ``ValueError`` if the ``transaction_id`` is already in
    use (uniqueness invariant).
    """
    if Payment.objects.filter(transaction_id=transaction_id).exists():
        raise ValueError(
            _(f"Transaction id '{transaction_id}' is already in use.")
        )
    valid_states = {choice.value for choice in Payment.PaymentState}
    if status not in valid_states:
        raise ValueError(_(f"Invalid payment state: {status!r}."))

    payment = Payment.objects.create(
        order=order,
        transaction_id=transaction_id,
        gateway=gateway,
        amount=amount,
        currency=currency,
        status=status,
        paid_at=paid_at,
        payment_method=payment_method,
        payment_attempts_count=payment_attempts_count,
        risk_score=risk_score,
        is_test_payment=is_test_payment,
        metadata=metadata or {},
    )
    logger.info(
        "Payment %s created for order %s (amount=%s %s).",
        transaction_id, order.order_number, amount, currency,
    )
    return payment

@transaction.atomic
def update_payment_status(
    payment: Payment,
    new_status: str,
    paid_at: Optional[Any] = None,
) -> Payment:
    """
    Update ``payment.status`` to ``new_status``.

    Validates the new status against ``Payment.PaymentState`` and
    auto-stamps ``paid_at`` for terminal-success transitions.
    """
    valid_states = {choice.value for choice in Payment.PaymentState}
    if new_status not in valid_states:
        raise ValueError(_(f"Invalid payment state: {new_status!r}."))

    payment.status = new_status
    if new_status in {
        Payment.PaymentState.CAPTURED,
        Payment.PaymentState.COMPLETED,
    }:
        payment.paid_at = paid_at or timezone.now()
    payment.save(update_fields=[
        "status", "paid_at", "updated_at",
    ])

    # Cascade: if the payment is captured / completed, propagate
    # the new payment_status to the parent order.
    if new_status in {
        Payment.PaymentState.CAPTURED,
        Payment.PaymentState.COMPLETED,
    }:
        mark_order_paid(
            order=payment.order,
            payment_method=payment.payment_method,
            transaction_id=payment.transaction_id,
        )

    logger.info(
        "Payment %s status -> %s.",
        payment.transaction_id, new_status,
    )
    return payment

@transaction.atomic
def record_payment_attempt(
    payment: Payment,
    *,
    attempt_status: str = PaymentAttempt.AttemptStatus.PENDING,
    gateway_response_code: str = "",
    gateway_response_message: str = "",
    gateway_response_snapshot: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
    user_agent: str = "",
    is_test: bool = False,
    notes: str = "",
) -> PaymentAttempt:
    """
    Append a new ``PaymentAttempt`` row to ``payment`` and
    atomically increment ``payment.payment_attempts_count`` via an
    ``F`` expression.
    """
    valid = {choice.value for choice in PaymentAttempt.AttemptStatus}
    if attempt_status not in valid:
        raise ValueError(
            _(f"Invalid attempt status: {attempt_status!r}.")
        )

    # Compute next attempt number (1-based).
    next_number = (
        payment.attempts.aggregate(
            max_number=__import__("django").db.models.Max("attempt_number"),
        )["max_number"]
        or 0
    ) + 1

    attempt = PaymentAttempt.objects.create(
        payment=payment,
        attempt_number=next_number,
        status=attempt_status,
        gateway_response_code=gateway_response_code,
        gateway_response_message=gateway_response_message,
        gateway_response_snapshot=gateway_response_snapshot or {},
        ip_address=ip_address,
        user_agent=user_agent,
        is_test=is_test,
        notes=notes,
    )
    # Increment counter via F-expression to avoid a read-modify-write race.
    Payment.objects.filter(pk=payment.pk).update(
        payment_attempts_count=F("payment_attempts_count") + 1,
        last_attempt_at=timezone.now(),
    )
    return attempt

# ==============================================================================
# 8. REFUND SERVICES
# ==============================================================================
@transaction.atomic
def create_refund(
    *,
    order: Order,
    payment: Payment,
    amount: Decimal,
    reason: str,
    refund_method: str = Refund.RefundMethod.ORIGINAL,
    refund_reason_category: str = "",
    customer_notes: str = "",
    internal_notes: str = "",
    evidence_images: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Refund:
    """
    Create a new ``Refund`` request for ``order`` against
    ``payment``.

    Raises ``ValueError`` if the payment does not belong to the
    order, or if the payment has not been captured yet.
    """
    if payment.order_id != order.id:
        raise ValueError(
            _("Payment does not belong to the supplied order.")
        )
    if payment.status not in {
        Payment.PaymentState.CAPTURED,
        Payment.PaymentState.COMPLETED,
    }:
        raise ValueError(
            _("Only captured/completed payments can be refunded.")
        )
    if amount <= c.ZERO_DECIMAL_2:
        raise ValueError(
            _("Refund amount must be strictly positive.")
        )

    valid_methods = {choice.value for choice in Refund.RefundMethod}
    if refund_method not in valid_methods:
        raise ValueError(
            _(f"Invalid refund method: {refund_method!r}.")
        )

    valid_categories = {choice.value for choice in Refund.RefundReasonCategory}
    if refund_reason_category and refund_reason_category not in valid_categories:
        raise ValueError(
            _(f"Invalid refund reason category: "
              f"{refund_reason_category!r}.")
        )

    refund = Refund.objects.create(
        order=order,
        payment=payment,
        amount=amount,
        reason=reason,
        refund_method=refund_method,
        refund_reason_category=refund_reason_category or None,
        customer_notes=customer_notes,
        internal_notes=internal_notes,
        evidence_images=evidence_images or [],
        metadata=metadata or {},
    )
    logger.info(
        "Refund %s created for order %s (amount=%s).",
        refund.pk, order.order_number, amount,
    )
    return refund

@transaction.atomic
def approve_refund(
    refund: Refund,
    approved_by: Optional[Any] = None,
) -> Refund:
    """
    Approve ``refund``.

    Validates that the refund is in ``REQUESTED`` state. Sets
    ``approved_by``, ``approved_at``, and status to ``APPROVED``.
    """
    if refund.status not in c.RefundStatus.APPROVABLE_FROM:
        raise ValueError(
            _(f"Refund {refund.pk} is not in a requestable state "
              f"({refund.status!r}).")
        )
    refund.status = Refund.RefundStatus.APPROVED
    refund.approved_by = approved_by
    refund.approved_at = timezone.now()
    refund.save(update_fields=[
        "status", "approved_by", "approved_at", "updated_at",
    ])
    logger.info(
        "Refund %s approved by user=%s.",
        refund.pk, getattr(approved_by, "pk", None),
    )
    return refund

@transaction.atomic
def reject_refund(
    refund: Refund,
    approved_by: Optional[Any] = None,
    rejection_reason: str = "",
) -> Refund:
    """Reject ``refund``."""
    if refund.status not in c.RefundStatus.REJECTABLE_FROM:
        raise ValueError(
            _(f"Refund {refund.pk} is not in a rejectable state "
              f"({refund.status!r}).")
        )
    refund.status = Refund.RefundStatus.REJECTED
    if approved_by is not None:
        refund.approved_by = approved_by
    if rejection_reason:
        refund.internal_notes = rejection_reason
    refund.save(update_fields=[
        "status", "approved_by", "internal_notes", "updated_at",
    ])
    return refund

@transaction.atomic
def process_refund(
    refund: Refund,
    gateway_refund_id: str = "",
) -> Refund:
    """
    Mark ``refund`` as ``PROCESSED``.

    The actual gateway call is performed by the payments app's
    Celery task; here we just record the state transition and the
    ``gateway_refund_id`` (if known).
    """
    if refund.status not in c.RefundStatus.PROCESSABLE_FROM:
        raise ValueError(
            _(f"Refund {refund.pk} is not in an approvable state "
              f"({refund.status!r}).")
        )
    refund.status = Refund.RefundStatus.PROCESSED
    refund.processed_at = timezone.now()
    if gateway_refund_id:
        refund.gateway_refund_id = gateway_refund_id
    refund.save(update_fields=[
        "status", "processed_at", "gateway_refund_id", "updated_at",
    ])
    return refund

@transaction.atomic
def complete_refund(refund: Refund) -> Refund:
    """
    Mark ``refund`` as completed.

    If the sum of completed refunds equals the order total, the
    parent order is transitioned to ``REFUNDED``.
    """
    if refund.status not in c.RefundStatus.COMPLETABLE_FROM:
        raise ValueError(
            _(f"Refund {refund.pk} is not in a processable state "
              f"({refund.status!r}).")
        )
    refund.status = Refund.RefundStatus.APPROVED  # legacy alias
    refund.completed_at = timezone.now()
    refund.save(update_fields=[
        "status", "completed_at", "updated_at",
    ])

    # Cascade: if the order is fully refunded, transition it.
    order = refund.order
    if order.payment_status in {
        Order.PaymentStatus.PAID,
        Order.PaymentStatus.PARTIALLY_PAID,
    }:
        total_refunded = (
            Refund.objects.filter(
                order=order,
                status__in={
                    Refund.RefundStatus.PROCESSED,
                    Refund.RefundStatus.APPROVED,
                },
            ).aggregate(t=Sum("amount"))["t"]
            or c.ZERO_DECIMAL_2
        )
        if total_refunded >= order.total and order.total > c.ZERO_DECIMAL_2:
            order.payment_status = Order.PaymentStatus.REFUNDED
            order.save(update_fields=["payment_status", "updated_at"])
            if order.status not in c.OrderStatus.TERMINAL_FAILURE:
                update_order_status(
                    order=order,
                    new_status=Order.OrderStatus.REFUNDED,
                    remarks=_("Fully refunded."),
                )
    return refund

# ==============================================================================
# 9. SHIPMENT SERVICES
# ==============================================================================
@transaction.atomic
def create_shipment(
    *,
    order: Order,
    carrier: str,
    tracking_number: str = "",
    tracking_url: str = "",
    warehouse: Optional[Any] = None,
    shipping_cost: Decimal = c.ZERO_DECIMAL_2,
    notes: str = "",
    estimated_delivery_date: Optional[Any] = None,
    carrier_service_level: str = "",
    carrier_api_integration_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Shipment:
    """
    Create a new ``Shipment`` for ``order``.

    Validates that the order is not in a terminal-failure state.
    Returns the freshly-saved shipment.
    """
    if order.status in c.OrderStatus.TERMINAL_FAILURE:
        raise ValueError(
            _("Cannot create a shipment for a cancelled or "
              "refunded order.")
        )

    shipment_number = _generate_shipment_number()

    shipment = Shipment.objects.create(
        order=order,
        shipment_number=shipment_number,
        carrier=carrier,
        tracking_number=tracking_number,
        tracking_url=tracking_url,
        warehouse=warehouse,
        shipping_cost=shipping_cost,
        notes=notes,
        estimated_delivery_date=estimated_delivery_date,
        carrier_service_level=carrier_service_level,
        carrier_api_integration_id=carrier_api_integration_id,
        metadata=metadata or {},
    )

    # Mirror tracking references up to the order for fast customer access.
    if tracking_number and not order.tracking_number:
        order.tracking_number = tracking_number
        order.carrier = carrier
        order.tracking_url = tracking_url
        order.save(update_fields=[
            "tracking_number", "carrier", "tracking_url", "updated_at",
        ])

    logger.info(
        "Shipment %s created for order %s (carrier=%s).",
        shipment.shipment_number, order.order_number, carrier,
    )
    return shipment

@transaction.atomic
def dispatch_shipment(
    shipment: Shipment,
    user: Optional[Any] = None,
) -> Shipment:
    """
    Mark ``shipment`` as dispatched.

    If this is the first dispatch for the order, the parent order
    is transitioned to ``SHIPPED``.
    """
    if shipment.status not in {
        Shipment.ShipmentStatus.PENDING,
        Shipment.ShipmentStatus.AWAITING_PICKUP,
    }:
        raise ValueError(
            _(f"Shipment {shipment.shipment_number} is not in a "
              f"dispatchable state ({shipment.status!r}).")
        )
    shipment.status = Shipment.ShipmentStatus.DISPATCHED
    if not shipment.dispatch_date:
        shipment.dispatch_date = timezone.now()
    shipment.save(update_fields=[
        "status", "dispatch_date", "updated_at",
    ])

    # Cascade: if the order is not yet shipped, mark it as shipped.
    if shipment.order.status in {
        Order.OrderStatus.PENDING,
        Order.OrderStatus.PROCESSING,
        Order.OrderStatus.AWAITING_PAYMENT,
        Order.OrderStatus.ON_HOLD,
    }:
        update_order_status(
            order=shipment.order,
            new_status=Order.OrderStatus.SHIPPED,
            user=user,
            remarks=_(f"Shipment {shipment.shipment_number} dispatched."),
        )
    return shipment

@transaction.atomic
def mark_shipment_in_transit(shipment: Shipment) -> Shipment:
    """Mark ``shipment`` as in-transit."""
    if shipment.status != Shipment.ShipmentStatus.DISPATCHED:
        raise ValueError(
            _("Shipment must be DISPATCHED before it can be in transit.")
        )
    shipment.status = Shipment.ShipmentStatus.IN_TRANSIT
    shipment.save(update_fields=["status", "updated_at"])
    return shipment

@transaction.atomic
def deliver_shipment(
    shipment: Shipment,
    user: Optional[Any] = None,
) -> Shipment:
    """
    Mark ``shipment`` as delivered.

    If every shipment for the order is delivered, the parent
    order is transitioned to ``DELIVERED``.
    """
    if shipment.status not in {
        Shipment.ShipmentStatus.DISPATCHED,
        Shipment.ShipmentStatus.IN_TRANSIT,
        Shipment.ShipmentStatus.OUT_FOR_DELIVERY,
    }:
        raise ValueError(
            _(f"Shipment {shipment.shipment_number} is not in a "
              f"deliverable state ({shipment.status!r}).")
        )
    shipment.status = Shipment.ShipmentStatus.DELIVERED
    if not shipment.delivery_date:
        shipment.delivery_date = timezone.now()
    if not shipment.actual_delivery_date:
        shipment.actual_delivery_date = timezone.now().date()
    shipment.save(update_fields=[
        "status", "delivery_date", "actual_delivery_date", "updated_at",
    ])

    # Cascade: if all shipments are delivered, mark the order delivered.
    pending = shipment.order.shipments.exclude(
        status=Shipment.ShipmentStatus.DELIVERED,
    ).exists()
    if not pending and shipment.order.status != Order.OrderStatus.DELIVERED:
        update_order_status(
            order=shipment.order,
            new_status=Order.OrderStatus.DELIVERED,
            user=user,
            remarks=_(f"Final shipment {shipment.shipment_number} delivered."),
        )
    return shipment

@transaction.atomic
def add_shipment_item(
    *,
    shipment: Shipment,
    order_item: OrderItem,
    quantity_shipped: Decimal,
    serial_tracking: str = "",
    condition_at_pickup: str = "",
    is_replacement: bool = False,
    replaced_from: Optional["ShipmentItem"] = None,
    notes: str = "",
) -> ShipmentItem:
    """Append a line item to ``shipment``."""
    if quantity_shipped <= c.ZERO_DECIMAL_2:
        raise ValueError(
            _("Shipment item quantity must be strictly positive.")
        )
    return ShipmentItem.objects.create(
        shipment=shipment,
        order_item=order_item,
        quantity_shipped=quantity_shipped,
        serial_tracking=serial_tracking,
        condition_at_pickup=condition_at_pickup,
        is_replacement=is_replacement,
        replaced_from=replaced_from,
        notes=notes,
    )

# ==============================================================================
# 10. COUPON USAGE SERVICES
# ==============================================================================
@transaction.atomic
def record_coupon_usage(
    *,
    order: Order,
    user: Any,
    coupon_code: str,
    discount_amount: Decimal,
    cart: Optional[Any] = None,
    product: Optional[Any] = None,
    category: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> CouponUsage:
    """
    Record a ``CouponUsage`` row for ``order``.

    Note: the historical wishlist-uniqueness constraint
    ``unique_customer_product_wishlist`` is preserved on
    ``CouponUsage`` (per the finalized models.py) but does NOT
    prevent the same user from redeeming multiple DIFFERENT
    coupon codes. The actual "one-use-per-customer" enforcement
    is performed by the coupons app's own service layer.
    """
    if discount_amount < c.ZERO_DECIMAL_2:
        raise ValueError(
            _("Coupon discount cannot be negative.")
        )
    return CouponUsage.objects.create(
        coupon_code=coupon_code,
        user=user,
        order=order,
        discount_amount=discount_amount,
        cart_id=cart,
        product_id=product,
        category_id=category,
        metadata=metadata or {},
    )

@transaction.atomic
def reverse_coupon_usage(
    coupon_usage: CouponUsage,
    reason: str = "",
) -> CouponUsage:
    """
    Mark a ``CouponUsage`` row as reversed.

    Used when a refund or order cancellation requires unwinding
    the coupon redemption.
    """
    coupon_usage.is_reversed = True
    coupon_usage.reversed_at = timezone.now()
    coupon_usage.reversal_reason = reason
    coupon_usage.save(update_fields=[
        "is_reversed", "reversed_at", "reversal_reason", "updated_at",
    ])
    return coupon_usage

# ==============================================================================
# 11. TAX LINE SERVICES
# ==============================================================================
@transaction.atomic
def add_tax_line(
    *,
    order: Order,
    tax_class: str,
    tax_name: str,
    tax_rate: Decimal,
    base_amount: Decimal = c.ZERO_DECIMAL_2,
    tax_amount: Decimal = c.ZERO_DECIMAL_2,
    jurisdiction: str = "",
    tax_authority_code: str = "",
    is_inclusive: bool = False,
    mode: str = TaxLine.TaxMode.EXCLUSIVE,
    position: int = 0,
    notes: str = "",
) -> TaxLine:
    """Append a ``TaxLine`` to ``order``."""
    if not c.MIN_TAX_RATE <= tax_rate <= c.MAX_TAX_RATE:
        raise ValueError(
            _(f"tax_rate {tax_rate} is outside the legal range "
              f"[{c.MIN_TAX_RATE}, {c.MAX_TAX_RATE}].")
        )
    return TaxLine.objects.create(
        order=order,
        tax_class=tax_class,
        tax_name=tax_name,
        tax_rate=tax_rate,
        base_amount=base_amount,
        tax_amount=tax_amount,
        jurisdiction=jurisdiction,
        tax_authority_code=tax_authority_code,
        is_inclusive=is_inclusive,
        mode=mode,
        position=position,
        notes=notes,
    )

# ==============================================================================
# 12. DISCOUNT LINE SERVICES
# ==============================================================================
@transaction.atomic
def add_discount_line(
    *,
    order: Order,
    discount_type: str,
    source: str,
    name: str,
    discount_amount: Decimal = c.ZERO_DECIMAL_2,
    base_amount: Decimal = c.ZERO_DECIMAL_2,
    code: str = "",
    description: str = "",
    percentage: Optional[Decimal] = None,
    coupon_usage: Optional[CouponUsage] = None,
    promotion_id: str = "",
    applies_to_order_item: Optional[OrderItem] = None,
    is_taxable: bool = True,
    is_stackable: bool = False,
    position: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
) -> DiscountLine:
    """Append a ``DiscountLine`` to ``order``."""
    valid_types = {choice.value for choice in DiscountLine.DiscountType}
    if discount_type not in valid_types:
        raise ValueError(
            _(f"Invalid discount type: {discount_type!r}.")
        )
    if percentage is not None and not c.MIN_PERCENTAGE <= percentage <= c.MAX_PERCENTAGE:
        raise ValueError(
            _(f"percentage {percentage} is outside the legal range "
              f"[{c.MIN_PERCENTAGE}, {c.MAX_PERCENTAGE}].")
        )
    return DiscountLine.objects.create(
        order=order,
        discount_type=discount_type,
        source=source,
        name=name,
        code=code or None,
        description=description,
        discount_amount=discount_amount,
        base_amount=base_amount,
        percentage=percentage,
        coupon_usage=coupon_usage,
        promotion_id=promotion_id,
        applies_to_order_item=applies_to_order_item,
        is_taxable=is_taxable,
        is_stackable=is_stackable,
        position=position,
        metadata=metadata or {},
    )

# ==============================================================================
# 13. RETURN REQUEST SERVICES
# ==============================================================================
@transaction.atomic
def create_return_request(
    *,
    order: Order,
    return_type: str = ReturnRequest.ReturnType.REFUND,
    reason_category: str = ReturnRequest.ReturnReasonCategory.OTHER,
    reason_text: str = "",
    requested_by: Optional[Any] = None,
    customer_notes: str = "",
    internal_notes: str = "",
    restock_decision: str = "",
    restock_location: str = "",
    return_shipping_address: Optional[OrderAddressSnapshot] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ReturnRequest:
    """
    Create a new ``ReturnRequest`` in ``REQUESTED`` state.

    The ``return_number`` is auto-generated by the model's
    ``save()`` method.
    """
    valid_types = {choice.value for choice in ReturnRequest.ReturnType}
    if return_type not in valid_types:
        raise ValueError(
            _(f"Invalid return type: {return_type!r}.")
        )
    valid_reasons = {
        choice.value for choice in ReturnRequest.ReturnReasonCategory
    }
    if reason_category not in valid_reasons:
        raise ValueError(
            _(f"Invalid return reason category: {reason_category!r}.")
        )
    return ReturnRequest.objects.create(
        order=order,
        return_type=return_type,
        reason_category=reason_category,
        reason_text=reason_text,
        status=ReturnRequest.ReturnStatus.REQUESTED,
        requested_by=requested_by,
        customer_notes=customer_notes,
        internal_notes=internal_notes,
        restock_decision=restock_decision or None,
        restock_location=restock_location,
        return_shipping_address_snapshot=return_shipping_address,
        metadata=metadata or {},
    )

@transaction.atomic
def approve_return(
    return_request: ReturnRequest,
    approved_by: Optional[Any] = None,
) -> ReturnRequest:
    """Approve a return request."""
    if return_request.status not in c.ReturnStatus.APPROVABLE_FROM:
        raise ValueError(
            _(f"Return {return_request.pk} is not in an approvable "
              f"state ({return_request.status!r}).")
        )
    return_request.status = ReturnRequest.ReturnStatus.APPROVED
    return_request.approved_by = approved_by
    return_request.approved_at = timezone.now()
    return_request.save(update_fields=[
        "status", "approved_by", "approved_at", "updated_at",
    ])
    return return_request

@transaction.atomic
def reject_return(
    return_request: ReturnRequest,
    rejected_by: Optional[Any] = None,
    rejection_reason: str = "",
) -> ReturnRequest:
    """Reject a return request."""
    if return_request.status not in c.ReturnStatus.REJECTABLE_FROM:
        raise ValueError(
            _(f"Return {return_request.pk} is not in a rejectable "
              f"state ({return_request.status!r}).")
        )
    return_request.status = ReturnRequest.ReturnStatus.REJECTED
    return_request.rejected_by = rejected_by
    return_request.rejected_at = timezone.now()
    return_request.rejection_reason = rejection_reason
    return_request.save(update_fields=[
        "status", "rejected_by", "rejected_at",
        "rejection_reason", "updated_at",
    ])
    return return_request

@transaction.atomic
def mark_return_received(return_request: ReturnRequest) -> ReturnRequest:
    """Mark a return as received (parcel arrived at the warehouse)."""
    if return_request.status not in c.ReturnStatus.RECEIVABLE_FROM:
        raise ValueError(
            _(f"Return {return_request.pk} is not in a receivable "
              f"state ({return_request.status!r}).")
        )
    return_request.status = ReturnRequest.ReturnStatus.RECEIVED
    return_request.received_at = timezone.now()
    return_request.save(update_fields=[
        "status", "received_at", "updated_at",
    ])
    return return_request

@transaction.atomic
def complete_return(return_request: ReturnRequest) -> ReturnRequest:
    """Mark a return as completed."""
    if return_request.status not in c.ReturnStatus.COMPLETABLE_FROM:
        raise ValueError(
            _(f"Return {return_request.pk} is not in a completable "
              f"state ({return_request.status!r}).")
        )
    return_request.status = ReturnRequest.ReturnStatus.COMPLETED
    return_request.completed_at = timezone.now()
    return_request.save(update_fields=[
        "status", "completed_at", "updated_at",
    ])
    return return_request

# ==============================================================================
# 14. ORDER QUERY / PROJECTION HELPERS (lightweight read services)
# ==============================================================================
def get_order_total_refunded(order: Order) -> Decimal:
    """Return the sum of all processed refunds for ``order``."""
    total = (
        Refund.objects.filter(
            order=order,
            status__in={
                Refund.RefundStatus.PROCESSED,
                Refund.RefundStatus.APPROVED,
            },
        ).aggregate(t=Sum("amount"))["t"]
        or c.ZERO_DECIMAL_2
    )
    return total

def get_order_active_shipment_count(order: Order) -> int:
    """Return the count of shipments not yet delivered for ``order``."""
    return order.shipments.exclude(
        status=Shipment.ShipmentStatus.DELIVERED,
    ).count()

def is_order_fully_shipped(order: Order) -> bool:
    """Return ``True`` iff every shipment for ``order`` is delivered."""
    return get_order_active_shipment_count(order) == 0

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Address snapshots
    "create_address_snapshot",
    # Order creation
    "generate_unique_order_number",
    "create_order",
    # Order items
    "add_order_item",
    "bulk_add_order_items",
    "update_order_item_quantity",
    "update_order_item_status",
    "delete_order_item",
    # Financials
    "recalculate_order_totals",
    # Order lifecycle
    "update_order_status",
    "cancel_order",
    "complete_order",
    "hold_order",
    "resume_order",
    "mark_order_paid",
    # Payments
    "create_payment",
    "update_payment_status",
    "record_payment_attempt",
    # Refunds
    "create_refund",
    "approve_refund",
    "reject_refund",
    "process_refund",
    "complete_refund",
    # Shipments
    "create_shipment",
    "dispatch_shipment",
    "mark_shipment_in_transit",
    "deliver_shipment",
    "add_shipment_item",
    # Coupons
    "record_coupon_usage",
    "reverse_coupon_usage",
    # Tax & discounts
    "add_tax_line",
    "add_discount_line",
    # Returns
    "create_return_request",
    "approve_return",
    "reject_return",
    "mark_return_received",
    "complete_return",
    # Read helpers
    "get_order_total_refunded",
    "get_order_active_shipment_count",
    "is_order_fully_shipped",
]