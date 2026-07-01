import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Union

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.db.models.query import QuerySet
from django.utils import timezone

from apps.orders.models import (
    CouponUsage,
    Order,
    OrderAddressSnapshot,
    OrderItem,
    OrderStatusHistory,
    Payment,
    Refund,
    Shipment,
)

User = get_user_model()


# ==============================================================================
# BASE QUERYSET FACTORIES
# ==============================================================================

def get_base_order_queryset() -> QuerySet[Order]:
    """
    Returns a highly optimized base queryset for orders to prevent N+1 issues.
    """
    return Order.objects.select_related(
        'customer',
        'shipping_address',
        'billing_address'
    )


def get_detailed_order_queryset() -> QuerySet[Order]:
    """
    Returns an order queryset with deep prefetching for detail views.
    """
    return get_base_order_queryset().prefetch_related(
        'items',
        'items__product',
        'items__variant',
        'payments',
        'shipments',
        'refunds',
        'status_history'
    )


# ==============================================================================
# ORDER RETRIEVAL
# ==============================================================================

def get_order_by_uuid(order_id: Union[str, uuid.UUID]) -> Optional[Order]:
    """
    Retrieves an order by its primary key UUID.
    """
    return get_detailed_order_queryset().filter(id=order_id).first()


def get_order_by_number(order_number: str) -> Optional[Order]:
    """
    Retrieves an order by its human-readable order number.
    """
    return get_detailed_order_queryset().filter(order_number=order_number).first()


def get_order_detail(order_id: Union[str, uuid.UUID], user: Optional[Any] = None) -> Optional[Order]:
    """
    Retrieves a fully populated order, optionally scoping it to a specific user.
    """
    qs = get_detailed_order_queryset().filter(id=order_id)
    if user and user.is_authenticated:
        qs = qs.filter(customer=user)
    return qs.first()


# ==============================================================================
# CUSTOMER ORDER QUERIES
# ==============================================================================

def get_customer_orders(user: Any) -> QuerySet[Order]:
    """
    Retrieves all orders for a specific customer.
    """
    if not user or not user.is_authenticated:
        return Order.objects.none()
    return get_base_order_queryset().filter(customer=user).order_by('-created_at')


def get_customer_order_count(user: Any) -> int:
    """
    Returns the total number of orders placed by a customer.
    """
    if not user or not user.is_authenticated:
        return 0
    return Order.objects.filter(customer=user).count()


def get_customer_recent_orders(user: Any, limit: int = 5) -> QuerySet[Order]:
    """
    Retrieves the most recent orders for a customer.
    """
    return get_customer_orders(user)[:limit]


def get_customer_completed_orders(user: Any) -> QuerySet[Order]:
    """
    Retrieves successfully completed or delivered orders for a customer.
    """
    return get_customer_orders(user).filter(
        status__in=[Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED]
    )


def get_customer_cancelled_orders(user: Any) -> QuerySet[Order]:
    """
    Retrieves cancelled or refunded orders for a customer.
    """
    return get_customer_orders(user).filter(
        status__in=[Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED]
    )


# ==============================================================================
# ORDER STATUS & STATE QUERIES
# ==============================================================================

def get_orders_by_status(status: str) -> QuerySet[Order]:
    """
    Retrieves orders matching a specific status.
    """
    return get_base_order_queryset().filter(status=status)


def get_pending_orders() -> QuerySet[Order]:
    """
    Retrieves all pending orders.
    """
    return get_orders_by_status(Order.OrderStatus.PENDING)


def get_processing_orders() -> QuerySet[Order]:
    """
    Retrieves all processing orders.
    """
    return get_orders_by_status(Order.OrderStatus.PROCESSING)


def get_shipped_orders() -> QuerySet[Order]:
    """
    Retrieves all shipped orders.
    """
    return get_orders_by_status(Order.OrderStatus.SHIPPED)


def get_delivered_orders() -> QuerySet[Order]:
    """
    Retrieves all delivered orders.
    """
    return get_orders_by_status(Order.OrderStatus.DELIVERED)


def get_cancelled_orders() -> QuerySet[Order]:
    """
    Retrieves all cancelled orders.
    """
    return get_orders_by_status(Order.OrderStatus.CANCELLED)


def get_refunded_orders() -> QuerySet[Order]:
    """
    Retrieves all refunded orders.
    """
    return get_orders_by_status(Order.OrderStatus.REFUNDED)


def get_paid_orders() -> QuerySet[Order]:
    """
    Retrieves all fully paid orders.
    """
    return get_base_order_queryset().filter(payment_status=Order.PaymentStatus.PAID)


def get_unpaid_orders() -> QuerySet[Order]:
    """
    Retrieves all pending/failed unpaid orders.
    """
    return get_base_order_queryset().filter(
        payment_status__in=[Order.PaymentStatus.PENDING, Order.PaymentStatus.FAILED]
    )


# ==============================================================================
# ORDER ITEMS & HISTORY QUERIES
# ==============================================================================

def get_order_items(order: Order) -> QuerySet[OrderItem]:
    """
    Retrieves all items for a specific order.
    """
    return OrderItem.objects.select_related('product', 'variant').filter(order=order)


def get_order_history(order: Order) -> QuerySet[OrderStatusHistory]:
    """
    Retrieves the status transition history of a specific order.
    """
    return OrderStatusHistory.objects.select_related('created_by').filter(order=order).order_by('-created_at')


# ==============================================================================
# SEARCH & FILTER QUERIES
# ==============================================================================

def search_orders(search_query: str) -> QuerySet[Order]:
    """
    Searches orders efficiently across multiple fields.
    """
    if not search_query:
        return get_base_order_queryset()

    return get_base_order_queryset().filter(
        Q(order_number__icontains=search_query) |
        Q(email__icontains=search_query) |
        Q(customer__email__icontains=search_query) |
        Q(customer__first_name__icontains=search_query) |
        Q(customer__last_name__icontains=search_query) |
        Q(shipping_address__full_name__icontains=search_query) |
        Q(shipping_address__phone_number__icontains=search_query) |
        Q(transaction_id__icontains=search_query) |
        Q(tracking_number__icontains=search_query)
    ).distinct()


# ==============================================================================
# DASHBOARD & REPORTING QUERIES
# ==============================================================================

def get_recent_orders(limit: int = 10) -> QuerySet[Order]:
    """
    Retrieves the most recent overall orders.
    """
    return get_base_order_queryset().order_by('-created_at')[:limit]


def get_orders_in_date_range(start_date: Union[date, datetime], end_date: Union[date, datetime]) -> QuerySet[Order]:
    """
    Retrieves orders placed within a specific date range.
    """
    return get_base_order_queryset().filter(created_at__range=(start_date, end_date))


def get_today_orders() -> QuerySet[Order]:
    """
    Retrieves orders placed today.
    """
    now = timezone.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return get_orders_in_date_range(start_of_day, now)


def get_this_week_orders() -> QuerySet[Order]:
    """
    Retrieves orders placed within the last 7 days.
    """
    now = timezone.now()
    start_of_week = now - timedelta(days=7)
    return get_orders_in_date_range(start_of_week, now)


def get_this_month_orders() -> QuerySet[Order]:
    """
    Retrieves orders placed within the current month.
    """
    now = timezone.now()
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return get_orders_in_date_range(start_of_month, now)


def get_sales_summary(queryset: Optional[QuerySet[Order]] = None) -> Dict[str, Decimal]:
    """
    Returns an aggregated summary of sales metrics for a given order queryset.
    Defaults to all non-cancelled/non-refunded orders if no queryset is provided.
    """
    if queryset is None:
        queryset = Order.objects.exclude(
            status__in=[Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED]
        )

    aggregates = queryset.aggregate(
        total_revenue=Coalesce(Sum('total'), Decimal('0.00')),
        total_subtotal=Coalesce(Sum('subtotal'), Decimal('0.00')),
        total_discounts=Coalesce(Sum('discount_total'), Decimal('0.00')),
        total_shipping=Coalesce(Sum('shipping_cost'), Decimal('0.00')),
        total_tax=Coalesce(Sum('tax'), Decimal('0.00')),
        order_count=Count('id')
    )
    
    return {
        "revenue": aggregates['total_revenue'],
        "subtotal": aggregates['total_subtotal'],
        "discounts": aggregates['total_discounts'],
        "shipping": aggregates['total_shipping'],
        "tax": aggregates['total_tax'],
        "order_count": aggregates['order_count']
    }


# ==============================================================================
# SHIPMENT QUERIES
# ==============================================================================

def get_order_shipments(order: Order) -> QuerySet[Shipment]:
    """
    Retrieves all shipments associated with an order.
    """
    return Shipment.objects.filter(order=order).order_by('-created_at')


def get_pending_shipments() -> QuerySet[Shipment]:
    """
    Retrieves all pending shipments awaiting dispatch.
    """
    return Shipment.objects.select_related('order').filter(status=Shipment.ShipmentStatus.PENDING)


def get_active_shipments() -> QuerySet[Shipment]:
    """
    Retrieves all active shipments currently in transit or dispatched.
    """
    return Shipment.objects.select_related('order').filter(
        status__in=[Shipment.ShipmentStatus.DISPATCHED, Shipment.ShipmentStatus.IN_TRANSIT]
    )


def get_delivered_shipments() -> QuerySet[Shipment]:
    """
    Retrieves all successfully delivered shipments.
    """
    return Shipment.objects.select_related('order').filter(status=Shipment.ShipmentStatus.DELIVERED)


# ==============================================================================
# PAYMENT QUERIES
# ==============================================================================

def get_order_payments(order: Order) -> QuerySet[Payment]:
    """
    Retrieves all payment transactions associated with an order.
    """
    return Payment.objects.filter(order=order).order_by('-created_at')


def get_successful_payments() -> QuerySet[Payment]:
    """
    Retrieves all successfully captured payments.
    """
    return Payment.objects.select_related('order').filter(status=Payment.PaymentState.COMPLETED)


def get_pending_payments() -> QuerySet[Payment]:
    """
    Retrieves all pending payments.
    """
    return Payment.objects.select_related('order').filter(status=Payment.PaymentState.PENDING)


def get_failed_payments() -> QuerySet[Payment]:
    """
    Retrieves all failed payment transactions.
    """
    return Payment.objects.select_related('order').filter(status=Payment.PaymentState.FAILED)


def get_payment_summaries(start_date: Optional[Union[date, datetime]] = None) -> Dict[str, Any]:
    """
    Aggregates overall payment volume by gateway.
    """
    qs = Payment.objects.filter(status=Payment.PaymentState.COMPLETED)
    if start_date:
        qs = qs.filter(created_at__gte=start_date)

    return list(qs.values('gateway').annotate(
        total_volume=Coalesce(Sum('amount'), Decimal('0.00')),
        transaction_count=Count('id')
    ).order_by('-total_volume'))


# ==============================================================================
# REFUND QUERIES
# ==============================================================================

def get_order_refunds(order: Order) -> QuerySet[Refund]:
    """
    Retrieves all refunds associated with an order.
    """
    return Refund.objects.select_related('payment', 'approved_by').filter(order=order).order_by('-created_at')


def get_pending_refunds() -> QuerySet[Refund]:
    """
    Retrieves refunds waiting for administrative approval.
    """
    return Refund.objects.select_related('order', 'payment').filter(status=Refund.RefundStatus.REQUESTED)


def get_completed_refunds() -> QuerySet[Refund]:
    """
    Retrieves successfully processed refunds.
    """
    return Refund.objects.select_related('order', 'payment', 'approved_by').filter(status=Refund.RefundStatus.PROCESSED)


# ==============================================================================
# COUPON USAGE QUERIES
# ==============================================================================

def get_coupon_usage_history(coupon_code: str) -> QuerySet[CouponUsage]:
    """
    Retrieves the historical usage of a specific coupon code.
    """
    return CouponUsage.objects.select_related('user', 'order').filter(coupon_code__iexact=coupon_code).order_by('-used_at')


def get_coupon_usage_stats(coupon_code: str) -> Dict[str, Any]:
    """
    Returns aggregated statistics for a specific coupon code.
    """
    aggregates = CouponUsage.objects.filter(coupon_code__iexact=coupon_code).aggregate(
        total_discount=Coalesce(Sum('discount_amount'), Decimal('0.00')),
        usage_count=Count('id')
    )
    return {
        "coupon_code": coupon_code,
        "total_discount": aggregates['total_discount'],
        "usage_count": aggregates['usage_count']
    }


def get_user_coupon_usage(user: Any) -> QuerySet[CouponUsage]:
    """
    Retrieves all coupons used by a specific user.
    """
    if not user or not user.is_authenticated:
        return CouponUsage.objects.none()
    return CouponUsage.objects.select_related('order').filter(user=user).order_by('-used_at')