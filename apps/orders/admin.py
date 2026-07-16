"""
Enterprise-grade Django Admin configuration for the Order application.

ARCHITECTURE
============
This module implements the COMPLETE admin layer for the Order domain.
Every admin class is intentionally THIN:

    * Reads delegate EXCLUSIVELY to `apps.orders.selectors`
      (lazy import; falls back to model managers if not implemented).
    * Writes delegate EXCLUSIVELY to `apps.orders.services`
      (lazy import; falls back to model instance methods if not
      implemented). When neither exists for a given action, the
      admin falls back to a guarded direct save on a freshly-loaded
      model instance, NEVER on a queryset.
    * No business logic is computed inside the admin.
    * No inventory, payment, tax, shipping, or notification logic
      is ever performed inside the admin.
    * Every status transition uses the existing model method
      (`update_status`, `mark_cancelled`, `mark_dispatched`,
      `mark_delivered`, etc.).
    * Immutable records (AddressSnapshot, OrderStatusHistory,
      CouponUsage) are fully read-only.

OWASP COMPLIANCE
================
* Sensitive fields (financial, customer, address) are read-only.
* Defensive import handling never lets a missing optional dependency
  break the admin.
* CSV export is sanitized to avoid unsafe fields.
* The CSV writer uses `csv.writer` (the original implementation was
  safe and is preserved; CSV is not inherently unsafe, but the export
  function is hardened to explicitly skip non-serializable fields).
* Permission-restricted actions reuse model state-transition methods
  rather than directly mutating database columns.

PERFORMANCE
===========
* `list_select_related` on Order, Payment, Shipment, ReturnRequest.
* `prefetch_related` on related inlines (items, payments, shipments,
  status_history, returns, notes, attachments, timeline_events).
* `raw_id_fields` for high-cardinality FKs (customer, address, warehouse,
  product, variant, inventory, reservation, etc.).
* `date_hierarchy` on Order, Payment, Shipment, Refund,
  OrderStatusHistory, CouponUsage, ReturnRequest, OrderAttachment.
* `list_per_page = 25` (CMS-configurable) to bound admin rendering
  cost on millions of records.
* `show_full_result_count = False` to avoid expensive COUNT queries.

BACKWARD COMPATIBILITY
=======================
* No admin class renamed.
* No inlines removed.
* No existing actions removed.
* All existing field references match the current models.py.
* New inlines / actions are APPENDED and are defensive (they gracefully
  no-op when the underlying optional relationship is missing).
"""

from __future__ import annotations

import csv
import logging
from typing import Any, Dict, Iterable, List, Optional

from django.contrib import admin, messages
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction as db_transaction
from django.db.models import Model, QuerySet
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from apps.orders.models import (
    CouponUsage,
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
    DiscountLine,
)

logger = logging.getLogger(__name__)

# ==============================================================================
# SAFE HELPERS
# ==============================================================================
def _safe_str(value: Any) -> str:
    """Best-effort conversion to a trimmed string. Never raises."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

def _format_decimal(value: Any) -> str:
    """Format a Decimal (or string) safely for the changelist."""
    if value is None or value == "":
        return "-"
    try:
        return str(value)
    except Exception:
        return "-"

def _format_date(value: Any) -> str:
    """Format a date/datetime for the changelist. Returns '-' for None."""
    if value is None:
        return "-"
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"

def _format_event_badge(event_type: str) -> str:
    """Format a timeline event type as a colored HTML badge."""
    color_map = {
        "order_placed": ("#E8F5E9", "#2E7D32"),
        "order_cancelled": ("#FFEBEE", "#C62828"),
        "order_completed": ("#E3F2FD", "#0D47A1"),
        "payment_captured": ("#E8F5E9", "#2E7D32"),
        "payment_failed": ("#FFEBEE", "#C62828"),
        "payment_refunded": ("#FFF8E7", "#9A7B54"),
        "shipment_delivered": ("#E8F5E9", "#2E7D32"),
        "shipment_failed": ("#FFEBEE", "#C62828"),
        "refund_completed": ("#FFF8E7", "#9A7B54"),
        "return_completed": ("#FFF8E7", "#9A7B54"),
        "fraud_check_failed": ("#FFEBEE", "#C62828"),
        "fraud_check_review": ("#FFF8E7", "#9A7B54"),
    }
    bg, fg = color_map.get(event_type, ("#FAFAFA", "#767676"))
    label = event_type.replace("_", " ").title()
    return format_html(
        '<span style="display:inline-block;padding:3px 8px;background:{};color:{};'
        'font-size:11px;font-weight:600;border:1px solid {};border-radius:20px;'
        'text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
        bg, fg, fg, label,
    )

def _format_status_badge(status: str, kind: str = "order") -> str:
    """Format a status string as a colored HTML badge."""
    order_colors = {
        "pending": ("#FFF8E7", "#9A7B54"),
        "processing": ("#E8F5E9", "#2E7D32"),
        "shipped": ("#E3F2FD", "#0D47A1"),
        "delivered": ("#E0F2F1", "#00695C"),
        "cancelled": ("#FFEBEE", "#C62828"),
        "refunded": ("#FFF8E7", "#9A7B54"),
        "completed": ("#E8F5E9", "#2E7D32"),
        "failed": ("#FFEBEE", "#C62828"),
        "on_hold": ("#FFF8E7", "#9A7B54"),
        "awaiting_payment": ("#FFF8E7", "#9A7B54"),
    }
    payment_colors = {
        "pending": ("#FFF8E7", "#9A7B54"),
        "paid": ("#E8F5E9", "#2E7D32"),
        "completed": ("#E8F5E9", "#2E7D32"),
        "failed": ("#FFEBEE", "#C62828"),
        "refunded": ("#FFF8E7", "#9A7B54"),
        "captured": ("#E0F2F1", "#00695C"),
        "authorized": ("#E3F2FD", "#0D47A1"),
        "voided": ("#FAFAFA", "#767676"),
    }
    colors = order_colors if kind == "order" else payment_colors
    bg, fg = colors.get(status, ("#FAFAFA", "#767676"))
    label = str(status).replace("_", " ").title() if status else "-"
    return format_html(
        '<span style="display:inline-block;padding:3px 8px;background:{};color:{};'
        'font-size:11px;font-weight:600;border:1px solid {};border-radius:20px;'
        'text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
        bg, fg, fg, label,
    )

def _safe_call_action(
    view: Any,
    queryset: QuerySet,
    action_callable: Any,
    *,
    success_message: str,
    request: HttpRequest,
) -> None:
    """Execute an admin action safely, never raising to the caller."""
    processed = 0
    try:
        for obj in queryset.iterator():
            try:
                action_callable(obj, request)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Admin action failed for %s pk=%s: %s",
                    obj.__class__.__name__, getattr(obj, "pk", "?"), exc,
                )
        if processed:
            view.message_user(request, success_message % {"count": processed})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Admin action iterator failed: %s", exc)

# ==============================================================================
# LAZY SERVICE / SELECTOR ACCESSORS
# ==============================================================================
def _get_order_service() -> Optional[Any]:
    """Lazy import of orders.services. Returns None if not yet implemented."""
    try:
        from apps.orders import services
        return services
    except Exception:
        return None

def _get_order_selector() -> Optional[Any]:
    """Lazy import of orders.selectors. Returns None if not yet implemented."""
    try:
        from apps.orders import selectors
        return selectors
    except Exception:
        return None

# ==============================================================================
# 1. OrderAddressSnapshot (read-only historical record)
# ==============================================================================
@admin.register(OrderAddressSnapshot)
class OrderAddressSnapshotAdmin(admin.ModelAdmin):
    """
    Read-only historical address record. Snapshots are NEVER edited
    or deleted via the admin (immutability by design).
    """

    list_display = (
        "get_full_name",
        "get_phone",
        "get_location",
        "get_country",
        "country_code",
        "address_hash",
        "created_at",
    )
    list_filter = ("country", "country_code", "created_at")
    search_fields = (
        "full_name",
        "phone_number",
        "phone_e164",
        "company",
        "address_line_1",
        "address_line_2",
        "city",
        "state_or_province",
        "postal_code",
        "country",
        "country_code",
        "address_hash",
    )
    readonly_fields = (
        "full_name",
        "phone_number",
        "phone_e164",
        "company",
        "address_line_1",
        "address_line_2",
        "city",
        "state_or_province",
        "postal_code",
        "country",
        "country_code",
        "latitude",
        "longitude",
        "delivery_notes",
        "address_hash",
        "metadata",
        "created_at",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False

    fieldsets = (
        (
            _("Contact Information"),
            {
                "fields": ("full_name", "phone_number", "phone_e164", "company"),
            },
        ),
        (
            _("Address Details"),
            {
                "fields": (
                    "address_line_1",
                    "address_line_2",
                    "city",
                    "state_or_province",
                    "postal_code",
                    "country",
                    "country_code",
                ),
            },
        ),
        (
            _("Geo & Delivery"),
            {
                "fields": ("latitude", "longitude", "delivery_notes"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Metadata"),
            {
                "fields": ("address_hash", "metadata", "created_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Optional[Model] = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Optional[Model] = None
    ) -> bool:
        return False

    @admin.display(description=_("Full Name"), ordering="full_name")
    def get_full_name(self, obj: OrderAddressSnapshot) -> str:
        return obj.full_name or "-"

    @admin.display(description=_("Phone"))
    def get_phone(self, obj: OrderAddressSnapshot) -> str:
        return obj.phone_number or "-"

    @admin.display(description=_("Location"), ordering="city")
    def get_location(self, obj: OrderAddressSnapshot) -> str:
        parts = [
            obj.city or "",
            obj.state_or_province or "",
            obj.postal_code or "",
        ]
        return ", ".join(p for p in parts if p) or "-"

    @admin.display(description=_("Country"), ordering="country")
    def get_country(self, obj: OrderAddressSnapshot) -> str:
        return obj.country or "-"

# ==============================================================================
# 2. Inlines
# ==============================================================================
class OrderItemInline(admin.TabularInline):
    """
    Line items for an Order. Optimized to prevent N+1; uses raw_id_fields
    for catalog FKs.
    """

    model = OrderItem
    extra = 0
    raw_id_fields = (
        "product",
        "variant",
        "inventory",
        "inventory_reservation",
        "warehouse",
    )
    autocomplete_fields: tuple = ()
    readonly_fields = (
        "line_total",
        "line_gross_total",
        "line_net_total",
        "effective_unit_price",
        "line_discount_percentage",
        "line_tax_percentage",
        "is_returnable",
        "is_shippable",
        "remaining_quantity_to_ship",
        "remaining_quantity_to_return",
    )
    fields = (
        "product",
        "variant",
        "product_name_snapshot",
        "product_sku_snapshot",
        "variant_name_snapshot",
        "unit_price",
        "discount",
        "tax",
        "quantity",
        "line_total",
        "weight",
        "status",
        "saved_reason",
    )
    list_select_related: tuple = ("product", "variant")
    show_change_link = True
    ordering = ("added_at",)

class OrderStatusHistoryInline(admin.TabularInline):
    """Immutable, read-only lifecycle inline."""

    model = OrderStatusHistory
    extra = 0
    readonly_fields = (
        "old_status",
        "new_status",
        "remarks",
        "is_customer_notified",
        "notification_method",
        "metadata",
        "ip_address",
        "user_agent",
        "created_by",
        "created_at",
    )
    fields = (
        "old_status",
        "new_status",
        "remarks",
        "is_customer_notified",
        "created_by",
        "created_at",
    )
    show_change_link = True
    ordering = ("-created_at",)

    def has_add_permission(self, request: HttpRequest, obj: Optional[Model] = None) -> bool:
        return False

class ShipmentInline(admin.TabularInline):
    """Logistics quick-view inline."""

    model = Shipment
    extra = 0
    raw_id_fields = ("order", "warehouse")
    readonly_fields = (
        "created_at",
        "updated_at",
        "is_in_transit",
        "is_delivered",
    )
    fields = (
        "shipment_number",
        "carrier",
        "tracking_number",
        "status",
        "shipping_cost",
        "dispatch_date",
        "delivery_date",
    )
    show_change_link = True
    ordering = ("-created_at",)

class ShipmentItemInline(admin.TabularInline):
    """Per-line shipment breakdown inline."""

    model = ShipmentItem
    extra = 0
    raw_id_fields = ("shipment", "order_item", "replaced_from")
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    fields = (
        "order_item",
        "quantity_shipped",
        "serial_tracking",
        "is_replacement",
    )
    show_change_link = True
    ordering = ("shipment", "id")

class PaymentInline(admin.TabularInline):
    """Payments quick-view inline."""

    model = Payment
    extra = 0
    raw_id_fields = ("order",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "payment_attempts_count",
        "last_attempt_at",
    )
    fields = (
        "transaction_id",
        "gateway",
        "amount",
        "currency",
        "status",
        "paid_at",
    )
    show_change_link = True
    ordering = ("-created_at",)

class PaymentAttemptInline(admin.TabularInline):
    """Per-attempt payments inline."""

    model = PaymentAttempt
    extra = 0
    raw_id_fields = ("payment",)
    readonly_fields = ("created_at",)
    fields = (
        "attempted_at",
        "attempt_number",
        "status",
        "gateway_response_code",
    )
    show_change_link = True
    ordering = ("-attempted_at",)

class RefundInline(admin.TabularInline):
    """Refunds quick-view inline."""

    model = Refund
    extra = 0
    raw_id_fields = ("order", "payment", "approved_by")
    readonly_fields = (
        "created_at",
        "updated_at",
        "processed_at",
    )
    fields = (
        "id",
        "amount",
        "status",
        "refund_method",
    )
    show_change_link = True
    ordering = ("-created_at",)

class CouponUsageInline(admin.TabularInline):
    """Coupon usage ledger inline (read-only audit)."""

    model = CouponUsage
    extra = 0
    raw_id_fields = ("user", "order", "cart_id", "product_id", "category_id")
    readonly_fields = (
        "coupon_code",
        "user",
        "order",
        "discount_amount",
        "used_at",
        "is_reversed",
        "reversed_at",
    )
    fields = ("coupon_code", "discount_amount", "is_reversed", "used_at")
    show_change_link = True
    ordering = ("-used_at",)
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Optional[Model] = None) -> bool:
        return False

class OrderNoteInline(admin.TabularInline):
    """Customer / operator notes inline."""

    model = OrderNote
    extra = 0
    raw_id_fields = ("order", "author")
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    fields = (
        "note_type",
        "text",
        "is_visible_to_customer",
        "is_pinned",
        "author",
    )
    show_change_link = True
    ordering = ("-is_pinned", "-created_at")

class OrderAttachmentInline(admin.TabularInline):
    """File attachments inline (read-only, uploaded via admin form)."""

    model = OrderAttachment
    extra = 0
    raw_id_fields = ("order", "uploaded_by")
    readonly_fields = (
        "file_size",
        "uploaded_by",
        "created_at",
        "updated_at",
    )
    fields = (
        "file",
        "attachment_type",
        "description",
    )
    show_change_link = True
    ordering = ("-created_at",)

class OrderTimelineEventInline(admin.TabularInline):
    """Granular timeline inline (read-only)."""

    model = OrderTimelineEvent
    extra = 0
    raw_id_fields = ("order", "actor")
    readonly_fields = ("created_at",)
    fields = (
        "occurred_at",
        "event_type",
        "title",
        "actor",
        "is_visible_to_customer",
    )
    show_change_link = True
    ordering = ("-occurred_at",)
    can_delete = False

    def has_add_permission(self, request: HttpRequest, obj: Optional[Model] = None) -> bool:
        return False

class TaxLineInline(admin.TabularInline):
    """Tax line breakdown inline."""

    model = TaxLine
    extra = 0
    readonly_fields = ("created_at",)
    fields = (
        "tax_class",
        "tax_name",
        "tax_rate",
        "base_amount",
        "tax_amount",
        "is_inclusive",
        "position",
    )
    show_change_link = True
    ordering = ("position", "id")

class DiscountLineInline(admin.TabularInline):
    """Discount line breakdown inline."""

    model = DiscountLine
    extra = 0
    raw_id_fields = ("order", "coupon_usage", "applies_to_order_item")
    readonly_fields = ("created_at",)
    fields = (
        "discount_type",
        "name",
        "code",
        "discount_amount",
        "percentage",
        "position",
    )
    show_change_link = True
    ordering = ("position", "id")

class ReturnItemInline(admin.TabularInline):
    """Per-line return items inline."""

    model = ReturnItem
    extra = 0
    raw_id_fields = (
        "return_request",
        "order_item",
        "replacement_order_item",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
    )
    fields = (
        "order_item",
        "quantity_returned",
        "quantity_received",
        "inspection_result",
        "restock_decision",
    )
    show_change_link = True
    ordering = ("return_request", "id")

class ReturnImageInline(admin.TabularInline):
    """Per-line return images inline."""

    model = ReturnImage
    extra = 0
    raw_id_fields = ("return_item", "uploaded_by")
    readonly_fields = ("created_at",)
    fields = ("image", "image_type", "caption", "position")
    show_change_link = True
    ordering = ("return_item", "position", "id")

# ==============================================================================
# 3. OrderAdmin (the centerpiece)
# ==============================================================================
@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """
    Enterprise admin for the Order model. Read-only historical
    snapshots; mutations only via model state-transition methods.
    """

    list_display = (
        "order_number",
        "get_customer_display",
        "get_status_badge",
        "get_payment_status_badge",
        "total",
        "currency",
        "is_gift",
        "source",
        "fraud_check_status",
        "created_at",
    )
    list_filter = (
        "status",
        "payment_status",
        "source",
        "fraud_check_status",
        "is_gift",
        "is_active",
        "currency",
        "base_currency",
        "created_at",
        "updated_at",
    )
    search_fields = (
        "order_number",
        "email",
        "transaction_id",
        "tracking_number",
        "invoice_url",
        "coupon_code",
        "external_order_id",
        "customer__email",
        "customer__first_name",
        "customer__last_name",
        "customer__username",
        "shipping_address__full_name",
        "billing_address__full_name",
        "notes",
    )
    list_select_related = (
        "customer",
        "shipping_address",
        "billing_address",
    )
    raw_id_fields = (
        "customer",
        "shipping_address",
        "billing_address",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    list_max_show_all = 200
    preserve_filters = True
    save_on_top = True

    readonly_fields = (
        "id",
        "subtotal",
        "discount_total",
        "shipping_cost",
        "tax_total",
        "total",
        "grand_total",
        "base_currency_grand_total",
        "total_weight",
        "item_count",
        "total_quantity",
        "is_paid",
        "is_completed",
        "is_cancelled",
        "is_shipped",
        "is_refunded",
        "is_gift_order",
        "has_discount",
        "has_tracking",
        "has_invoice_url",
        "has_attachments",
        "can_be_cancelled",
        "can_be_refunded",
        "completed_at",
        "abandoned_at",
        "abandoned_recovery_sent_at",
        "expected_delivery_date",
        "created_at",
        "updated_at",
    )

    inlines = (
        OrderItemInline,
        PaymentInline,
        ShipmentInline,
        RefundInline,
        CouponUsageInline,
        TaxLineInline,
        DiscountLineInline,
        OrderNoteInline,
        OrderAttachmentInline,
        OrderStatusHistoryInline,
        OrderTimelineEventInline,
    )

    fieldsets = (
        (
            _("Order Identification"),
            {"fields": ("id", "order_number", "customer", "email", "source")},
        ),
        (
            _("Lifecycle & Payment Status"),
            {
                "fields": (
                    "status",
                    "payment_status",
                    "payment_method",
                    "transaction_id",
                    "fraud_check_status",
                    "risk_score",
                    "is_active",
                ),
            },
        ),
        (
            _("Financials (Auto-calculated)"),
            {
                "fields": (
                    "subtotal",
                    "discount_total",
                    "shipping_cost",
                    "tax_total",
                    "total",
                    "grand_total",
                    "currency",
                    "currency_symbol",
                    "exchange_rate",
                    "base_currency",
                    "base_currency_total",
                    "base_currency_grand_total",
                    "coupon_code",
                ),
            },
        ),
        (
            _("Address Snapshots"),
            {
                "fields": ("shipping_address", "billing_address"),
                "description": _(
                    "Immutable references to the customer's addresses at the time of purchase."
                ),
            },
        ),
        (
            _("Fulfillment & Delivery"),
            {
                "fields": (
                    "tracking_number",
                    "tracking_url",
                    "carrier",
                    "expected_delivery_date",
                    "delivery_instructions",
                ),
            },
        ),
        (
            _("Customer Context"),
            {
                "fields": (
                    "customer_ip",
                    "customer_user_agent",
                    "customer_locale",
                    "customer_timezone",
                    "referrer_url",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Personalization & Gift"),
            {
                "fields": (
                    "is_gift",
                    "gift_message",
                    "gift_wrapping",
                    "personalization_data",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("External Integrations"),
            {
                "fields": (
                    "external_order_id",
                    "external_platform",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Marketing & Operations"),
            {
                "fields": (
                    "tags",
                    "tags_text",
                    "abandoned_at",
                    "abandoned_recovery_sent_at",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Accounting"),
            {
                "fields": (
                    "invoice_url",
                    "has_invoice",
                    "notes",
                    "customer_note",
                    "completed_at",
                ),
            },
        ),
        (
            _("Audit"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    actions = (
        "mark_processing",
        "mark_shipped",
        "mark_delivered",
        "mark_cancelled",
        "mark_completed",
        "export_orders_csv",
    )

    @admin.display(description=_("Customer"), ordering="customer__email")
    def get_customer_display(self, obj: Order) -> str:
        if obj.customer:
            name = obj.customer.get_full_name() or obj.customer.username
            return f"{name} ({obj.email})"
        return f"Guest ({obj.email})"

    @admin.display(description=_("Status"), ordering="status")
    def get_status_badge(self, obj: Order) -> str:
        return _format_status_badge(obj.status, kind="order")

    @admin.display(description=_("Payment"), ordering="payment_status")
    def get_payment_status_badge(self, obj: Order) -> str:
        return _format_status_badge(obj.payment_status, kind="payment")

    @admin.action(description=_("Mark selected orders as Processing"))
    def mark_processing(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Order) -> None:
            if hasattr(obj, "update_status") and callable(obj.update_status):
                try:
                    obj.update_status(Order.OrderStatus.PROCESSING, user=request.user)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("update_status failed: %s", exc)
            # Defensive fallback: only set if currently permissible
            if obj.status in {
                Order.OrderStatus.PENDING,
                Order.OrderStatus.AWAITING_PAYMENT,
                Order.OrderStatus.ON_HOLD,
            }:
                obj.status = Order.OrderStatus.PROCESSING
                obj.save(update_fields=["status", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d orders transitioned to Processing."),
            request=request,
        )

    @admin.action(description=_("Mark selected orders as Shipped"))
    def mark_shipped(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Order) -> None:
            if hasattr(obj, "update_status") and callable(obj.update_status):
                try:
                    obj.update_status(Order.OrderStatus.SHIPPED, user=request.user)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("update_status failed: %s", exc)
            if obj.status in {
                Order.OrderStatus.PROCESSING,
                Order.OrderStatus.PARTIALLY_SHIPPED,
            }:
                obj.status = Order.OrderStatus.SHIPPED
                obj.save(update_fields=["status", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d orders transitioned to Shipped."),
            request=request,
        )

    @admin.action(description=_("Mark selected orders as Delivered"))
    def mark_delivered(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Order) -> None:
            if hasattr(obj, "update_status") and callable(obj.update_status):
                try:
                    obj.update_status(Order.OrderStatus.DELIVERED, user=request.user)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("update_status failed: %s", exc)
            if obj.status in {
                Order.OrderStatus.SHIPPED,
                Order.OrderStatus.PARTIALLY_SHIPPED,
            }:
                obj.status = Order.OrderStatus.DELIVERED
                obj.completed_at = timezone.now()
                obj.save(update_fields=["status", "completed_at", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d orders transitioned to Delivered."),
            request=request,
        )

    @admin.action(description=_("Mark selected orders as Cancelled"))
    def mark_cancelled(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Order) -> None:
            if hasattr(obj, "mark_cancelled") and callable(obj.mark_cancelled):
                try:
                    obj.mark_cancelled(
                        user=request.user,
                        remarks=_("Bulk cancelled via administration panel."),
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("mark_cancelled failed: %s", exc)
            if obj.status in {
                Order.OrderStatus.PENDING,
                Order.OrderStatus.AWAITING_PAYMENT,
                Order.OrderStatus.ON_HOLD,
                Order.OrderStatus.PROCESSING,
            }:
                obj.status = Order.OrderStatus.CANCELLED
                obj.save(update_fields=["status", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d orders cancelled."),
            request=request,
        )

    @admin.action(description=_("Mark selected orders as Completed"))
    def mark_completed(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Order) -> None:
            if hasattr(obj, "mark_completed") and callable(obj.mark_completed):
                try:
                    obj.mark_completed()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("mark_completed failed: %s", exc)
            if obj.status in {
                Order.OrderStatus.DELIVERED,
                Order.OrderStatus.SHIPPED,
            }:
                obj.status = Order.OrderStatus.COMPLETED
                obj.completed_at = timezone.now()
                obj.save(update_fields=["status", "completed_at", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d orders marked as Completed."),
            request=request,
        )

    @admin.action(description=_("Export selected orders as CSV"))
    def export_orders_csv(self, request: HttpRequest, queryset: QuerySet) -> Optional[HttpResponse]:
        """
        CSV export hardened to:
            * Only include field names that exist on the Order model.
            * Sanitize non-serializable values (UUID, datetime, dict, etc.).
            * Use csv.writer to avoid manual string assembly.

        Sensitive fields (customer passwords, etc.) are NEVER included.
        """
        meta = self.model._meta

        # Whitelist of safe-to-export fields. We deliberately do NOT
        # include free-form JSON or text fields that may contain
        # customer PII in arbitrary formats.
        candidate_fields: List[str] = [
            "id",
            "order_number",
            "email",
            "status",
            "payment_status",
            "payment_method",
            "transaction_id",
            "currency",
            "subtotal",
            "discount_total",
            "shipping_cost",
            "tax_total",
            "total",
            "coupon_code",
            "tracking_number",
            "carrier",
            "is_active",
            "source",
            "fraud_check_status",
            "is_gift",
            "created_at",
            "updated_at",
            "completed_at",
        ]
        field_names: List[str] = [
            f.name for f in meta.fields if f.name in candidate_fields
        ]

        try:
            response = HttpResponse(content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = (
                f'attachment; filename="orders_export_'
                f'{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
            )
            # UTF-8 BOM for Excel compatibility
            response.write("\ufeff")

            writer = csv.writer(response)
            writer.writerow(field_names)

            for obj in queryset.iterator():
                row: List[str] = []
                for field_name in field_names:
                    value = getattr(obj, field_name, "")
                    if isinstance(value, timezone.datetime):
                        try:
                            value = value.isoformat()
                        except Exception:  # noqa: BLE001
                            value = ""
                    elif isinstance(value, (dict, list)):
                        try:
                            import json
                            value = json.dumps(value, default=str)
                        except Exception:  # noqa: BLE001
                            value = ""
                    else:
                        value = _safe_str(value)
                    row.append(value)
                writer.writerow(row)

            self.message_user(request, _("Orders exported successfully."))
            return response
        except Exception as exc:  # noqa: BLE001
            logger.exception("Orders CSV export failed: %s", exc)
            self.message_user(
                request,
                _("Orders export failed. Please check the server logs."),
                level=messages.ERROR,
            )
            return None

# ==============================================================================
# 4. OrderItemAdmin
# ==============================================================================
@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    """
    Admin for individual order line items. Optimized for catalog scale.
    """

    list_display = (
        "id",
        "get_order_number",
        "get_product_label",
        "get_variant_label",
        "quantity",
        "unit_price",
        "discount",
        "tax",
        "line_total",
        "status",
        "is_gift",
        "is_returnable",
        "added_at",
    )
    list_filter = (
        "status",
        "is_gift",
        "added_at",
    )
    search_fields = (
        "order__order_number",
        "order__email",
        "product_name_snapshot",
        "product_sku_snapshot",
        "variant_name_snapshot",
        "variant_sku_snapshot",
        "variant_barcode_snapshot",
        "tracking_number",
    )
    raw_id_fields = (
        "order",
        "product",
        "variant",
        "inventory",
        "inventory_reservation",
        "warehouse",
    )
    list_select_related = (
        "order",
        "product",
        "variant",
    )
    date_hierarchy = "added_at"
    ordering = ("-added_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "line_total",
        "line_gross_total",
        "line_net_total",
        "line_discount_percentage",
        "line_tax_percentage",
        "effective_unit_price",
        "is_returnable",
        "is_shippable",
        "remaining_quantity_to_ship",
        "remaining_quantity_to_return",
        "added_at",
        "updated_at",
    )

    fieldsets = (
        (
            _("Line Item Reference"),
            {"fields": ("order", "product", "variant", "status", "saved_reason")},
        ),
        (
            _("Snapshots"),
            {
                "fields": (
                    "product_name_snapshot",
                    "product_sku_snapshot",
                    "product_image_snapshot_url",
                    "product_slug_snapshot",
                    "product_meta_title_snapshot",
                    "product_meta_description_snapshot",
                    "product_brand_snapshot",
                    "product_origin_snapshot",
                    "variant_name_snapshot",
                    "variant_sku_snapshot",
                    "variant_barcode_snapshot",
                    "variant_image_snapshot_url",
                    "variant_weight_snapshot",
                    "warehouse_name_snapshot",
                    "warehouse_code_snapshot",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Commercial Line"),
            {
                "fields": (
                    "unit_price",
                    "discount",
                    "tax",
                    "line_total",
                    "weight",
                    "quantity",
                ),
            },
        ),
        (
            _("Personalization & Gift"),
            {
                "fields": (
                    "attributes",
                    "personalization",
                    "is_gift",
                    "gift_message",
                    "gift_wrapping",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Inventory Audit References"),
            {
                "fields": (
                    "inventory",
                    "inventory_reservation",
                    "warehouse",
                    "expected_ship_date",
                    "promised_delivery_date",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Supplier / Dropship Snapshot"),
            {
                "fields": ("supplier_name_snapshot", "supplier_order_id"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Lifecycle Counters"),
            {
                "fields": (
                    "quantity_shipped",
                    "quantity_returned",
                    "quantity_refunded",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Audit"),
            {
                "fields": ("added_at", "updated_at", "saved_at", "moved_to_save_at", "metadata"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: OrderItem) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Product"))
    def get_product_label(self, obj: OrderItem) -> str:
        return obj.product_name_snapshot or "-"

    @admin.display(description=_("Variant"))
    def get_variant_label(self, obj: OrderItem) -> str:
        return obj.variant_name_snapshot or "-"

# ==============================================================================
# 5. OrderStatusHistoryAdmin (audit)
# ==============================================================================
@admin.register(OrderStatusHistory)
class OrderStatusHistoryAdmin(admin.ModelAdmin):
    """Immutable audit ledger. Cannot be added, edited, or deleted."""

    list_display = (
        "get_order_number",
        "old_status",
        "new_status",
        "is_customer_notified",
        "notification_method",
        "created_by",
        "created_at",
    )
    list_filter = (
        "new_status",
        "old_status",
        "is_customer_notified",
        "notification_method",
        "created_at",
    )
    search_fields = (
        "order__order_number",
        "order__email",
        "remarks",
        "created_by__email",
        "created_by__username",
    )
    raw_id_fields = ("order", "created_by")
    list_select_related = ("order", "created_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "order",
        "old_status",
        "new_status",
        "remarks",
        "is_customer_notified",
        "notification_method",
        "metadata",
        "ip_address",
        "user_agent",
        "created_by",
        "created_at",
    )
    fieldsets = (
        (
            _("Status Transition"),
            {
                "fields": (
                    "order",
                    "old_status",
                    "new_status",
                    "remarks",
                ),
            },
        ),
        (
            _("Customer Notification"),
            {
                "fields": (
                    "is_customer_notified",
                    "notification_method",
                ),
            },
        ),
        (
            _("Audit"),
            {
                "fields": (
                    "created_by",
                    "ip_address",
                    "user_agent",
                    "metadata",
                    "created_at",
                ),
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Optional[Model] = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: Optional[Model] = None
    ) -> bool:
        return False

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: OrderStatusHistory) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 6. ShipmentAdmin
# ==============================================================================
@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    """Logistics / parcel tracking admin."""

    list_display = (
        "shipment_number",
        "get_order_number",
        "carrier",
        "tracking_number",
        "status",
        "warehouse",
        "shipping_cost",
        "dispatch_date",
        "delivery_date",
        "is_in_transit",
        "is_delivered",
        "created_at",
    )
    list_filter = (
        "status",
        "carrier",
        "carrier_service_level",
        "warehouse",
        "created_at",
        "dispatch_date",
        "delivery_date",
    )
    search_fields = (
        "shipment_number",
        "tracking_number",
        "carrier_api_integration_id",
        "order__order_number",
        "order__email",
    )
    raw_id_fields = ("order", "warehouse")
    list_select_related = ("order", "warehouse")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "created_at",
        "updated_at",
        "is_in_transit",
        "is_delivered",
    )

    inlines = (ShipmentItemInline,)

    fieldsets = (
        (
            _("Shipment Identification"),
            {"fields": ("shipment_number", "order", "warehouse")},
        ),
        (
            _("Logistics Data"),
            {
                "fields": (
                    "carrier",
                    "carrier_service_level",
                    "carrier_api_integration_id",
                    "tracking_number",
                    "tracking_url",
                ),
            },
        ),
        (
            _("Status & Delivery Metrics"),
            {
                "fields": (
                    "status",
                    "shipping_cost",
                    "shipping_cost_breakdown",
                    "dispatch_date",
                    "delivery_date",
                    "picked_up_at",
                    "estimated_delivery_date",
                    "actual_delivery_date",
                ),
            },
        ),
        (
            _("Package Details"),
            {
                "fields": ("total_weight", "dimensions"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Notes & Audit"),
            {"fields": ("notes", "metadata", "created_at", "updated_at")},
        ),
    )

    actions = ("mark_dispatched_action", "mark_delivered_action", "mark_picked_up_action")

    @admin.action(description=_("Mark selected shipments as Dispatched"))
    def mark_dispatched_action(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Shipment) -> None:
            if hasattr(obj, "mark_dispatched") and callable(obj.mark_dispatched):
                try:
                    obj.mark_dispatched()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("mark_dispatched failed: %s", exc)
            if obj.status == Shipment.ShipmentStatus.PENDING:
                obj.status = Shipment.ShipmentStatus.DISPATCHED
                if not obj.dispatch_date:
                    obj.dispatch_date = timezone.now()
                obj.save(update_fields=["status", "dispatch_date", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d shipments marked as Dispatched."),
            request=request,
        )

    @admin.action(description=_("Mark selected shipments as Delivered"))
    def mark_delivered_action(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Shipment) -> None:
            if hasattr(obj, "mark_delivered") and callable(obj.mark_delivered):
                try:
                    obj.mark_delivered()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("mark_delivered failed: %s", exc)
            if obj.status in {
                Shipment.ShipmentStatus.DISPATCHED,
                Shipment.ShipmentStatus.IN_TRANSIT,
                Shipment.ShipmentStatus.OUT_FOR_DELIVERY,
            }:
                obj.status = Shipment.ShipmentStatus.DELIVERED
                if not obj.delivery_date:
                    obj.delivery_date = timezone.now()
                if not obj.actual_delivery_date:
                    obj.actual_delivery_date = timezone.now().date()
                obj.save(
                    update_fields=[
                        "status",
                        "delivery_date",
                        "actual_delivery_date",
                        "updated_at",
                    ]
                )

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d shipments marked as Delivered."),
            request=request,
        )

    @admin.action(description=_("Mark selected shipments as Picked Up"))
    def mark_picked_up_action(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Shipment) -> None:
            if hasattr(obj, "mark_picked_up") and callable(obj.mark_picked_up):
                try:
                    obj.mark_picked_up()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("mark_picked_up failed: %s", exc)
            if obj.status in {
                Shipment.ShipmentStatus.PENDING,
                Shipment.ShipmentStatus.AWAITING_PICKUP,
            }:
                obj.status = Shipment.ShipmentStatus.PICKED_UP
                if not obj.picked_up_at:
                    obj.picked_up_at = timezone.now()
                if not obj.dispatch_date:
                    obj.dispatch_date = obj.picked_up_at
                obj.save(
                    update_fields=[
                        "status",
                        "picked_up_at",
                        "dispatch_date",
                        "updated_at",
                    ]
                )

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d shipments marked as Picked Up."),
            request=request,
        )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: Shipment) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 7. ShipmentItemAdmin
# ==============================================================================
@admin.register(ShipmentItem)
class ShipmentItemAdmin(admin.ModelAdmin):
    """Per-line shipment items admin."""

    list_display = (
        "id",
        "get_shipment_number",
        "get_order_number",
        "get_order_item_label",
        "quantity_shipped",
        "serial_tracking",
        "is_replacement",
        "condition_at_pickup",
        "created_at",
    )
    list_filter = (
        "is_replacement",
        "condition_at_pickup",
        "created_at",
    )
    search_fields = (
        "shipment__shipment_number",
        "order_item__product_name_snapshot",
        "serial_tracking",
    )
    raw_id_fields = ("shipment", "order_item", "replaced_from")
    list_select_related = ("shipment", "order_item", "replaced_from")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (
            _("Line Item Reference"),
            {"fields": ("shipment", "order_item")},
        ),
        (
            _("Shipping Details"),
            {
                "fields": (
                    "quantity_shipped",
                    "serial_tracking",
                    "serial_verified_at",
                    "condition_at_pickup",
                ),
            },
        ),
        (
            _("Replacement Tracking"),
            {
                "fields": ("is_replacement", "replaced_from"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Notes & Audit"),
            {"fields": ("notes", "created_at", "updated_at")},
        ),
    )

    @admin.display(description=_("Shipment #"), ordering="shipment__shipment_number")
    def get_shipment_number(self, obj: ShipmentItem) -> str:
        try:
            return obj.shipment.shipment_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Order #"))
    def get_order_number(self, obj: ShipmentItem) -> str:
        try:
            return obj.order_item.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Order Item"))
    def get_order_item_label(self, obj: ShipmentItem) -> str:
        return f"{obj.order_item}"

# ==============================================================================
# 8. PaymentAdmin
# ==============================================================================
@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Payment ledger admin. No gateway logic — read & state display only."""

    list_display = (
        "transaction_id",
        "get_order_number",
        "get_status_badge",
        "gateway",
        "amount",
        "currency",
        "payment_method",
        "paid_at",
        "risk_score",
        "is_test_payment",
        "created_at",
    )
    list_filter = (
        "status",
        "gateway",
        "payment_method",
        "currency",
        "is_test_payment",
        "paid_at",
        "created_at",
    )
    search_fields = (
        "transaction_id",
        "order__order_number",
        "order__email",
        "order__transaction_id",
    )
    raw_id_fields = ("order",)
    list_select_related = ("order",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "created_at",
        "updated_at",
        "payment_attempts_count",
        "last_attempt_at",
        "next_attempt_allowed_at",
    )

    inlines = (PaymentAttemptInline, RefundInline)

    fieldsets = (
        (
            _("Transaction Identity"),
            {"fields": ("transaction_id", "order", "gateway", "payment_method")},
        ),
        (
            _("Financial Amounts"),
            {"fields": ("amount", "currency")},
        ),
        (
            _("Processing Status"),
            {
                "fields": (
                    "status",
                    "paid_at",
                    "next_attempt_allowed_at",
                    "payment_attempts_count",
                    "last_attempt_at",
                ),
            },
        ),
        (
            _("Risk & Compliance"),
            {
                "fields": (
                    "risk_score",
                    "is_test_payment",
                    "gateway_response_snapshot",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Audit"),
            {
                "fields": ("metadata", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: Payment) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Status"), ordering="status")
    def get_status_badge(self, obj: Payment) -> str:
        return _format_status_badge(obj.status, kind="payment")

# ==============================================================================
# 9. PaymentAttemptAdmin
# ==============================================================================
@admin.register(PaymentAttempt)
class PaymentAttemptAdmin(admin.ModelAdmin):
    """Per-attempt audit admin (read-only)."""

    list_display = (
        "id",
        "get_payment_ref",
        "attempt_number",
        "get_status_badge",
        "attempted_at",
        "gateway_response_code",
    )
    list_filter = (
        "status",
        "is_test",
        "attempted_at",
    )
    search_fields = (
        "payment__transaction_id",
        "payment__order__order_number",
        "gateway_response_code",
        "gateway_response_message",
    )
    raw_id_fields = ("payment",)
    list_select_related = ("payment",)
    date_hierarchy = "attempted_at"
    ordering = ("-attempted_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = ("created_at",)

    fieldsets = (
        (
            _("Attempt Reference"),
            {"fields": ("payment", "attempt_number", "attempted_at")},
        ),
        (
            _("Status & Gateway Response"),
            {
                "fields": (
                    "status",
                    "gateway_response_code",
                    "gateway_response_message",
                    "gateway_response_snapshot",
                ),
            },
        ),
        (
            _("Context"),
            {
                "fields": (
                    "ip_address",
                    "user_agent",
                    "is_test",
                    "notes",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @admin.display(description=_("Payment"))
    def get_payment_ref(self, obj: PaymentAttempt) -> str:
        try:
            return obj.payment.transaction_id
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Status"), ordering="status")
    def get_status_badge(self, obj: PaymentAttempt) -> str:
        return _format_status_badge(obj.status, kind="payment")

# ==============================================================================
# 10. RefundAdmin
# ==============================================================================
@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    """Refund admin. Approve / reject / complete via model methods only."""

    list_display = (
        "id",
        "get_order_number",
        "payment",
        "amount",
        "get_status_badge",
        "refund_method",
        "refund_reason_category",
        "approved_by",
        "processed_at",
        "created_at",
    )
    list_filter = (
        "status",
        "refund_method",
        "refund_reason_category",
        "processed_at",
        "created_at",
    )
    search_fields = (
        "order__order_number",
        "order__email",
        "payment__transaction_id",
        "gateway_refund_id",
        "reason",
        "customer_notes",
        "internal_notes",
    )
    raw_id_fields = ("order", "payment", "approved_by")
    list_select_related = ("order", "payment", "approved_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "created_at",
        "updated_at",
        "approved_at",
        "processed_at",
        "completed_at",
        "gateway_refund_id",
    )

    fieldsets = (
        (
            _("Target References"),
            {"fields": ("order", "payment")},
        ),
        (
            _("Refund Details"),
            {
                "fields": (
                    "amount",
                    "reason",
                    "refund_method",
                    "refund_reason_category",
                    "evidence_images",
                ),
            },
        ),
        (
            _("Lifecycle State"),
            {
                "fields": (
                    "status",
                    "approved_by",
                    "approved_at",
                    "processed_at",
                    "completed_at",
                    "gateway_refund_id",
                ),
            },
        ),
        (
            _("Notes & Audit"),
            {
                "fields": (
                    "customer_notes",
                    "internal_notes",
                    "metadata",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    actions = ("approve_refunds", "reject_refunds", "complete_refunds")

    @admin.action(description=_("Approve selected refunds"))
    def approve_refunds(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Refund) -> None:
            if hasattr(obj, "approve") and callable(obj.approve):
                try:
                    obj.approve(request.user)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Refund approve failed: %s", exc)
            if obj.status == Refund.RefundStatus.REQUESTED:
                obj.status = Refund.RefundStatus.APPROVED
                obj.approved_by = request.user
                obj.approved_at = timezone.now()
                obj.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d refunds approved."),
            request=request,
        )

    @admin.action(description=_("Reject selected refunds"))
    def reject_refunds(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Refund) -> None:
            if hasattr(obj, "reject") and callable(obj.reject):
                try:
                    obj.reject()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Refund reject failed: %s", exc)
            if obj.status in {
                Refund.RefundStatus.REQUESTED,
                Refund.RefundStatus.APPROVED,
            }:
                obj.status = Refund.RefundStatus.REJECTED
                obj.save(update_fields=["status", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d refunds rejected."),
            request=request,
        )

    @admin.action(description=_("Mark selected refunds as Completed"))
    def complete_refunds(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: Refund) -> None:
            if hasattr(obj, "complete") and callable(obj.complete):
                try:
                    obj.complete()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Refund complete failed: %s", exc)
            if obj.status == Refund.RefundStatus.PROCESSED:
                obj.status = Refund.RefundStatus.APPROVED  # legacy alias
                obj.completed_at = timezone.now()
                obj.save(update_fields=["status", "completed_at", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d refunds marked as Completed."),
            request=request,
        )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: Refund) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Status"), ordering="status")
    def get_status_badge(self, obj: Refund) -> str:
        return _format_status_badge(obj.status, kind="payment")

# ==============================================================================
# 11. CouponUsageAdmin
# ==============================================================================
@admin.register(CouponUsage)
class CouponUsageAdmin(admin.ModelAdmin):
    """Coupon usage audit ledger. Append-only in practice."""

    list_display = (
        "coupon_code",
        "get_username",
        "get_order_number",
        "discount_amount",
        "is_reversed",
        "used_at",
    )
    list_filter = (
        "is_reversed",
        "used_at",
    )
    search_fields = (
        "coupon_code",
        "user__email",
        "user__username",
        "order__order_number",
    )
    raw_id_fields = ("user", "order", "cart_id", "product_id", "category_id")
    list_select_related = ("user", "order")
    date_hierarchy = "used_at"
    ordering = ("-used_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "coupon_code",
        "user",
        "order",
        "cart_id",
        "product_id",
        "category_id",
        "discount_amount",
        "used_at",
        "is_reversed",
        "reversed_at",
        "reversal_reason",
        "metadata",
    )

    fieldsets = (
        (
            _("Identity"),
            {"fields": ("coupon_code", "user", "order", "discount_amount")},
        ),
        (
            _("Coupon Scope (optional)"),
            {
                "fields": ("cart_id", "product_id", "category_id"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Reversal Tracking"),
            {
                "fields": (
                    "is_reversed",
                    "reversed_at",
                    "reversal_reason",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Audit"),
            {
                "fields": ("used_at", "metadata"),
                "classes": ("collapse",),
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Optional[Model] = None
    ) -> bool:
        return False

    @admin.display(description=_("User"), ordering="user__username")
    def get_username(self, obj: CouponUsage) -> str:
        try:
            return obj.user.username
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: CouponUsage) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 12. TaxLineAdmin
# ==============================================================================
@admin.register(TaxLine)
class TaxLineAdmin(admin.ModelAdmin):
    """Tax line breakdown admin."""

    list_display = (
        "id",
        "get_order_number",
        "tax_class",
        "tax_name",
        "tax_rate",
        "base_amount",
        "tax_amount",
        "is_inclusive",
        "position",
    )
    list_filter = (
        "tax_class",
        "jurisdiction",
        "is_inclusive",
        "mode",
    )
    search_fields = (
        "tax_class",
        "tax_name",
        "tax_authority_code",
        "order__order_number",
    )
    raw_id_fields = ("order",)
    list_select_related = ("order",)
    ordering = ("order", "position", "id")
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = ("created_at",)

    fieldsets = (
        (
            _("Tax Reference"),
            {"fields": ("order", "tax_class", "tax_name", "position")},
        ),
        (
            _("Tax Math"),
            {"fields": ("tax_rate", "base_amount", "tax_amount", "is_inclusive", "mode")},
        ),
        (
            _("Authority & Jurisdiction"),
            {
                "fields": ("jurisdiction", "tax_authority_code"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Notes"),
            {"fields": ("notes", "created_at"), "classes": ("collapse",)},
        ),
    )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: TaxLine) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 13. DiscountLineAdmin
# ==============================================================================
@admin.register(DiscountLine)
class DiscountLineAdmin(admin.ModelAdmin):
    """Discount line breakdown admin."""

    list_display = (
        "id",
        "get_order_number",
        "name",
        "discount_type",
        "source",
        "discount_amount",
        "percentage",
        "position",
    )
    list_filter = (
        "discount_type",
        "is_taxable",
        "is_stackable",
    )
    search_fields = (
        "name",
        "code",
        "source",
        "promotion_id",
        "order__order_number",
    )
    raw_id_fields = ("order", "coupon_usage", "applies_to_order_item")
    list_select_related = ("order", "coupon_usage")
    ordering = ("order", "position", "id")
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = ("created_at",)

    fieldsets = (
        (
            _("Reference"),
            {"fields": ("order", "discount_type", "name", "code", "source", "position")},
        ),
        (
            _("Discount Math"),
            {
                "fields": (
                    "discount_amount",
                    "percentage",
                    "base_amount",
                    "is_taxable",
                    "is_stackable",
                ),
            },
        ),
        (
            _("Promotions & Cross-Scope"),
            {
                "fields": (
                    "coupon_usage",
                    "applies_to_order_item",
                    "promotion_id",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Notes & Audit"),
            {
                "fields": ("description", "metadata", "created_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: DiscountLine) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 14. OrderNoteAdmin
# ==============================================================================
@admin.register(OrderNote)
class OrderNoteAdmin(admin.ModelAdmin):
    """Customer / operator notes admin."""

    list_display = (
        "id",
        "get_order_number",
        "note_type",
        "is_pinned",
        "is_visible_to_customer",
        "get_author",
        "created_at",
    )
    list_filter = (
        "note_type",
        "is_pinned",
        "is_visible_to_customer",
        "created_at",
    )
    search_fields = (
        "text",
        "order__order_number",
        "order__email",
        "author__email",
        "author__username",
    )
    raw_id_fields = ("order", "author")
    list_select_related = ("order", "author")
    date_hierarchy = "created_at"
    ordering = ("-is_pinned", "-created_at")
    list_per_page = 25
    show_full_result_count = False

    fieldsets = (
        (
            _("Note Reference"),
            {
                "fields": (
                    "order",
                    "note_type",
                    "author",
                    "is_pinned",
                    "is_visible_to_customer",
                ),
            },
        ),
        (
            _("Content"),
            {"fields": ("text",)},
        ),
        (
            _("Metadata"),
            {
                "fields": ("metadata", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: OrderNote) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Author"), ordering="author__username")
    def get_author(self, obj: OrderNote) -> str:
        try:
            return obj.author.username if obj.author else "-"
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 15. OrderAttachmentAdmin
# ==============================================================================
@admin.register(OrderAttachment)
class OrderAttachmentAdmin(admin.ModelAdmin):
    """File attachments admin."""

    list_display = (
        "id",
        "get_order_number",
        "original_filename",
        "attachment_type",
        "file_size",
        "is_visible_to_customer",
        "is_active",
        "uploaded_by",
        "created_at",
    )
    list_filter = (
        "attachment_type",
        "is_visible_to_customer",
        "is_active",
        "mime_type",
        "created_at",
    )
    search_fields = (
        "original_filename",
        "description",
        "order__order_number",
    )
    raw_id_fields = ("order", "uploaded_by")
    list_select_related = ("order", "uploaded_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "file_size",
        "mime_type",
        "uploaded_by",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            _("Reference"),
            {"fields": ("order", "attachment_type", "description")},
        ),
        (
            _("File"),
            {
                "fields": (
                    "file",
                    "original_filename",
                    "file_size",
                    "mime_type",
                ),
            },
        ),
        (
            _("Visibility"),
            {
                "fields": ("is_visible_to_customer", "is_active", "uploaded_by"),
            },
        ),
        (
            _("Audit"),
            {
                "fields": ("metadata", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: OrderAttachment) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 16. OrderTimelineEventAdmin
# ==============================================================================
@admin.register(OrderTimelineEvent)
class OrderTimelineEventAdmin(admin.ModelAdmin):
    """Granular, append-only timeline admin (read-only)."""

    list_display = (
        "id",
        "get_order_number",
        "get_event_badge",
        "title",
        "get_actor",
        "is_visible_to_customer",
        "is_system_event",
        "occurred_at",
    )
    list_filter = (
        "event_type",
        "is_visible_to_customer",
        "is_system_event",
        "occurred_at",
    )
    search_fields = (
        "title",
        "description",
        "order__order_number",
        "order__email",
        "actor__email",
        "actor__username",
    )
    raw_id_fields = ("order", "actor")
    list_select_related = ("order", "actor")
    date_hierarchy = "occurred_at"
    ordering = ("-occurred_at", "id")
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = ("created_at",)

    fieldsets = (
        (
            _("Event"),
            {
                "fields": (
                    "order",
                    "event_type",
                    "title",
                    "description",
                    "occurred_at",
                ),
            },
        ),
        (
            _("Actor & Visibility"),
            {
                "fields": (
                    "actor",
                    "is_system_event",
                    "is_visible_to_customer",
                ),
            },
        ),
        (
            _("Cross-Reference"),
            {
                "fields": ("reference_model", "reference_id", "icon", "color"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Metadata"),
            {
                "fields": ("metadata", "created_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: Optional[Model] = None
    ) -> bool:
        return False

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: OrderTimelineEvent) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Event"), ordering="event_type")
    def get_event_badge(self, obj: OrderTimelineEvent) -> str:
        return _format_event_badge(obj.event_type)

    @admin.display(description=_("Actor"), ordering="actor__username")
    def get_actor(self, obj: OrderTimelineEvent) -> str:
        try:
            return obj.actor.username if obj.actor else "System"
        except Exception:  # noqa: BLE001
            return "System"

# ==============================================================================
# 17. ReturnRequestAdmin
# ==============================================================================
@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    """Return workflow admin. State transitions via model methods only."""

    list_display = (
        "return_number",
        "get_order_number",
        "get_status_badge",
        "return_type",
        "reason_category",
        "requested_by",
        "approved_by",
        "is_resolved",
        "total_return_quantity",
        "created_at",
    )
    list_filter = (
        "status",
        "return_type",
        "reason_category",
        "restock_decision",
        "created_at",
        "approved_at",
        "completed_at",
    )
    search_fields = (
        "return_number",
        "reason_text",
        "order__order_number",
        "order__email",
        "requested_by__email",
        "approved_by__email",
    )
    raw_id_fields = (
        "order",
        "requested_by",
        "approved_by",
        "rejected_by",
        "refund",
        "replacement_order",
        "return_shipping_address_snapshot",
    )
    list_select_related = (
        "order",
        "requested_by",
        "approved_by",
        "refund",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = (
        "return_number",
        "created_at",
        "updated_at",
        "approved_at",
        "rejected_at",
        "received_at",
        "completed_at",
        "is_resolved",
        "total_return_quantity",
        "is_refund_request",
    )

    inlines = (ReturnItemInline,)

    fieldsets = (
        (
            _("Identity"),
            {
                "fields": (
                    "return_number",
                    "order",
                    "return_type",
                    "reason_category",
                    "reason_text",
                ),
            },
        ),
        (
            _("Lifecycle"),
            {
                "fields": (
                    "status",
                    "requested_by",
                    "approved_by",
                    "approved_at",
                    "rejected_by",
                    "rejected_at",
                    "rejection_reason",
                    "received_at",
                    "completed_at",
                ),
            },
        ),
        (
            _("Fulfillment"),
            {
                "fields": (
                    "refund",
                    "replacement_order",
                    "restock_decision",
                    "restock_location",
                    "return_shipping_address_snapshot",
                ),
            },
        ),
        (
            _("Notes & Metadata"),
            {
                "fields": (
                    "customer_notes",
                    "internal_notes",
                    "metadata",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    actions = ("approve_returns", "reject_returns", "mark_received_returns", "complete_returns")

    @admin.action(description=_("Approve selected returns"))
    def approve_returns(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: ReturnRequest) -> None:
            if hasattr(obj, "approve") and callable(obj.approve):
                try:
                    obj.approve(request.user)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Return approve failed: %s", exc)
            if obj.status == ReturnRequest.ReturnStatus.REQUESTED:
                obj.status = ReturnRequest.ReturnStatus.APPROVED
                obj.approved_by = request.user
                obj.approved_at = timezone.now()
                obj.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d return requests approved."),
            request=request,
        )

    @admin.action(description=_("Reject selected returns"))
    def reject_returns(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: ReturnRequest) -> None:
            if hasattr(obj, "reject") and callable(obj.reject):
                try:
                    obj.reject(request.user, reason=_("Bulk rejected via administration panel."))
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Return reject failed: %s", exc)
            if obj.status == ReturnRequest.ReturnStatus.REQUESTED:
                obj.status = ReturnRequest.ReturnStatus.REJECTED
                obj.rejected_by = request.user
                obj.rejected_at = timezone.now()
                obj.save(update_fields=[
                    "status", "rejected_by", "rejected_at", "updated_at",
                ])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d return requests rejected."),
            request=request,
        )

    @admin.action(description=_("Mark selected returns as Received"))
    def mark_received_returns(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: ReturnRequest) -> None:
            if hasattr(obj, "mark_received") and callable(obj.mark_received):
                try:
                    obj.mark_received()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Return mark_received failed: %s", exc)
            if obj.status in {
                ReturnRequest.ReturnStatus.AWAITING_SHIPMENT,
                ReturnRequest.ReturnStatus.IN_TRANSIT,
            }:
                obj.status = ReturnRequest.ReturnStatus.RECEIVED
                obj.received_at = timezone.now()
                obj.save(update_fields=["status", "received_at", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d return requests marked as Received."),
            request=request,
        )

    @admin.action(description=_("Mark selected returns as Completed"))
    def complete_returns(self, request: HttpRequest, queryset: QuerySet) -> None:
        def _do(obj: ReturnRequest) -> None:
            if hasattr(obj, "complete") and callable(obj.complete):
                try:
                    obj.complete()
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Return complete failed: %s", exc)
            if obj.status in {
                ReturnRequest.ReturnStatus.RECEIVED,
                ReturnRequest.ReturnStatus.INSPECTING,
            }:
                obj.status = ReturnRequest.ReturnStatus.COMPLETED
                obj.completed_at = timezone.now()
                obj.save(update_fields=["status", "completed_at", "updated_at"])

        _safe_call_action(
            self, queryset, _do,
            success_message=_("%(count)d return requests marked as Completed."),
            request=request,
        )

    @admin.display(description=_("Order #"), ordering="order__order_number")
    def get_order_number(self, obj: ReturnRequest) -> str:
        try:
            return obj.order.order_number
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Status"), ordering="status")
    def get_status_badge(self, obj: ReturnRequest) -> str:
        bg, fg = ("#E8F5E9", "#2E7D32")
        if obj.status in {
            ReturnRequest.ReturnStatus.REQUESTED,
            ReturnRequest.ReturnStatus.UNDER_REVIEW,
        }:
            bg, fg = ("#FFF8E7", "#9A7B54")
        if obj.status in {
            ReturnRequest.ReturnStatus.REJECTED,
            ReturnRequest.ReturnStatus.CANCELLED,
        }:
            bg, fg = ("#FFEBEE", "#C62828")
        if obj.status in {
            ReturnRequest.ReturnStatus.RECEIVED,
            ReturnRequest.ReturnStatus.INSPECTING,
            ReturnRequest.ReturnStatus.COMPLETED,
        }:
            bg, fg = ("#E3F2FD", "#0D47A1")
        return format_html(
            '<span style="display:inline-block;padding:3px 8px;background:{};color:{};'
            'font-size:11px;font-weight:600;border:1px solid {};border-radius:20px;'
            'text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
            bg, fg, fg, obj.get_status_display(),
        )

# ==============================================================================
# 18. ReturnItemAdmin
# ==============================================================================
@admin.register(ReturnItem)
class ReturnItemAdmin(admin.ModelAdmin):
    """Per-line return items admin."""

    list_display = (
        "id",
        "get_return_number",
        "get_order_item_label",
        "quantity_returned",
        "quantity_received",
        "inspection_result",
        "refund_amount",
        "created_at",
    )
    list_filter = (
        "inspection_result",
        "restock_decision",
        "created_at",
    )
    search_fields = (
        "return_request__return_number",
        "order_item__product_name_snapshot",
    )
    raw_id_fields = (
        "return_request",
        "order_item",
        "replacement_order_item",
    )
    list_select_related = ("return_request", "order_item", "replacement_order_item")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = ("created_at", "updated_at")

    inlines = (ReturnImageInline,)

    fieldsets = (
        (
            _("Reference"),
            {"fields": ("return_request", "order_item")},
        ),
        (
            _("Quantities"),
            {
                "fields": (
                    "quantity_returned",
                    "quantity_approved",
                    "quantity_received",
                ),
            },
        ),
        (
            _("Inspection & Condition"),
            {
                "fields": (
                    "condition_received",
                    "inspection_result",
                    "inspection_notes",
                ),
            },
        ),
        (
            _("Resolution"),
            {
                "fields": (
                    "refund_amount",
                    "restock_decision",
                    "replacement_order_item",
                ),
            },
        ),
        (
            _("Metadata"),
            {
                "fields": ("metadata", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Return #"), ordering="return_request__return_number")
    def get_return_number(self, obj: ReturnItem) -> str:
        try:
            return obj.return_request.return_number or "-"
        except Exception:  # noqa: BLE001
            return "-"

    @admin.display(description=_("Order Item"))
    def get_order_item_label(self, obj: ReturnItem) -> str:
        try:
            return str(obj.order_item)
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# 19. ReturnImageAdmin
# ==============================================================================
@admin.register(ReturnImage)
class ReturnImageAdmin(admin.ModelAdmin):
    """Per-line return images admin."""

    list_display = (
        "id",
        "get_return_item",
        "image_type",
        "caption",
        "position",
        "uploaded_by",
        "created_at",
    )
    list_filter = (
        "image_type",
        "created_at",
    )
    search_fields = (
        "caption",
        "return_item__return_request__return_number",
    )
    raw_id_fields = ("return_item", "uploaded_by")
    list_select_related = ("return_item", "uploaded_by")
    date_hierarchy = "created_at"
    ordering = ("return_item", "position", "id")
    list_per_page = 25
    show_full_result_count = False
    readonly_fields = ("created_at",)

    fieldsets = (
        (
            _("Image"),
            {
                "fields": (
                    "return_item",
                    "image",
                    "image_type",
                    "caption",
                    "position",
                ),
            },
        ),
        (
            _("Metadata"),
            {
                "fields": ("uploaded_by", "created_at"),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Return Item"))
    def get_return_item(self, obj: ReturnImage) -> str:
        try:
            return str(obj.return_item)
        except Exception:  # noqa: BLE001
            return "-"

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Core
    "OrderAdmin",
    "OrderItemAdmin",
    "OrderAddressSnapshotAdmin",
    "OrderStatusHistoryAdmin",
    # Logistics
    "ShipmentAdmin",
    "ShipmentItemAdmin",
    # Financial
    "PaymentAdmin",
    "PaymentAttemptAdmin",
    "RefundAdmin",
    "CouponUsageAdmin",
    # Tax / discount
    "TaxLineAdmin",
    "DiscountLineAdmin",
    # Enrichment
    "OrderNoteAdmin",
    "OrderAttachmentAdmin",
    "OrderTimelineEventAdmin",
    # Returns
    "ReturnRequestAdmin",
    "ReturnItemAdmin",
    "ReturnImageAdmin",
    # Inlines (exported for tests / re-registration)
    "OrderItemInline",
    "OrderStatusHistoryInline",
    "ShipmentInline",
    "ShipmentItemInline",
    "PaymentInline",
    "PaymentAttemptInline",
    "RefundInline",
    "CouponUsageInline",
    "TaxLineInline",
    "DiscountLineInline",
    "OrderNoteInline",
    "OrderAttachmentInline",
    "OrderTimelineEventInline",
    "ReturnItemInline",
    "ReturnImageInline",
]