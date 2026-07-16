"""
Enterprise-grade read / query layer for the Orders application.

This module is the SINGLE SOURCE OF TRUTH for all database READ
operations related to the order domain. It centralises every
optimised QuerySet, aggregate, and projection so that views, admin
classes, serializers, management commands, and Celery tasks all
read data through one consistent API.

ARCHITECTURE
============
Layered responsibility model:

    models.py        → Persists data
    signals.py       → Detects ORM lifecycle events
    event_handlers.py → Coordinates domain workflows
    services.py      → EXECUTES writes / business logic
    selectors.py     → READS data (this file — read-only)
    tasks.py         → Background work
    views.py         → HTTP request handling

This file is the ONLY layer that:
    1. Builds optimized QuerySets.
    2. Performs aggregations and annotations.
    3. Returns projections, search datasets, dashboard data.
    4. Encapsulates every ``select_related`` / ``prefetch_related``
       path used across the project.

It NEVER:
    1. Creates objects.
    2. Updates objects.
    3. Deletes objects.
    4. Triggers notifications, signals, or tasks.
    5. Validates business rules.
    6. Performs inventory / payment / shipment mutations.
    7. Imports views, admin, services, or event_handlers.

PERFORMANCE
===========
* All heavy list endpoints use ``select_related`` and
  ``prefetch_related`` to avoid N+1 queries.
* All aggregation functions use ``Coalesce`` to ensure deterministic
  ``Decimal`` results even when the underlying columns are NULL.
* All status / payment-status filters use the canonical values
  declared in ``apps.orders.constants``.
* Heavy summary functions return ``dict`` projections (not model
  instances) so they can be cached cheaply at the API layer.

SECURITY
========
* Selectors return ONLY the columns and relations declared in
  ``models.py``. No service-layer state, no plaintext secrets, no
  PII is ever injected by a selector.
* All querysets are unevaluated at the function boundary. The
  caller decides when to execute them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from django.db.models import Avg, Count, Q, QuerySet, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

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
)

# ==============================================================================
# CURRENCY / DECIMAL HELPERS
# ==============================================================================
#: Safe output factory for ``Coalesce(Sum(...), Decimal("0.00"))``.
def _coalesced_sum(field: str) -> Coalesce:
    """Return a ``Coalesce(Sum(field), Decimal("0.00"))`` expression."""
    return Coalesce(Sum(field), Decimal("0.00"))

# ==============================================================================
# BASE QUERYSETS
# ==============================================================================
def get_base_order_queryset() -> QuerySet[Order]:
    """
    Return the project-wide base ``Order`` queryset.

    This queryset applies the standard ``select_related`` joins used
    by every downstream selector so that calling code never needs
    to re-specify them.

    The queryset is returned unevaluated; callers control
    evaluation.
    """
    return Order.objects.select_related(
        "customer",
        "shipping_address",
        "billing_address",
    )

def get_detailed_order_queryset() -> QuerySet[Order]:
    """
    Return an ``Order`` queryset with deep prefetching for detail
    views (admin change view, customer "order detail" page, etc.).
    """
    return get_base_order_queryset().prefetch_related(
        "items",
        "items__product",
        "items__variant",
        "items__warehouse",
        "payments",
        "payments__attempts",
        "shipments",
        "shipments__line_items",
        "refunds",
        "status_history",
        "tax_lines",
        "discount_lines",
        "notes",
        "attachments",
        "coupon_usages",
        "timeline_events",
        "return_requests",
    )

# ==============================================================================
# 1. ORDER RETRIEVAL
# ==============================================================================
def get_order_by_uuid(
    order_id: Union[str, uuid.UUID],
) -> Optional[Order]:
    """
    Retrieve a single order by its primary-key UUID.

    Returns ``None`` if no order matches. Uses the deep-detail
    queryset so that admin / detail views can render immediately
    without triggering N+1 queries.
    """
    return get_detailed_order_queryset().filter(id=order_id).first()

def get_order_by_number(order_number: str) -> Optional[Order]:
    """
    Retrieve a single order by its human-readable ``order_number``.
    """
    return get_detailed_order_queryset().filter(
        order_number=order_number,
    ).first()

def get_order_detail(
    order_id: Union[str, uuid.UUID],
    user: Optional[Any] = None,
    scoped_to_user: bool = False,
) -> Optional[Order]:
    """
    Retrieve a fully-prefetched order.

    When ``scoped_to_user`` is ``True``, the query restricts the
    order to a specific authenticated user. This is the canonical
    way to fetch an order for display on a customer-facing page.
    """
    qs = get_detailed_order_queryset().filter(id=order_id)
    if scoped_to_user and user is not None and getattr(
        user, "is_authenticated", False
    ):
        qs = qs.filter(customer=user)
    return qs.first()

def get_orders(
    *,
    status: Optional[str] = None,
    payment_status: Optional[str] = None,
    customer: Optional[Any] = None,
    currency: Optional[str] = None,
    source: Optional[str] = None,
    is_active: Optional[bool] = None,
    is_gift: Optional[bool] = None,
    fraud_check_status: Optional[str] = None,
    min_total: Optional[Decimal] = None,
    max_total: Optional[Decimal] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
) -> QuerySet[Order]:
    """
    Compose a filtered ``Order`` queryset using any combination
    of supported criteria. All criteria are optional.

    The queryset is returned unevaluated. Apply further chaining
    (``.order_by()``, slicing, etc.) as needed.
    """
    qs = get_base_order_queryset()

    if status is not None:
        qs = qs.filter(status=status)
    if payment_status is not None:
        qs = qs.filter(payment_status=payment_status)
    if customer is not None:
        qs = qs.filter(customer=customer)
    if currency is not None:
        qs = qs.filter(currency=currency)
    if source is not None:
        qs = qs.filter(source=source)
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    if is_gift is not None:
        qs = qs.filter(is_gift=is_gift)
    if fraud_check_status is not None:
        qs = qs.filter(fraud_check_status=fraud_check_status)
    if min_total is not None:
        qs = qs.filter(total__gte=min_total)
    if max_total is not None:
        qs = qs.filter(total__lte=max_total)
    if created_after is not None:
        qs = qs.filter(created_at__gte=created_after)
    if created_before is not None:
        qs = qs.filter(created_at__lte=created_before)

    return qs

# ==============================================================================
# 2. CUSTOMER ORDER QUERIES
# ==============================================================================
def get_customer_orders(user: Any) -> QuerySet[Order]:
    """
    Return the orders for a specific authenticated customer.

    Returns an empty queryset for anonymous users so that the
    caller can chain ``.count()`` / ``.exists()`` without having
    to special-case ``None``.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return Order.objects.none()
    return get_base_order_queryset().filter(customer=user).order_by("-created_at")

def get_customer_order_count(user: Any) -> int:
    """Return the total number of orders placed by a customer."""
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    return Order.objects.filter(customer=user).count()

def get_customer_recent_orders(
    user: Any,
    limit: int = 5,
) -> QuerySet[Order]:
    """Return the most recent orders for a specific customer."""
    return get_customer_orders(user)[:limit]

def get_customer_completed_orders(user: Any) -> QuerySet[Order]:
    """
    Return the customer's orders in a terminal-success state
    (``DELIVERED`` or ``COMPLETED``).
    """
    return get_customer_orders(user).filter(
        status__in=(Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED)
    )

def get_customer_cancelled_orders(user: Any) -> QuerySet[Order]:
    """
    Return the customer's orders in a terminal-failure state
    (``CANCELLED`` or ``REFUNDED``).
    """
    return get_customer_orders(user).filter(
        status__in=(Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED)
    )

def get_customer_open_orders(user: Any) -> QuerySet[Order]:
    """
    Return the customer's orders that are NOT in a terminal
    state (still actionable).
    """
    terminal = c.OrderStatus.TERMINAL_SUCCESS | c.OrderStatus.TERMINAL_FAILURE
    return get_customer_orders(user).exclude(status__in=terminal)

# ==============================================================================
# 3. ORDER STATUS / STATE QUERIES
# ==============================================================================
def get_orders_by_status(status: str) -> QuerySet[Order]:
    """Return all orders in a specific status."""
    return get_base_order_queryset().filter(status=status).order_by("-created_at")

def get_orders_by_payment_status(payment_status: str) -> QuerySet[Order]:
    """Return all orders in a specific payment-status state."""
    return get_base_order_queryset().filter(
        payment_status=payment_status
    ).order_by("-created_at")

def get_orders_by_currency(currency: str) -> QuerySet[Order]:
    """Return all orders settled in a specific ISO 4217 currency."""
    return get_base_order_queryset().filter(currency=currency)

def get_orders_by_source(source: str) -> QuerySet[Order]:
    """Return all orders originating from a specific source channel."""
    return get_base_order_queryset().filter(source=source)

def get_pending_orders() -> QuerySet[Order]:
    """Return all orders in the ``PENDING`` state."""
    return get_orders_by_status(Order.OrderStatus.PENDING)

def get_processing_orders() -> QuerySet[Order]:
    """Return all orders in the ``PROCESSING`` state."""
    return get_orders_by_status(Order.OrderStatus.PROCESSING)

def get_shipped_orders() -> QuerySet[Order]:
    """Return all orders in the ``SHIPPED`` state."""
    return get_orders_by_status(Order.OrderStatus.SHIPPED)

def get_delivered_orders() -> QuerySet[Order]:
    """Return all orders in the ``DELIVERED`` state."""
    return get_orders_by_status(Order.OrderStatus.DELIVERED)

def get_cancelled_orders() -> QuerySet[Order]:
    """Return all orders in the ``CANCELLED`` state."""
    return get_orders_by_status(Order.OrderStatus.CANCELLED)

def get_refunded_orders() -> QuerySet[Order]:
    """Return all orders in the ``REFUNDED`` state."""
    return get_orders_by_status(Order.OrderStatus.REFUNDED)

def get_completed_orders() -> QuerySet[Order]:
    """Return all orders in the terminal-success state ``COMPLETED``."""
    return get_orders_by_status(Order.OrderStatus.COMPLETED)

def get_awaiting_payment_orders() -> QuerySet[Order]:
    """Return all orders awaiting payment capture."""
    return get_orders_by_status(Order.OrderStatus.AWAITING_PAYMENT)

def get_on_hold_orders() -> QuerySet[Order]:
    """Return all orders currently in the ``ON_HOLD`` state."""
    return get_orders_by_status(Order.OrderStatus.ON_HOLD)

def get_failed_orders() -> QuerySet[Order]:
    """Return all orders in the ``FAILED`` state."""
    return get_orders_by_status(Order.OrderStatus.FAILED)

def get_paid_orders() -> QuerySet[Order]:
    """Return all fully-paid orders."""
    return get_base_order_queryset().filter(
        payment_status=Order.PaymentStatus.PAID,
    ).order_by("-created_at")

def get_unpaid_orders() -> QuerySet[Order]:
    """Return all orders that are not yet paid."""
    return get_base_order_queryset().filter(
        payment_status__in=(
            Order.PaymentStatus.PENDING,
            Order.PaymentStatus.FAILED,
        )
    ).order_by("-created_at")

def get_gift_orders() -> QuerySet[Order]:
    """Return all orders flagged as gift orders."""
    return get_base_order_queryset().filter(is_gift=True)

def get_orders_pending_fraud_review() -> QuerySet[Order]:
    """Return all orders currently under manual fraud review."""
    return get_base_order_queryset().filter(
        fraud_check_status=Order.FraudCheckStatus.MANUAL_REVIEW,
    )

# ==============================================================================
# 4. ORDER ITEMS & HISTORY
# ==============================================================================
def get_order_items(order: Order) -> QuerySet[OrderItem]:
    """Return the line items for an order."""
    return (
        OrderItem.objects
        .select_related("product", "variant", "warehouse")
        .filter(order=order)
        .order_by("added_at")
    )

def get_order_item_by_id(item_id: int) -> Optional[OrderItem]:
    """Return a single ``OrderItem`` by primary key."""
    return (
        OrderItem.objects
        .select_related("order", "product", "variant", "warehouse")
        .filter(pk=item_id)
        .first()
    )

def get_gift_items() -> QuerySet[OrderItem]:
    """Return all line items that are flagged as gift items."""
    return OrderItem.objects.filter(is_gift=True).select_related("order")

def get_personalized_items() -> QuerySet[OrderItem]:
    """Return all line items with non-empty personalization data."""
    return (
        OrderItem.objects
        .filter(personalization__isnull=False)
        .exclude(personalization={})
        .select_related("order")
    )

def get_order_history(
    order: Order,
    *,
    limit: Optional[int] = None,
) -> QuerySet[OrderStatusHistory]:
    """Return the status transition history of an order."""
    qs = (
        OrderStatusHistory.objects
        .select_related("created_by")
        .filter(order=order)
        .order_by("-created_at")
    )
    if limit is not None:
        qs = qs[:limit]
    return qs

# ==============================================================================
# 5. SEARCH & FILTERING
# ==============================================================================
def search_orders(
    search_query: str,
    *,
    extra_filters: Optional[Q] = None,
) -> QuerySet[Order]:
    """
    Search orders by a free-text query.

    Matches against:
        * ``order_number``
        * ``email``
        * ``customer__email``
        * ``customer__first_name`` / ``customer__last_name`` / ``customer__username``
        * ``shipping_address__full_name`` / ``shipping_address__phone_number``
        * ``transaction_id``
        * ``tracking_number``
    """
    if not search_query:
        return get_base_order_queryset()

    base_q = (
        Q(order_number__icontains=search_query)
        | Q(email__icontains=search_query)
        | Q(customer__email__icontains=search_query)
        | Q(customer__first_name__icontains=search_query)
        | Q(customer__last_name__icontains=search_query)
        | Q(customer__username__icontains=search_query)
        | Q(shipping_address__full_name__icontains=search_query)
        | Q(shipping_address__phone_number__icontains=search_query)
        | Q(transaction_id__icontains=search_query)
        | Q(tracking_number__icontains=search_query)
    )
    if extra_filters is not None:
        base_q = base_q & extra_filters
    return get_base_order_queryset().filter(base_q).distinct()

# ==============================================================================
# 6. DATE / TIME WINDOWS
# ==============================================================================
def get_orders_in_date_range(
    start_date: Union[date, datetime],
    end_date: Union[date, datetime],
) -> QuerySet[Order]:
    """Return all orders whose ``created_at`` lies in ``[start, end]``."""
    return get_base_order_queryset().filter(
        created_at__range=(start_date, end_date),
    )

def get_today_orders() -> QuerySet[Order]:
    """Return all orders placed today."""
    now = timezone.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return get_orders_in_date_range(start_of_day, now)

def get_yesterday_orders() -> QuerySet[Order]:
    """Return all orders placed yesterday."""
    now = timezone.now()
    end_of_yesterday = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_yesterday = end_of_yesterday - timedelta(days=1)
    return get_orders_in_date_range(start_of_yesterday, end_of_yesterday)

def get_this_week_orders() -> QuerySet[Order]:
    """Return all orders placed in the last 7 days."""
    return get_orders_in_date_range(
        timezone.now() - timedelta(days=7),
        timezone.now(),
    )

def get_this_month_orders() -> QuerySet[Order]:
    """Return all orders placed since the start of the current month."""
    now = timezone.now()
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return get_orders_in_date_range(start_of_month, now)

def get_this_year_orders() -> QuerySet[Order]:
    """Return all orders placed since the start of the current year."""
    now = timezone.now()
    start_of_year = now.replace(
        month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return get_orders_in_date_range(start_of_year, now)

def get_orders_since(moment: datetime) -> QuerySet[Order]:
    """Return all orders placed on-or-after ``moment``."""
    return get_base_order_queryset().filter(created_at__gte=moment)

def get_orders_before(moment: datetime) -> QuerySet[Order]:
    """Return all orders placed on-or-before ``moment``."""
    return get_base_order_queryset().filter(created_at__lte=moment)

# ==============================================================================
# 7. AGGREGATIONS / DASHBOARD
# ==============================================================================
def get_recent_orders(limit: int = 10) -> QuerySet[Order]:
    """Return the most recent orders overall."""
    return get_base_order_queryset().order_by("-created_at")[:limit]

def get_largest_orders(limit: int = 10) -> QuerySet[Order]:
    """Return the orders with the highest ``total``."""
    return get_base_order_queryset().order_by("-total")[:limit]

def get_order_count(
    queryset: Optional[QuerySet[Order]] = None,
) -> int:
    """Return the count of orders in ``queryset`` (or all orders)."""
    if queryset is None:
        queryset = Order.objects.all()
    return queryset.count()

def get_sales_summary(
    queryset: Optional[QuerySet[Order]] = None,
) -> Dict[str, Any]:
    """
    Return a financial summary of the orders in ``queryset``.

    Defaults to every order that is NOT in a terminal-failure
    state (cancelled / refunded / failed).

    Returns a ``dict`` with the keys: ``revenue``, ``subtotal``,
    ``discounts``, ``shipping``, ``tax``, ``order_count``,
    ``average_order_value`` and ``currency_breakdown``.
    """
    if queryset is None:
        queryset = Order.objects.exclude(
            status__in=(
                Order.OrderStatus.CANCELLED,
                Order.OrderStatus.REFUNDED,
                Order.OrderStatus.FAILED,
            )
        )

    aggregates = queryset.aggregate(
        total_revenue=Coalesce(Sum("total"), Decimal("0.00")),
        total_subtotal=Coalesce(Sum("subtotal"), Decimal("0.00")),
        total_discounts=Coalesce(Sum("discount_total"), Decimal("0.00")),
        total_shipping=Coalesce(Sum("shipping_cost"), Decimal("0.00")),
        total_tax=Coalesce(Sum("tax_total"), Decimal("0.00")),
        order_count=Count("id"),
    )

    order_count = aggregates["order_count"] or 0
    total_revenue = aggregates["total_revenue"] or Decimal("0.00")
    average_order_value = (
        total_revenue / order_count if order_count > 0 else Decimal("0.00")
    )

    currency_breakdown = list(
        queryset.values("currency").annotate(
            currency_revenue=Coalesce(Sum("total"), Decimal("0.00")),
            currency_order_count=Count("id"),
        ).order_by("-currency_revenue")
    )

    return {
        "revenue": total_revenue,
        "subtotal": aggregates["total_subtotal"],
        "discounts": aggregates["total_discounts"],
        "shipping": aggregates["total_shipping"],
        "tax": aggregates["total_tax"],
        "order_count": order_count,
        "average_order_value": average_order_value.quantize(Decimal("0.01")),
        "currency_breakdown": currency_breakdown,
    }

def get_status_distribution() -> Dict[str, int]:
    """
    Return the count of orders grouped by ``status``.

    The result is a ``dict`` keyed by the canonical status values
    declared in ``Order.OrderStatus``.
    """
    rows = (
        Order.objects
        .values("status")
        .annotate(count=Count("id"))
    )
    distribution: Dict[str, int] = {
        choice.value: 0 for choice in Order.OrderStatus
    }
    for row in rows:
        distribution[row["status"]] = row["count"]
    return distribution

def get_payment_status_distribution() -> Dict[str, int]:
    """Return the count of orders grouped by ``payment_status``."""
    rows = Order.objects.values("payment_status").annotate(count=Count("id"))
    distribution: Dict[str, int] = {
        choice.value: 0 for choice in Order.PaymentStatus
    }
    for row in rows:
        distribution[row["payment_status"]] = row["count"]
    return distribution

def get_source_distribution() -> Dict[str, int]:
    """Return the count of orders grouped by ``source``."""
    rows = Order.objects.values("source").annotate(count=Count("id"))
    distribution: Dict[str, int] = {
        choice.value: 0 for choice in Order.Source
    }
    for row in rows:
        distribution[row["source"]] = row["count"]
    return distribution

def get_daily_summary(
    days: int = 30,
) -> List[Dict[str, Any]]:
    """
    Return a per-day summary of order volume for the last ``days``.

    Each row is a dict with keys ``date``, ``order_count``,
    ``revenue``.
    """
    since = timezone.now() - timedelta(days=days)
    rows = (
        Order.objects
        .filter(created_at__gte=since)
        .extra(select={"day": "date(created_at)"})
        .values("day")
        .annotate(
            order_count=Count("id"),
            revenue=Coalesce(Sum("total"), Decimal("0.00")),
        )
        .order_by("day")
    )
    return [
        {
            "date": row["day"],
            "order_count": row["order_count"],
            "revenue": row["revenue"],
        }
        for row in rows
    ]

def get_monthly_summary(
    months: int = 12,
) -> List[Dict[str, Any]]:
    """
    Return a per-month summary of order volume for the last
    ``months``.
    """
    since = timezone.now() - timedelta(days=months * 30)
    rows = (
        Order.objects
        .filter(created_at__gte=since)
        .extra(select={"month": "to_char(created_at, 'YYYY-MM')"})
        .values("month")
        .annotate(
            order_count=Count("id"),
            revenue=Coalesce(Sum("total"), Decimal("0.00")),
        )
        .order_by("month")
    )
    return [
        {
            "month": row["month"],
            "order_count": row["order_count"],
            "revenue": row["revenue"],
        }
        for row in rows
    ]

def get_kpi_summary() -> Dict[str, Any]:
    """
    Return a single-page KPI summary of the order domain.

    Designed for executive dashboards. All values are computed
    via optimised aggregate queries.
    """
    today = get_today_orders()
    this_week = get_this_week_orders()
    this_month = get_this_month_orders()
    return {
        "orders_today": today.count(),
        "revenue_today": today.aggregate(
            t=Coalesce(Sum("total"), Decimal("0.00"))
        )["t"],
        "orders_this_week": this_week.count(),
        "revenue_this_week": this_week.aggregate(
            t=Coalesce(Sum("total"), Decimal("0.00"))
        )["t"],
        "orders_this_month": this_month.count(),
        "revenue_this_month": this_month.aggregate(
            t=Coalesce(Sum("total"), Decimal("0.00"))
        )["t"],
        "pending_orders": get_pending_orders().count(),
        "awaiting_payment_orders": get_awaiting_payment_orders().count(),
        "on_hold_orders": get_on_hold_orders().count(),
        "failed_orders": get_failed_orders().count(),
        "unpaid_orders": get_unpaid_orders().count(),
        "open_returns": get_pending_returns().count(),
        "pending_refunds": get_pending_refunds().count(),
    }

# ==============================================================================
# 8. SHIPMENT QUERIES
# ==============================================================================
def get_order_shipments(order: Order) -> QuerySet[Shipment]:
    """Return all shipments for an order."""
    return (
        Shipment.objects
        .select_related("warehouse")
        .filter(order=order)
        .order_by("-created_at")
    )

def get_shipment_by_number(shipment_number: str) -> Optional[Shipment]:
    """Return a shipment by its human-readable ``shipment_number``."""
    return (
        Shipment.objects
        .select_related("order", "warehouse")
        .filter(shipment_number=shipment_number)
        .first()
    )

def get_shipment_by_tracking_number(
    tracking_number: str,
) -> Optional[Shipment]:
    """Return the shipment matching a carrier tracking number."""
    return (
        Shipment.objects
        .select_related("order", "warehouse")
        .filter(tracking_number=tracking_number)
        .first()
    )

def get_pending_shipments() -> QuerySet[Shipment]:
    """Return all shipments awaiting dispatch."""
    return (
        Shipment.objects
        .select_related("order", "warehouse")
        .filter(status=Shipment.ShipmentStatus.PENDING)
        .order_by("created_at")
    )

def get_active_shipments() -> QuerySet[Shipment]:
    """Return all shipments currently in transit."""
    in_transit = (
        Shipment.ShipmentStatus.DISPATCHED,
        Shipment.ShipmentStatus.IN_TRANSIT,
        Shipment.ShipmentStatus.OUT_FOR_DELIVERY,
    )
    return (
        Shipment.objects
        .select_related("order", "warehouse")
        .filter(status__in=in_transit)
        .order_by("dispatch_date")
    )

def get_delivered_shipments(
    limit: Optional[int] = None,
) -> QuerySet[Shipment]:
    """Return all successfully delivered shipments."""
    qs = (
        Shipment.objects
        .select_related("order", "warehouse")
        .filter(status=Shipment.ShipmentStatus.DELIVERED)
        .order_by("-delivery_date")
    )
    if limit is not None:
        qs = qs[:limit]
    return qs

def get_shipment_dashboard() -> Dict[str, int]:
    """Return counts of shipments in each high-level state."""
    in_transit = (
        Shipment.ShipmentStatus.DISPATCHED,
        Shipment.ShipmentStatus.IN_TRANSIT,
        Shipment.ShipmentStatus.OUT_FOR_DELIVERY,
    )
    return {
        "pending": Shipment.objects.filter(
            status=Shipment.ShipmentStatus.PENDING,
        ).count(),
        "in_transit": Shipment.objects.filter(status__in=in_transit).count(),
        "delivered": Shipment.objects.filter(
            status=Shipment.ShipmentStatus.DELIVERED,
        ).count(),
        "exception": Shipment.objects.filter(
            status=Shipment.ShipmentStatus.EXCEPTION,
        ).count(),
        "returned": Shipment.objects.filter(
            status=Shipment.ShipmentStatus.RETURNED,
        ).count(),
    }

# ==============================================================================
# 9. PAYMENT QUERIES
# ==============================================================================
def get_order_payments(order: Order) -> QuerySet[Payment]:
    """Return all payment records for an order."""
    return (
        Payment.objects
        .filter(order=order)
        .order_by("-created_at")
    )

def get_payment_by_transaction_id(
    transaction_id: str,
) -> Optional[Payment]:
    """Return a payment by its gateway-assigned transaction id."""
    return (
        Payment.objects
        .select_related("order")
        .filter(transaction_id=transaction_id)
        .first()
    )

def get_successful_payments(
    limit: Optional[int] = None,
) -> QuerySet[Payment]:
    """Return all successfully captured payments."""
    qs = (
        Payment.objects
        .select_related("order")
        .filter(status=Payment.PaymentState.CAPTURED)
        .order_by("-paid_at")
    )
    if limit is not None:
        qs = qs[:limit]
    return qs

def get_pending_payments() -> QuerySet[Payment]:
    """Return all payments in the ``PENDING`` state."""
    return (
        Payment.objects
        .select_related("order")
        .filter(status=Payment.PaymentState.PENDING)
    )

def get_failed_payments(
    limit: Optional[int] = None,
) -> QuerySet[Payment]:
    """Return all payments in the ``FAILED`` state."""
    qs = (
        Payment.objects
        .select_related("order")
        .filter(status=Payment.PaymentState.FAILED)
        .order_by("-created_at")
    )
    if limit is not None:
        qs = qs[:limit]
    return qs

def get_refundable_payments() -> QuerySet[Payment]:
    """
    Return all payments that can be refunded (captured / completed).
    """
    return Payment.objects.select_related("order").filter(
        status__in=(
            Payment.PaymentState.CAPTURED,
            Payment.PaymentState.COMPLETED,
        )
    )

def get_payment_summary_by_gateway(
    start_date: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Return aggregated payment volume by gateway.

    Each row is a dict with keys ``gateway``, ``total_volume``,
    ``transaction_count``.
    """
    qs = Payment.objects.filter(
        status=Payment.PaymentState.CAPTURED,
    )
    if start_date is not None:
        qs = qs.filter(created_at__gte=start_date)

    rows = (
        qs.values("gateway")
        .annotate(
            total_volume=Coalesce(Sum("amount"), Decimal("0.00")),
            transaction_count=Count("id"),
        )
        .order_by("-total_volume")
    )
    return list(rows)

def get_payment_dashboard() -> Dict[str, Any]:
    """Return payment-domain KPIs for dashboards."""
    successful = Payment.objects.filter(
        status=Payment.PaymentState.CAPTURED,
    )
    return {
        "total_successful_payments": successful.count(),
        "total_successful_volume": successful.aggregate(
            t=Coalesce(Sum("amount"), Decimal("0.00"))
        )["t"],
        "pending_payments": Payment.objects.filter(
            status=Payment.PaymentState.PENDING,
        ).count(),
        "failed_payments": Payment.objects.filter(
            status=Payment.PaymentState.FAILED,
        ).count(),
        "refunded_payments": Payment.objects.filter(
            status=Payment.PaymentState.REFUNDED,
        ).count(),
        "disputed_payments": Payment.objects.filter(
            status=Payment.PaymentState.DISPUTED,
        ).count(),
    }

# ==============================================================================
# 10. REFUND QUERIES
# ==============================================================================
def get_order_refunds(order: Order) -> QuerySet[Refund]:
    """Return all refunds for an order."""
    return (
        Refund.objects
        .select_related("payment", "approved_by")
        .filter(order=order)
        .order_by("-created_at")
    )

def get_refund_by_id(refund_id: int) -> Optional[Refund]:
    """Return a single refund by primary key."""
    return (
        Refund.objects
        .select_related("order", "payment", "approved_by")
        .filter(pk=refund_id)
        .first()
    )

def get_pending_refunds() -> QuerySet[Refund]:
    """Return refunds awaiting administrative approval."""
    return (
        Refund.objects
        .select_related("order", "payment")
        .filter(status=Refund.RefundStatus.REQUESTED)
        .order_by("-created_at")
    )

def get_completed_refunds() -> QuerySet[Refund]:
    """Return successfully processed refunds."""
    return (
        Refund.objects
        .select_related("order", "payment", "approved_by")
        .filter(status=Refund.RefundStatus.PROCESSED)
        .order_by("-processed_at")
    )

def get_rejected_refunds() -> QuerySet[Refund]:
    """Return rejected refunds."""
    return (
        Refund.objects
        .select_related("order", "payment", "approved_by")
        .filter(status=Refund.RefundStatus.REJECTED)
        .order_by("-updated_at")
    )

def get_refund_summary(
    queryset: Optional[QuerySet[Refund]] = None,
) -> Dict[str, Any]:
    """
    Return aggregate metrics for the supplied refund queryset.
    """
    if queryset is None:
        queryset = Refund.objects.all()

    aggregates = queryset.aggregate(
        total_amount=Coalesce(Sum("amount"), Decimal("0.00")),
        refund_count=Count("id"),
        avg_amount=Coalesce(Avg("amount"), Decimal("0.00")),
    )
    status_breakdown = list(
        queryset.values("status").annotate(count=Count("id"))
    )
    return {
        "total_amount": aggregates["total_amount"],
        "refund_count": aggregates["refund_count"],
        "average_amount": aggregates["avg_amount"],
        "status_breakdown": status_breakdown,
    }

# ==============================================================================
# 11. RETURN REQUEST QUERIES
# ==============================================================================
def get_order_returns(order: Order) -> QuerySet[ReturnRequest]:
    """Return all return requests for an order."""
    return (
        ReturnRequest.objects
        .filter(order=order)
        .order_by("-created_at")
    )

def get_return_by_number(return_number: str) -> Optional[ReturnRequest]:
    """Return a return request by its human-readable ``return_number``."""
    return (
        ReturnRequest.objects
        .select_related("order")
        .filter(return_number=return_number)
        .first()
    )

def get_pending_returns() -> QuerySet[ReturnRequest]:
    """Return all returns currently awaiting review / action."""
    return (
        ReturnRequest.objects
        .select_related("order")
        .filter(
            status__in=(
                ReturnRequest.ReturnStatus.REQUESTED,
                ReturnRequest.ReturnStatus.UNDER_REVIEW,
                ReturnRequest.ReturnStatus.AWAITING_SHIPMENT,
                ReturnRequest.ReturnStatus.IN_TRANSIT,
            )
        )
        .order_by("-created_at")
    )

def get_approved_returns() -> QuerySet[ReturnRequest]:
    """Return all approved returns."""
    return (
        ReturnRequest.objects
        .select_related("order")
        .filter(status=ReturnRequest.ReturnStatus.APPROVED)
        .order_by("-approved_at")
    )

def get_completed_returns() -> QuerySet[ReturnRequest]:
    """Return all completed returns."""
    return (
        ReturnRequest.objects
        .select_related("order")
        .filter(status=ReturnRequest.ReturnStatus.COMPLETED)
        .order_by("-completed_at")
    )

def get_return_dashboard() -> Dict[str, int]:
    """Return return-domain KPIs for dashboards."""
    return {
        "requested": ReturnRequest.objects.filter(
            status=ReturnRequest.ReturnStatus.REQUESTED,
        ).count(),
        "under_review": ReturnRequest.objects.filter(
            status=ReturnRequest.ReturnStatus.UNDER_REVIEW,
        ).count(),
        "approved": ReturnRequest.objects.filter(
            status=ReturnRequest.ReturnStatus.APPROVED,
        ).count(),
        "rejected": ReturnRequest.objects.filter(
            status=ReturnRequest.ReturnStatus.REJECTED,
        ).count(),
        "in_transit": ReturnRequest.objects.filter(
            status=ReturnRequest.ReturnStatus.IN_TRANSIT,
        ).count(),
        "completed": ReturnRequest.objects.filter(
            status=ReturnRequest.ReturnStatus.COMPLETED,
        ).count(),
    }

# ==============================================================================
# 12. TIMELINE / ATTACHMENT / NOTE QUERIES
# ==============================================================================
def get_order_timeline(
    order: Order,
    *,
    customer_visible_only: bool = False,
) -> QuerySet[Any]:
    """
    Return the granular timeline for an order, newest first.

    When ``customer_visible_only`` is ``True``, only events with
    ``is_visible_to_customer=True`` are returned.
    """
    from apps.orders.models import OrderTimelineEvent

    qs = OrderTimelineEvent.objects.filter(order=order).order_by(
        "-occurred_at", "-id"
    )
    if customer_visible_only:
        qs = qs.filter(is_visible_to_customer=True)
    return qs

def get_order_status_history(order: Order) -> QuerySet[OrderStatusHistory]:
    """Return the legacy status history for an order."""
    return OrderStatusHistory.objects.filter(order=order).order_by("-created_at")

def get_order_attachments(
    order: Order,
    *,
    active_only: bool = True,
) -> QuerySet[Any]:
    """
    Return the attachments for an order.

    When ``active_only`` is ``True`` (default), only attachments
    with ``is_active=True`` are returned.
    """
    from apps.orders.models import OrderAttachment

    qs = OrderAttachment.objects.filter(order=order).order_by("-created_at")
    if active_only:
        qs = qs.filter(is_active=True)
    return qs

def get_latest_attachments(
    order: Order,
    limit: int = 5,
) -> QuerySet[Any]:
    """Return the most recent attachments for an order."""
    return get_order_attachments(order)[:limit]

def get_order_notes(
    order: Order,
    *,
    customer_visible_only: bool = False,
) -> QuerySet[Any]:
    """Return the notes for an order, newest first."""
    from apps.orders.models import OrderNote

    qs = OrderNote.objects.filter(order=order).order_by(
        "-is_pinned", "-created_at"
    )
    if customer_visible_only:
        qs = qs.filter(is_visible_to_customer=True)
    return qs

def get_pinned_notes(order: Order) -> QuerySet[Any]:
    """Return the pinned notes for an order."""
    from apps.orders.models import OrderNote

    return OrderNote.objects.filter(
        order=order, is_pinned=True,
    ).order_by("-created_at")

# ==============================================================================
# 13. COUPON USAGE QUERIES
# ==============================================================================
def get_coupon_usage_history(
    coupon_code: str,
) -> QuerySet[CouponUsage]:
    """Return the historical usage of a specific coupon code."""
    return (
        CouponUsage.objects
        .select_related("user", "order")
        .filter(coupon_code__iexact=coupon_code)
        .order_by("-used_at")
    )

def get_coupon_usage_stats(coupon_code: str) -> Dict[str, Any]:
    """Return aggregated usage statistics for a single coupon code."""
    aggregates = CouponUsage.objects.filter(
        coupon_code__iexact=coupon_code,
    ).aggregate(
        total_discount=Coalesce(Sum("discount_amount"), Decimal("0.00")),
        usage_count=Count("id"),
        reversed_count=Count("id", filter=Q(is_reversed=True)),
    )
    return {
        "coupon_code": coupon_code,
        "total_discount": aggregates["total_discount"],
        "usage_count": aggregates["usage_count"],
        "reversed_count": aggregates["reversed_count"],
        "active_usage_count": (
            aggregates["usage_count"] - aggregates["reversed_count"]
        ),
    }

def get_user_coupon_usage(user: Any) -> QuerySet[CouponUsage]:
    """Return all coupons redeemed by a specific user."""
    if user is None or not getattr(user, "is_authenticated", False):
        return CouponUsage.objects.none()
    return (
        CouponUsage.objects
        .select_related("order")
        .filter(user=user)
        .order_by("-used_at")
    )

def get_top_coupons(
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Return the most-used coupon codes by total discount value."""
    return list(
        CouponUsage.objects
        .values("coupon_code")
        .annotate(
            total_discount=Coalesce(Sum("discount_amount"), Decimal("0.00")),
            usage_count=Count("id"),
        )
        .order_by("-total_discount")[:limit]
    )

# ==============================================================================
# 14. ORDER ADDRESS QUERIES
# ==============================================================================
def get_address_snapshot_by_id(
    snapshot_id: uuid.UUID,
) -> Optional[OrderAddressSnapshot]:
    """Return a single address snapshot by its UUID primary key."""
    return OrderAddressSnapshot.objects.filter(pk=snapshot_id).first()

def get_recent_address_snapshots(
    limit: int = 25,
) -> QuerySet[OrderAddressSnapshot]:
    """Return the most-recently-created address snapshots."""
    return OrderAddressSnapshot.objects.order_by("-created_at")[:limit]

# ==============================================================================
# 15. CSV EXPORT (read-only projection)
# ==============================================================================
def get_orders_for_csv_export(
    queryset: Optional[QuerySet[Order]] = None,
) -> QuerySet[Order]:
    """
    Return the safe-to-export ``Order`` queryset.

    The whitelist of fields is declared in
    ``constants.CSV_EXPORT_FIELDS`` and matches the admin export
    action. The queryset is returned unevaluated; the caller
    iterates over it.
    """
    if queryset is None:
        queryset = get_base_order_queryset()
    return queryset.only(*c.CSV_EXPORT_FIELDS).order_by("-created_at")

# ==============================================================================
# 16. PAGINATION HELPER
# ==============================================================================
def paginate_orders(
    queryset: QuerySet[Order],
    *,
    page: int = 1,
    page_size: int = c.DEFAULT_PAGE_SIZE,
    max_page_size: int = c.ADMIN_MAX_SHOW_ALL,
) -> Tuple[List[Order], int]:
    """
    Paginate a queryset manually.

    Returns a tuple of (page_results, total_count). The caller
    is expected to use these values to render pagination controls.

    Use this helper for plain Django views that do NOT have access
    to Django's built-in ``Paginator``. The admin / DRF layers
    have their own pagination infrastructure.
    """
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = c.DEFAULT_PAGE_SIZE
    if page_size > max_page_size:
        page_size = max_page_size

    total = queryset.count()
    start = (page - 1) * page_size
    end = start + page_size
    results = list(queryset[start:end])
    return results, total

# ==============================================================================
# 17. SHIPMENT ITEMS
# ==============================================================================
def get_shipment_items(shipment: Shipment) -> QuerySet[ShipmentItem]:
    """Return the line items of a single shipment."""
    return (
        ShipmentItem.objects
        .select_related("order_item", "order_item__product", "replaced_from")
        .filter(shipment=shipment)
        .order_by("id")
    )

# ==============================================================================
# 18. PAYMENT ATTEMPTS
# ==============================================================================
def get_payment_attempts(
    payment: Payment,
) -> QuerySet[PaymentAttempt]:
    """Return every attempt recorded against ``payment``."""
    return (
        PaymentAttempt.objects
        .filter(payment=payment)
        .order_by("-attempted_at")
    )

def get_latest_payment_attempt(
    payment: Payment,
) -> Optional[PaymentAttempt]:
    """Return the most-recent payment attempt (or ``None``)."""
    return (
        PaymentAttempt.objects
        .filter(payment=payment)
        .order_by("-attempted_at")
        .first()
    )

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Base querysets
    "get_base_order_queryset",
    "get_detailed_order_queryset",
    # Order retrieval
    "get_order_by_uuid",
    "get_order_by_number",
    "get_order_detail",
    "get_orders",
    # Customer order queries
    "get_customer_orders",
    "get_customer_order_count",
    "get_customer_recent_orders",
    "get_customer_completed_orders",
    "get_customer_cancelled_orders",
    "get_customer_open_orders",
    # Status-based queries
    "get_orders_by_status",
    "get_orders_by_payment_status",
    "get_orders_by_currency",
    "get_orders_by_source",
    "get_pending_orders",
    "get_processing_orders",
    "get_shipped_orders",
    "get_delivered_orders",
    "get_cancelled_orders",
    "get_refunded_orders",
    "get_completed_orders",
    "get_awaiting_payment_orders",
    "get_on_hold_orders",
    "get_failed_orders",
    "get_paid_orders",
    "get_unpaid_orders",
    "get_gift_orders",
    "get_orders_pending_fraud_review",
    # Items / history
    "get_order_items",
    "get_order_item_by_id",
    "get_gift_items",
    "get_personalized_items",
    "get_order_history",
    # Search
    "search_orders",
    # Date windows
    "get_orders_in_date_range",
    "get_today_orders",
    "get_yesterday_orders",
    "get_this_week_orders",
    "get_this_month_orders",
    "get_this_year_orders",
    "get_orders_since",
    "get_orders_before",
    # Aggregations / dashboard
    "get_recent_orders",
    "get_largest_orders",
    "get_order_count",
    "get_sales_summary",
    "get_status_distribution",
    "get_payment_status_distribution",
    "get_source_distribution",
    "get_daily_summary",
    "get_monthly_summary",
    "get_kpi_summary",
    # Shipments
    "get_order_shipments",
    "get_shipment_by_number",
    "get_shipment_by_tracking_number",
    "get_pending_shipments",
    "get_active_shipments",
    "get_delivered_shipments",
    "get_shipment_dashboard",
    "get_shipment_items",
    # Payments
    "get_order_payments",
    "get_payment_by_transaction_id",
    "get_successful_payments",
    "get_pending_payments",
    "get_failed_payments",
    "get_refundable_payments",
    "get_payment_summary_by_gateway",
    "get_payment_dashboard",
    "get_payment_attempts",
    "get_latest_payment_attempt",
    # Refunds
    "get_order_refunds",
    "get_refund_by_id",
    "get_pending_refunds",
    "get_completed_refunds",
    "get_rejected_refunds",
    "get_refund_summary",
    # Returns
    "get_order_returns",
    "get_return_by_number",
    "get_pending_returns",
    "get_approved_returns",
    "get_completed_returns",
    "get_return_dashboard",
    # Timeline / attachments / notes
    "get_order_timeline",
    "get_order_status_history",
    "get_order_attachments",
    "get_latest_attachments",
    "get_order_notes",
    "get_pinned_notes",
    # Coupons
    "get_coupon_usage_history",
    "get_coupon_usage_stats",
    "get_user_coupon_usage",
    "get_top_coupons",
    # Addresses
    "get_address_snapshot_by_id",
    "get_recent_address_snapshots",
    # Export / pagination
    "get_orders_for_csv_export",
    "paginate_orders",
]