from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

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
    OrderTimelineEvent,
    Payment,
    PaymentAttempt,
    Refund,
    ReturnRequest,
    Shipment,
    ShipmentItem,
)

def get_base_order_queryset() -> QuerySet[Order]:
    return Order.objects.select_related("customer", "shipping_address", "billing_address")

def get_detailed_order_queryset() -> QuerySet[Order]:
    return get_base_order_queryset().prefetch_related(
        "items", "items__product", "items__variant", "items__warehouse",
        "payments", "payments__attempts", "shipments", "shipments__line_items",
        "refunds", "status_history", "tax_lines", "discount_lines",
        "order_notes", "attachments", "coupon_usages", "timeline_events", "return_requests",
    )

def get_order_by_uuid(order_id: Union[str, uuid.UUID]) -> Optional[Order]:
    return get_detailed_order_queryset().filter(id=order_id).first()

def get_order_by_number(order_number: str) -> Optional[Order]:
    return get_detailed_order_queryset().filter(order_number=order_number).first()

def get_order_detail(order_id: Union[str, uuid.UUID], user: Optional[Any] = None, scoped_to_user: bool = False) -> Optional[Order]:
    qs = get_detailed_order_queryset().filter(id=order_id)
    if scoped_to_user and user and getattr(user, "is_authenticated", False):
        qs = qs.filter(customer=user)
    return qs.first()

def get_orders(**kwargs: Any) -> QuerySet[Order]:
    qs = get_base_order_queryset()
    status = kwargs.get("status")
    if status:
        qs = qs.filter(status=status)
    payment_status = kwargs.get("payment_status")
    if payment_status:
        qs = qs.filter(payment_status=payment_status)
    customer = kwargs.get("customer")
    if customer:
        qs = qs.filter(customer=customer)
    return qs

def get_order_list_for_user(user: Any, filters: Optional[Dict[str, Any]] = None) -> QuerySet[Order]:
    qs = get_base_order_queryset()
    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        qs = qs.filter(customer=user)

    if filters:
        q = filters.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(order_number__icontains=q) |
                Q(email__icontains=q) |
                Q(transaction_id__icontains=q) |
                Q(tracking_number__icontains=q)
            )
        status = filters.get("status")
        if status:
            qs = qs.filter(status=status)
        payment_status = filters.get("payment_status")
        if payment_status:
            qs = qs.filter(payment_status=payment_status)

    return qs.order_by("-created_at")

def get_customer_orders(user: Any) -> QuerySet[Order]:
    if not user or not getattr(user, "is_authenticated", False):
        return Order.objects.none()
    return get_base_order_queryset().filter(customer=user).order_by("-created_at")

def get_customer_order_count(user: Any) -> int:
    if not user or not getattr(user, "is_authenticated", False):
        return 0
    return Order.objects.filter(customer=user).count()

def get_customer_recent_orders(user: Any, limit: int = 5) -> QuerySet[Order]:
    return get_customer_orders(user)[:limit]

def get_customer_completed_orders(user: Any) -> QuerySet[Order]:
    return get_customer_orders(user).filter(status__in=(Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED))

def get_customer_cancelled_orders(user: Any) -> QuerySet[Order]:
    return get_customer_orders(user).filter(status__in=(Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED))

def get_customer_open_orders(user: Any) -> QuerySet[Order]:
    terminal = c.OrderStatus.TERMINAL_SUCCESS | c.OrderStatus.TERMINAL_FAILURE
    return get_customer_orders(user).exclude(status__in=terminal)

def search_orders(search_query: str) -> QuerySet[Order]:
    if not search_query:
        return get_base_order_queryset()
    return get_base_order_queryset().filter(
        Q(order_number__icontains=search_query) |
        Q(email__icontains=search_query) |
        Q(transaction_id__icontains=search_query) |
        Q(tracking_number__icontains=search_query)
    ).distinct()

def get_order_items(order: Order) -> QuerySet[OrderItem]:
    return OrderItem.objects.select_related("product", "variant", "warehouse").filter(order=order).order_by("added_at")

def get_order_item_by_id(item_id: int) -> Optional[OrderItem]:
    return OrderItem.objects.select_related("order", "product", "variant").filter(pk=item_id).first()

def get_order_shipments(order: Order) -> QuerySet[Shipment]:
    return Shipment.objects.select_related("warehouse").filter(order=order).order_by("-created_at")

def get_shipment_by_tracking_number(tracking_number: str) -> Optional[Shipment]:
    return Shipment.objects.select_related("order", "warehouse").filter(tracking_number=tracking_number).first()

def get_shipment_items(shipment: Shipment) -> QuerySet[ShipmentItem]:
    return ShipmentItem.objects.select_related("order_item", "order_item__product").filter(shipment=shipment).order_by("id")

def get_order_payments(order: Order) -> QuerySet[Payment]:
    return Payment.objects.filter(order=order).order_by("-created_at")

def get_payment_by_transaction_id(transaction_id: str) -> Optional[Payment]:
    return Payment.objects.select_related("order").filter(transaction_id=transaction_id).first()

def get_payment_attempts(payment: Payment) -> QuerySet[PaymentAttempt]:
    return PaymentAttempt.objects.filter(payment=payment).order_by("-attempted_at")

def get_order_refunds(order: Order) -> QuerySet[Refund]:
    return Refund.objects.select_related("payment", "approved_by").filter(order=order).order_by("-created_at")

def get_refund_by_id(refund_id: int) -> Optional[Refund]:
    return Refund.objects.select_related("order", "payment", "approved_by").filter(pk=refund_id).first()

def get_order_returns(order: Order) -> QuerySet[ReturnRequest]:
    return ReturnRequest.objects.filter(order=order).order_by("-created_at")

def get_order_timeline(order: Order, customer_visible_only: bool = False) -> QuerySet[OrderTimelineEvent]:
    qs = OrderTimelineEvent.objects.filter(order=order).order_by("-occurred_at", "-id")
    if customer_visible_only:
        qs = qs.filter(is_visible_to_customer=True)
    return qs

def get_order_status_history(order: Order) -> QuerySet[OrderStatusHistory]:
    return OrderStatusHistory.objects.filter(order=order).order_by("-created_at")

def get_order_attachments(order: Order, active_only: bool = True) -> QuerySet[Any]:
    qs = order.attachments.order_by("-created_at")
    if active_only:
        qs = qs.filter(is_active=True)
    return qs

def get_order_notes(order: Order, customer_visible_only: bool = False) -> QuerySet[Any]:
    qs = order.order_notes.order_by("-is_pinned", "-created_at")
    if customer_visible_only:
        qs = qs.filter(is_visible_to_customer=True)
    return qs

def get_orders_for_csv_export(queryset: Optional[QuerySet[Order]] = None) -> QuerySet[Order]:
    if queryset is None:
        queryset = get_base_order_queryset()
    return queryset.only(*c.CSV_EXPORT_FIELDS).order_by("-created_at")

def get_kpi_summary() -> Dict[str, Any]:
    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_qs = Order.objects.filter(created_at__gte=today)
    return {
        "orders_today": today_qs.count(),
        "revenue_today": today_qs.aggregate(t=Coalesce(Sum("total"), Decimal("0.00")))["t"],
        "pending_orders": Order.objects.filter(status=Order.OrderStatus.PENDING).count(),
        "processing_orders": Order.objects.filter(status=Order.OrderStatus.PROCESSING).count(),
    }

def get_sales_summary() -> Dict[str, Any]:
    qs = Order.objects.exclude(status__in=(Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED, Order.OrderStatus.FAILED))
    aggs = qs.aggregate(
        total_revenue=Coalesce(Sum("total"), Decimal("0.00")),
        total_subtotal=Coalesce(Sum("subtotal"), Decimal("0.00")),
        order_count=Count("id"),
    )
    return {
        "revenue": aggs["total_revenue"],
        "subtotal": aggs["total_subtotal"],
        "order_count": aggs["order_count"],
    }

def get_status_distribution() -> Dict[str, int]:
    rows = Order.objects.values("status").annotate(count=Count("id"))
    return {r["status"]: r["count"] for r in rows}

def get_payment_status_distribution() -> Dict[str, int]:
    rows = Order.objects.values("payment_status").annotate(count=Count("id"))
    return {r["payment_status"]: r["count"] for r in rows}

def get_source_distribution() -> Dict[str, int]:
    rows = Order.objects.values("source").annotate(count=Count("id"))
    return {r["source"]: r["count"] for r in rows}

def get_shipment_dashboard() -> Dict[str, int]:
    return {
        "pending": Shipment.objects.filter(status=Shipment.ShipmentStatus.PENDING).count(),
        "delivered": Shipment.objects.filter(status=Shipment.ShipmentStatus.DELIVERED).count(),
    }

def get_payment_dashboard() -> Dict[str, Any]:
    return {
        "successful": Payment.objects.filter(status=Payment.PaymentState.CAPTURED).count(),
    }

def get_return_dashboard() -> Dict[str, int]:
    return {
        "requested": ReturnRequest.objects.filter(status=ReturnRequest.ReturnStatus.REQUESTED).count(),
    }

def get_daily_summary(days: int = 30) -> List[Dict[str, Any]]:
    return []

def get_monthly_summary(months: int = 12) -> List[Dict[str, Any]]:
    return []

def get_recent_orders(limit: int = 10) -> QuerySet[Order]:
    return get_base_order_queryset().order_by("-created_at")[:limit]

def get_largest_orders(limit: int = 10) -> QuerySet[Order]:
    return get_base_order_queryset().order_by("-total")[:limit]

def get_order_tracking_info(order_id: Any, user: Any = None) -> Optional[Dict[str, Any]]:
    order = get_order_detail(order_id=order_id, user=user)
    if not order:
        return None
    return {
        "id": str(order.pk),
        "order_number": order.order_number,
        "created_at": order.created_at,
        "status": order.status,
        "get_status_display": order.get_status_display(),
        "carrier": order.carrier,
        "tracking_number": order.tracking_number,
        "shipping_address": order.shipping_address,
        "items": order.items.all(),
        "shipments": order.shipments.all(),
        "customer": order.customer,
    }

__all__ = [
    "get_base_order_queryset", "get_detailed_order_queryset", "get_order_by_uuid",
    "get_order_by_number", "get_order_detail", "get_orders", "get_order_list_for_user",
    "get_customer_orders", "get_customer_order_count", "get_customer_recent_orders",
    "get_customer_completed_orders", "get_customer_cancelled_orders", "get_customer_open_orders",
    "search_orders", "get_order_items", "get_order_item_by_id", "get_order_shipments",
    "get_shipment_by_tracking_number", "get_shipment_items", "get_order_payments",
    "get_payment_by_transaction_id", "get_payment_attempts", "get_order_refunds",
    "get_refund_by_id", "get_order_returns", "get_order_timeline", "get_order_status_history",
    "get_order_attachments", "get_order_notes", "get_orders_for_csv_export",
    "get_kpi_summary", "get_sales_summary", "get_status_distribution",
    "get_payment_status_distribution", "get_source_distribution", "get_shipment_dashboard",
    "get_payment_dashboard", "get_return_dashboard", "get_daily_summary", "get_monthly_summary",
    "get_recent_orders", "get_largest_orders", "get_order_tracking_info",
]