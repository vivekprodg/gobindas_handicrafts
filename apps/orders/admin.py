from __future__ import annotations

import csv
import logging
from typing import Any, List, Optional

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.orders.models import (
    CouponUsage,
    DiscountLine,
    Order,
    OrderAddressSnapshot,
    OrderAttachment,
    OrderItem,
    OrderNote,
    OrderStatusHistory,
    OrderTimelineEvent,
    Payment,
    PaymentAttempt,
    Refund,
    ReturnImage,
    ReturnItem,
    ReturnRequest,
    Shipment,
    ShipmentItem,
    TaxLine,
)

logger = logging.getLogger(__name__)

def _format_badge(status: str) -> str:
    colors = {
        "pending": ("#FFF8E7", "#9A7B54"),
        "processing": ("#E8F5E9", "#2E7D32"),
        "shipped": ("#E3F2FD", "#0D47A1"),
        "delivered": ("#E0F2F1", "#00695C"),
        "cancelled": ("#FFEBEE", "#C62828"),
        "completed": ("#E8F5E9", "#2E7D32"),
        "paid": ("#E8F5E9", "#2E7D32"),
    }
    bg, fg = colors.get(str(status).lower(), ("#FAFAFA", "#767676"))
    return format_html(
        '<span style="padding:3px 8px;background:{};color:{};font-size:11px;'
        'font-weight:600;border:1px solid {};border-radius:12px;'
        'text-transform:uppercase;">{}</span>',
        bg, fg, fg, status or "-",
    )

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    raw_id_fields = ("product", "variant", "inventory", "inventory_reservation", "warehouse")
    readonly_fields = ("line_total", "added_at")

class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ("old_status", "new_status", "remarks", "created_by", "created_at")
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Optional[Any] = None) -> bool:
        return False

class ShipmentInline(admin.TabularInline):
    model = Shipment
    extra = 0
    raw_id_fields = ("warehouse",)

class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0

class RefundInline(admin.TabularInline):
    model = Refund
    extra = 0
    raw_id_fields = ("payment", "approved_by")

class TaxLineInline(admin.TabularInline):
    model = TaxLine
    extra = 0

class DiscountLineInline(admin.TabularInline):
    model = DiscountLine
    extra = 0

class OrderNoteInline(admin.TabularInline):
    model = OrderNote
    extra = 0

class OrderAttachmentInline(admin.TabularInline):
    model = OrderAttachment
    extra = 0

class OrderTimelineEventInline(admin.TabularInline):
    model = OrderTimelineEvent
    extra = 0
    readonly_fields = ("event_type", "title", "actor", "occurred_at")
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Optional[Any] = None) -> bool:
        return False

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "customer", "email", "get_status_badge", "get_payment_badge", "total", "currency", "created_at")
    list_filter = ("status", "payment_status", "source", "is_gift", "is_active", "created_at")
    search_fields = ("order_number", "email", "transaction_id", "tracking_number", "customer__email")
    raw_id_fields = ("customer", "shipping_address", "billing_address")
    readonly_fields = ("id", "subtotal", "total", "created_at", "updated_at", "completed_at")
    inlines = [OrderItemInline, PaymentInline, ShipmentInline, RefundInline, TaxLineInline, DiscountLineInline, OrderNoteInline, OrderAttachmentInline, OrderStatusHistoryInline, OrderTimelineEventInline]
    actions = ["mark_processing", "mark_completed", "mark_cancelled", "export_as_csv"]

    @admin.display(description=_("Status"))
    def get_status_badge(self, obj: Order) -> str:
        return _format_badge(obj.status)

    @admin.display(description=_("Payment"))
    def get_payment_badge(self, obj: Order) -> str:
        return _format_badge(obj.payment_status)

    @admin.action(description=_("Mark selected as Processing"))
    def mark_processing(self, request: HttpRequest, queryset: QuerySet[Order]) -> None:
        queryset.update(status=Order.OrderStatus.PROCESSING, updated_at=timezone.now())

    @admin.action(description=_("Mark selected as Completed"))
    def mark_completed(self, request: HttpRequest, queryset: QuerySet[Order]) -> None:
        queryset.update(status=Order.OrderStatus.COMPLETED, completed_at=timezone.now(), updated_at=timezone.now())

    @admin.action(description=_("Mark selected as Cancelled"))
    def mark_cancelled(self, request: HttpRequest, queryset: QuerySet[Order]) -> None:
        queryset.update(status=Order.OrderStatus.CANCELLED, updated_at=timezone.now())

    @admin.action(description=_("Export selected to CSV"))
    def export_as_csv(self, request: HttpRequest, queryset: QuerySet[Order]) -> HttpResponse:
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="orders.csv"'
        writer = csv.writer(response)
        writer.writerow(["Order Number", "Email", "Status", "Payment Status", "Total", "Created At"])
        for o in queryset:
            writer.writerow([o.order_number, o.email, o.status, o.payment_status, o.total, o.created_at])
        return response

@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "product_name_snapshot", "quantity", "unit_price", "line_total", "status")
    list_filter = ("status", "added_at")
    search_fields = ("order__order_number", "product_name_snapshot", "product_sku_snapshot")
    raw_id_fields = ("order", "product", "variant", "inventory", "warehouse")

@admin.register(OrderAddressSnapshot)
class OrderAddressSnapshotAdmin(admin.ModelAdmin):
    list_display = ("full_name", "city", "country", "phone_number", "created_at")
    search_fields = ("full_name", "city", "country", "phone_number")
    readonly_fields = [f.name for f in OrderAddressSnapshot._meta.fields]

@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ("shipment_number", "order", "carrier", "tracking_number", "status", "created_at")
    list_filter = ("status", "carrier", "created_at")
    search_fields = ("shipment_number", "tracking_number", "order__order_number")
    raw_id_fields = ("order", "warehouse")

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "order", "gateway", "amount", "currency", "status", "paid_at")
    list_filter = ("status", "gateway", "created_at")
    search_fields = ("transaction_id", "order__order_number")
    raw_id_fields = ("order",)

@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "payment", "amount", "status", "created_at")
    list_filter = ("status", "refund_method", "created_at")
    raw_id_fields = ("order", "payment", "approved_by")

@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("return_number", "order", "return_type", "status", "created_at")
    list_filter = ("status", "return_type", "created_at")
    search_fields = ("return_number", "order__order_number")
    raw_id_fields = ("order", "requested_by", "approved_by")

@admin.register(CouponUsage)
class CouponUsageAdmin(admin.ModelAdmin):
    list_display = ("coupon_code", "user", "order", "discount_amount", "used_at")
    search_fields = ("coupon_code", "user__email", "order__order_number")
    raw_id_fields = ("user", "order")

@admin.register(OrderNote)
class OrderNoteAdmin(admin.ModelAdmin):
    list_display = ("id", "order", "note_type", "is_pinned", "is_visible_to_customer", "created_at")
    raw_id_fields = ("order", "author")

@admin.register(OrderAttachment)
class OrderAttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "order", "attachment_type", "is_active", "created_at")
    raw_id_fields = ("order", "uploaded_by")

@admin.register(OrderTimelineEvent)
class OrderTimelineEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "title", "order", "occurred_at")
    raw_id_fields = ("order", "actor")
    readonly_fields = [f.name for f in OrderTimelineEvent._meta.fields]

__all__ = [
    "OrderAdmin", "OrderItemAdmin", "OrderAddressSnapshotAdmin", "ShipmentAdmin",
    "PaymentAdmin", "RefundAdmin", "ReturnRequestAdmin", "CouponUsageAdmin",
    "OrderNoteAdmin", "OrderAttachmentAdmin", "OrderTimelineEventAdmin",
]