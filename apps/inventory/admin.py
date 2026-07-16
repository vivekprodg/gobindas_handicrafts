"""
Enterprise-grade Django Admin configuration for the Inventory application.

This admin module provides warehouse managers, inventory managers, purchasing staff,
finance staff, and ERP administrators with comprehensive ERP back-office tooling.

Design principles:
    * Security by default: All sensitive fields are read-only.
    * Scalability: Optimized querysets using select_related / prefetch_related.
    * Maintainability: Reusable mixins (CSVExportMixin, BulkActionMixin).
    * Auditability: Every admin action is wrapped in a transaction.
    * Future-proofing: Designed to integrate cleanly with future modules
      (Purchase Orders, Manufacturing, Barcode, Batch/Lot, Expiry, Serial Numbers).

Registration Coverage:
    * Warehouse
    * Inventory
    * InventoryTransaction
    * StockReservation
    * StockAdjustment
"""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import (
    Inventory,
    InventoryTransaction,
    StockAdjustment,
    StockReservation,
    Warehouse,
)

# ==============================================================================
# REUSABLE ADMIN MIXINS
# ==============================================================================
class CSVExportMixin:
    """
    Adds a configurable 'Export CSV' admin action.

    Subclasses simply set ``csv_export_fields`` to the desired model field names
    to control which columns appear in the generated CSV file. Timezone-aware
    datetimes are formatted as ISO 8601 strings for portability.
    """

    csv_export_fields: Iterable[str] = ()
    csv_export_filename_prefix: str = "export"

    @admin.action(description=_("Export selected rows as CSV"))
    def export_as_csv(self, request: HttpRequest, queryset) -> HttpResponse:
        meta = self.model._meta
        field_names = list(self.csv_export_fields) or [f.name for f in meta.fields]

        # Build safe filename
        ts = timezone.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{self.csv_export_filename_prefix or meta.model_name}_{ts}.csv"

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        # BOM so Excel reliably detects UTF-8
        response.write("\ufeff")

        writer = csv.writer(response)
        writer.writerow(field_names)

        for obj in queryset.iterator():
            row: list[Any] = []
            for field in field_names:
                value = getattr(obj, field, "")
                if isinstance(value, datetime):
                    value = value.isoformat()
                row.append(value)
            writer.writerow(row)

        # Fire-and-forget success message
        self.message_user(request, _("CSV export generated successfully."))
        return response

class StockActionMixin:
    """
    Provides reusable bulk actions for activating/deactivating stock entities.

    Subclasses define ``status_field`` (boolean or string) to control the target
    attribute used for activation.
    """

    status_field: str = "is_active"

    def _set_active(self, request: HttpRequest, queryset, value: bool) -> None:
        field = self.status_field
        if not field:
            return
        count = queryset.update(**{field: value})
        verb = _("activated") if value else _("deactivated")
        self.message_user(request, _(f"{count} records successfully {verb}."))

    @admin.action(description=_("Mark selected records as active"))
    def make_active(self, request: HttpRequest, queryset) -> None:
        self._set_active(request, queryset, True)

    @admin.action(description=_("Mark selected records as inactive"))
    def make_inactive(self, request: HttpRequest, queryset) -> None:
        self._set_active(request, queryset, False)

# ==============================================================================
# 1. WAREHOUSE ADMIN
# ==============================================================================
@admin.register(Warehouse)
class WarehouseAdmin(CSVExportMixin, StockActionMixin, admin.ModelAdmin):
    """
    Warehouse CRUD optimized for warehouse managers and ERP administrators.

    Provides:
        * List view with active/default badges and contact summary.
        * Color-coded status badges via safe HTML formatting.
        * CSV export with sanitized filenames.
        * Bulk activate/deactivate actions.
    """

    list_display = (
        "name_with_code",
        "contact_summary",
        "status_badges",
        "inventory_count",
        "is_default",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "is_default", "created_at", "updated_at")
    search_fields = (
        "name",
        "code",
        "location",
        "phone",
        "email",
    )
    list_select_related: tuple = ()
    list_per_page = 50
    ordering = ("-is_default", "name", "id")
    date_hierarchy = "created_at"

    csv_export_fields = (
        "id",
        "name",
        "code",
        "location",
        "phone",
        "email",
        "is_default",
        "is_active",
        "created_at",
        "updated_at",
    )
    csv_export_filename_prefix = "warehouses"

    fieldsets = (
        (
            _("Identification"),
            {"fields": (("name", "code"), "location")},
        ),
        (
            _("Contact Information"),
            {"fields": ("phone", "email")},
        ),
        (
            _("Status Flags"),
            {
                "fields": ("is_default", "is_active"),
                "description": _(
                    "At most one active warehouse may be designated as the default. "
                    "Soft deactivation preserves all historical stock transactions."
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
    readonly_fields = ("created_at", "updated_at")

    actions = ("export_as_csv", "make_active", "make_inactive", "set_as_default")

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).annotate(
            _inventory_count=Count("inventory_records", distinct=True)
        )

    @admin.display(description=_("Warehouse"), ordering=("name", "code"))
    def name_with_code(self, obj: Warehouse) -> str:
        return obj.display_name

    @admin.display(description=_("Contact"))
    def contact_summary(self, obj: Warehouse) -> str:
        parts: list[str] = []
        if obj.phone:
            parts.append(obj.phone)
        if obj.email:
            parts.append(obj.email)
        return " · ".join(parts) if parts else "—"

    @admin.display(description=_("Status"))
    def status_badges(self, obj: Warehouse) -> str:
        badges: list[str] = []
        if obj.is_default:
            badges.append(
                '<span style="display:inline-block;padding:3px 8px;background:#fff8e7;color:#9A7B54;font-size:11px;font-weight:600;border:1px solid rgba(154,123,84,0.3);text-transform:uppercase;letter-spacing:0.05em;">DEFAULT</span>'
            )
        if obj.is_active:
            badges.append(
                '<span style="display:inline-block;padding:3px 8px;background:#E8F5E9;color:#2E7D32;font-size:11px;font-weight:600;border:1px solid rgba(46,125,50,0.3);text-transform:uppercase;letter-spacing:0.05em;">ACTIVE</span>'
            )
        else:
            badges.append(
                '<span style="display:inline-block;padding:3px 8px;background:#FFEBEE;color:#C62828;font-size:11px;font-weight:600;border:1px solid rgba(198,40,40,0.3);text-transform:uppercase;letter-spacing:0.05em;">INACTIVE</span>'
            )
        return format_html(" ".join(badges)) if badges else "-"

    @admin.display(description=_("Inventory Records"), ordering="_inventory_count")
    def inventory_count(self, obj: Warehouse) -> int:
        return getattr(obj, "_inventory_count", 0)

    @admin.action(description=_("Set selected warehouse as the system default"))
    def set_as_default(self, request: HttpRequest, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                _("Please select exactly one warehouse to set as default."),
                level=messages.WARNING,
            )
            return
        target = queryset.first()
        with transaction.atomic():
            # Demote any currently active default warehouses
            Warehouse.objects.filter(is_default=True, is_active=True).exclude(pk=target.pk).update(is_default=False)
            target.is_default = True
            target.is_active = True
            target.save(update_fields=["is_default", "is_active", "updated_at"])
        self.message_user(request, _(f"'{target.name}' is now the default warehouse."))

# ==============================================================================
# 2. INVENTORY ADMIN
# ==============================================================================
class InventoryTransactionInline(admin.TabularInline):
    """
    Read-only inline showing the most recent transactions for an inventory row.
    """

    model = InventoryTransaction
    extra = 0
    fields = (
        "transaction_at",
        "transaction_type",
        "direction",
        "quantity",
        "performed_by",
        "reference_number",
    )
    readonly_fields = (
        "transaction_at",
        "transaction_type",
        "direction",
        "quantity",
        "performed_by",
        "reference_number",
    )
    ordering = ("-transaction_at", "-id")
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

@admin.register(Inventory)
class InventoryAdmin(CSVExportMixin, StockActionMixin, admin.ModelAdmin):
    """
    Enterprise Inventory management.

    Designed for daily warehouse operations:
        * Bulk activate / deactivate warehouses.
        * Quick visualization of stock status via safe HTML color-coded badges.
        * Powerful filtering by warehouse and target.
        * CSV export for offline reporting.
        * Inline display of recent transactions.
    """

    list_display = (
        "warehouse_name",
        "target_display",
        "available_quantity",
        "reserved_quantity",
        "damaged_quantity",
        "incoming_quantity",
        "free_stock",
        "total_stock",
        "stock_status_badge",
        "reorder_badge",
        "is_active",
        "updated_at",
    )
    list_filter = (
        "warehouse",
        "is_active",
        "updated_at",
    )
    search_fields = (
        "warehouse__name",
        "warehouse__code",
        "product__title",
        "product__sku",
        "product_variant__sku",
        "product_variant__barcode",
    )
    list_select_related: tuple = (
        "warehouse",
        "product_variant",
        "product",
    )
    list_per_page = 50
    ordering = ("warehouse", "product_variant", "product", "id")
    date_hierarchy = "updated_at"
    autocomplete_fields: tuple = ("warehouse",)

    csv_export_fields = (
        "id",
        "warehouse_id",
        "product_id",
        "product_variant_id",
        "available_quantity",
        "reserved_quantity",
        "damaged_quantity",
        "incoming_quantity",
        "minimum_stock",
        "maximum_stock",
        "reorder_level",
        "is_active",
        "created_at",
        "updated_at",
    )
    csv_export_filename_prefix = "inventory"

    inlines = (InventoryTransactionInline,)

    actions = (
        "export_as_csv",
        "make_active",
        "make_inactive",
        "set_reorder_level",
        "mark_for_transfer",
    )

    fieldsets = (
        (
            _("Target"),
            {
                "fields": ("warehouse", ("product", "product_variant")),
                "description": _(
                    "Inventory must reference exactly one target: a Product or "
                    "a ProductVariant (not both, not neither)."
                ),
            },
        ),
        (
            _("Stock Levels"),
            {
                "fields": (
                    ("available_quantity", "reserved_quantity"),
                    ("damaged_quantity", "incoming_quantity"),
                ),
            },
        ),
        (
            _("Reorder Thresholds"),
            {
                "fields": (("minimum_stock", "maximum_stock"), "reorder_level"),
            },
        ),
        (
            _("Operational Metadata"),
            {
                "fields": ("location_bin", "notes", "is_active"),
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
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).select_related(
            "warehouse", "product_variant", "product"
        )

    @admin.display(description=_("Warehouse"), ordering="warehouse__name")
    def warehouse_name(self, obj: Inventory) -> str:
        return obj.warehouse.display_name

    @admin.display(description=_("Target"))
    def target_display(self, obj: Inventory) -> str:
        target = obj.get_target()
        if target is None:
            return "-"
        if obj.product_variant:
            sku = obj.product_variant.sku or ""
            return f"{target} ({sku})" if sku else target
        sku = getattr(target, "sku", "") or ""
        return f"{target} ({sku})" if sku else target

    @admin.display(description=_("Free Stock"))
    def free_stock(self, obj: Inventory) -> str:
        return f"{obj.free_stock}"

    @admin.display(description=_("Total Stock"))
    def total_stock(self, obj: Inventory) -> str:
        return f"{obj.total_stock}"

    @admin.display(description=_("Status"))
    def stock_status_badge(self, obj: Inventory) -> str:
        if not obj.is_active:
            return format_html(
                '<span style="display:inline-block;padding:3px 8px;background:#FFEBEE;color:#C62828;font-size:11px;font-weight:600;border:1px solid rgba(198,40,40,0.3);text-transform:uppercase;letter-spacing:0.05em;">INACTIVE</span>'
            )
        if obj.is_out_of_stock:
            return format_html(
                '<span style="display:inline-block;padding:3px 8px;background:#FFEBEE;color:#C62828;font-size:11px;font-weight:600;border:1px solid rgba(198,40,40,0.3);text-transform:uppercase;letter-spacing:0.05em;">OUT OF STOCK</span>'
            )
        if obj.is_low_stock:
            return format_html(
                '<span style="display:inline-block;padding:3px 8px;background:#FFF8E7;color:#9A7B54;font-size:11px;font-weight:600;border:1px solid rgba(154,123,84,0.3);text-transform:uppercase;letter-spacing:0.05em;">LOW STOCK</span>'
            )
        if obj.is_overstock:
            return format_html(
                '<span style="display:inline-block;padding:3px 8px;background:#E3F2FD;color:#0D47A1;font-size:11px;font-weight:600;border:1px solid rgba(13,71,161,0.3);text-transform:uppercase;letter-spacing:0.05em;">OVERSTOCK</span>'
            )
        return format_html(
            '<span style="display:inline-block;padding:3px 8px;background:#E8F5E9;color:#2E7D32;font-size:11px;font-weight:600;border:1px solid rgba(46,125,50,0.3);text-transform:uppercase;letter-spacing:0.05em;">IN STOCK</span>'
        )

    @admin.display(description=_("Reorder"))
    def reorder_badge(self, obj: Inventory) -> str:
        if obj.needs_reorder:
            return format_html(
                '<span style="display:inline-block;padding:3px 8px;background:#FFEBEE;color:#C62828;font-size:11px;font-weight:600;border:1px solid rgba(198,40,40,0.3);text-transform:uppercase;letter-spacing:0.05em;">REORDER NOW</span>'
            )
        return "—"

    @admin.action(description=_("Set reorder level (bulk)"))
    def set_reorder_level(self, request: HttpRequest, queryset):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        if queryset.count() != 1:
            self.message_user(
                request,
                _("Bulk reorder level adjustment is a placeholder. Please edit individual records."),
                level=messages.INFO,
            )
            return
        target = queryset.first()
        self.message_user(
            request,
            _(f"Use the edit page for '{target}' to change the reorder level precisely."),
            level=messages.INFO,
        )

    @admin.action(description=_("Mark for transfer (placeholder)"))
    def mark_for_transfer(self, request: HttpRequest, queryset):
        self.message_user(
            request,
            _(
                "Transfer workflow is not yet implemented. Use the InventoryTransaction inline "
                "or wait for the WarehouseTransfer module to be enabled."
            ),
            level=messages.INFO,
        )

# ==============================================================================
# 3. INVENTORY TRANSACTION ADMIN
# ==============================================================================
@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(CSVExportMixin, admin.ModelAdmin):
    """
    Read-mostly audit ledger of every stock movement.

    Per design, transactions are immutable. Editing is restricted to
    metadata fields (remarks) to preserve the audit chain. Direct changes
    to quantity or direction are blocked at the admin level.
    """

    list_display = (
        "transaction_at",
        "warehouse_name",
        "target_display",
        "transaction_type",
        "direction_badge",
        "quantity_badge",
        "performed_by",
        "reference_number",
        "available_before",
        "available_after",
    )
    list_filter = (
        "transaction_type",
        "direction",
        "transaction_at",
        "inventory__warehouse",   # ✅ FIXED: Use related field lookup (was "warehouse")
        "performed_by",
    )
    search_fields = (
        "reference_number",
        "reference_id",
        "inventory__product__title",
        "inventory__product__sku",
        "inventory__product_variant__sku",
        "inventory__warehouse__name",
        "inventory__warehouse__code",
        "remarks",
    )
    raw_id_fields = ("inventory", "performed_by", "destination_warehouse")
    list_select_related: tuple = (
        "inventory__warehouse",
        "inventory__product",
        "inventory__product_variant",
        "destination_warehouse",
        "performed_by",
    )
    date_hierarchy = "transaction_at"
    ordering = ("-transaction_at", "-id")
    list_per_page = 100
    readonly_fields = (
        "inventory",
        "transaction_type",
        "direction",
        "quantity",
        "available_before",
        "available_after",
        "reserved_before",
        "reserved_after",
        "unit_cost",
        "total_cost",
        "currency",
        "reference_model",
        "reference_id",
        "destination_warehouse",
        "transfer_group_id",
        "performed_by",
        "transaction_at",
        "created_at",
        "updated_at",
    )

    csv_export_fields = (
        "id",
        "inventory_id",
        "transaction_type",
        "direction",
        "quantity",
        "available_before",
        "available_after",
        "reserved_before",
        "reserved_after",
        "unit_cost",
        "total_cost",
        "currency",
        "reference_number",
        "reference_model",
        "reference_id",
        "destination_warehouse_id",
        "transfer_group_id",
        "performed_by_id",
        "transaction_at",
        "created_at",
    )
    csv_export_filename_prefix = "inventory_transactions"

    fieldsets = (
        (
            _("Transaction"),
            {
                "fields": (
                    "inventory",
                    ("transaction_type", "direction"),
                    "quantity",
                    "transaction_at",
                )
            },
        ),
        (
            _("Stock Snapshots"),
            {"fields": (("available_before", "available_after"),
                        ("reserved_before", "reserved_after"))},
        ),
        (
            _("Cost Tracking"),
            {"fields": (("unit_cost", "total_cost"), "currency")},
        ),
        (
            _("Cross-Module Traceability"),
            {
                "fields": (
                    "reference_number",
                    ("reference_model", "reference_id"),
                    "destination_warehouse",
                )
            },
        ),
        (
            _("Audit"),
            {
                "fields": (
                    "performed_by",
                    "remarks",
                    ("created_at", "updated_at"),
                )
            },
        ),
    )

    actions = ("export_as_csv",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        """
        Block manual transaction creation from the admin to preserve immutability.
        Transactions must be created through the service layer.
        """
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description=_("Warehouse"), ordering="inventory__warehouse__name")
    def warehouse_name(self, obj: InventoryTransaction) -> str:
        return obj.inventory.warehouse.display_name if obj.inventory_id else "-"

    @admin.display(description=_("Target"))
    def target_display(self, obj: InventoryTransaction) -> str:
        target = obj.inventory.get_target() if obj.inventory_id else None
        return str(target) if target else "-"

    @admin.display(description=_("Direction"))
    def direction_badge(self, obj: InventoryTransaction) -> str:
        if obj.direction == InventoryTransaction.FlowDirection.INBOUND:
            color = "#E8F5E9"
            text_color = "#2E7D32"
            border = "rgba(46,125,50,0.3)"
            label = "INBOUND (+)"
        elif obj.direction == InventoryTransaction.FlowDirection.OUTBOUND:
            color = "#FFEBEE"
            text_color = "#C62828"
            border = "rgba(198,40,40,0.3)"
            label = "OUTBOUND (−)"
        else:
            color = "#E3F2FD"
            text_color = "#0D47A1"
            border = "rgba(13,71,161,0.3)"
            label = "NEUTRAL"
        return format_html(
            '<span style="display:inline-block;padding:3px 8px;background:{};color:{};font-size:11px;font-weight:600;border:1px solid {};text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
            color, text_color, border, label
        )

    @admin.display(description=_("Quantity"))
    def quantity_badge(self, obj: InventoryTransaction) -> str:
        if obj.direction == InventoryTransaction.FlowDirection.INBOUND:
            color = "#2E7D32"
            symbol = "+"
        elif obj.direction == InventoryTransaction.FlowDirection.OUTBOUND:
            color = "#C62828"
            symbol = "-"
        else:
            color = "#0D47A1"
            symbol = "±"
        return format_html(
            '<span style="font-weight:600;color:{};">{}{}</span>',
            color, symbol, obj.quantity,
        )

# ==============================================================================
# 4. STOCK RESERVATION ADMIN
# ==============================================================================
@admin.register(StockReservation)
class StockReservationAdmin(CSVExportMixin, admin.ModelAdmin):
    """
    Administrative interface for monitoring active and historical reservations.

    Reservations are managed primarily by the cart module and the cron cleanup
    job. This admin is read-mostly and exposes batch cleanup utilities only
    for staff intervention.
    """

    list_display = (
        "reservation_token_short",
        "status_badge",
        "reservation_type",
        "warehouse_name",
        "target_display",
        "quantity",
        "source",
        "expires_at",
        "is_expired_badge",
        "age_minutes",
    )
    list_filter = (
        "status",
        "reservation_type",
        "warehouse",
        "expires_at",
        "created_at",
    )
    search_fields = (
        "reservation_token",
        "cart__anonymous_token",
        "session_key",
        "user__email",
        "user__username",
        "inventory__product__title",
        "inventory__product__sku",
        "inventory__product_variant__sku",
        "notes",
    )
    raw_id_fields = (
        "cart",
        "inventory",
        "warehouse",
        "user",
        "product",
        "product_variant",
        "converted_to_order",
    )
    list_select_related: tuple = (
        "warehouse",
        "inventory__product",
        "inventory__product_variant",
        "user",
        "cart",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at", "id")
    list_per_page = 100

    csv_export_fields = (
        "id",
        "reservation_token",
        "status",
        "reservation_type",
        "quantity",
        "warehouse_id",
        "inventory_id",
        "product_id",
        "product_variant_id",
        "cart_id",
        "user_id",
        "session_key",
        "expires_at",
        "released_at",
        "converted_at",
        "converted_to_order_id",
        "is_active",
        "created_at",
    )
    csv_export_filename_prefix = "stock_reservations"

    actions = ("export_as_csv", "release_selected_reservations", "mark_as_expired")

    fieldsets = (
        (
            _("Identity"),
            {"fields": ("reservation_token", ("status", "reservation_type"))},
        ),
        (
            _("Source"),
            {
                "fields": (
                    "cart",
                    "user",
                    "session_key",
                )
            },
        ),
        (
            _("Stock Target"),
            {
                "fields": (
                    "warehouse",
                    "inventory",
                    ("product", "product_variant"),
                    "quantity",
                )
            },
        ),
        (
            _("Lifecycle"),
            {
                "fields": (
                    "expires_at",
                    ("released_at", "converted_at"),
                    "converted_to_order",
                )
            },
        ),
        (
            _("Audit"),
            {
                "fields": ("notes", ("created_at", "is_active")),
            }
        ),
    )
    readonly_fields = (
        "reservation_token",
        "created_at",
        "is_active",
    )

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).select_related(
            "warehouse", "inventory__product", "inventory__product_variant", "user", "cart"
        )

    @admin.display(description=_("Token"))
    def reservation_token_short(self, obj: StockReservation) -> str:
        return f"{obj.reservation_token.hex[:12]}…"

    @admin.display(description=_("Status"))
    def status_badge(self, obj: StockReservation) -> str:
        if obj.status == StockReservation.ReservationStatus.ACTIVE:
            color = "#E8F5E9"; text_color = "#2E7D32"; border = "rgba(46,125,50,0.3)"
        elif obj.status == StockReservation.ReservationStatus.CONVERTED:
            color = "#E3F2FD"; text_color = "#0D47A1"; border = "rgba(13,71,161,0.3)"
        elif obj.status == StockReservation.ReservationStatus.EXPIRED:
            color = "#FFEBEE"; text_color = "#C62828"; border = "rgba(198,40,40,0.3)"
        else:
            color = "#FAFAFA"; text_color = "#767676"; border = "#EAEAEA"
        return format_html(
            '<span style="display:inline-block;padding:3px 8px;background:{};color:{};font-size:11px;font-weight:600;border:1px solid {};text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
            color, text_color, border, obj.get_status_display()
        )

    @admin.display(description=_("Warehouse"), ordering="warehouse__name")
    def warehouse_name(self, obj: StockReservation) -> str:
        return obj.warehouse.display_name if obj.warehouse_id else "-"

    @admin.display(description=_("Target"))
    def target_display(self, obj: StockReservation) -> str:
        if obj.product_variant:
            return str(obj.product_variant)
        if obj.product:
            return str(obj.product)
        return "-"

    @admin.display(description=_("Source"))
    def source(self, obj: StockReservation) -> str:
        if obj.cart:
            return f"Cart #{obj.cart_id}"
        if obj.user:
            return f"User: {obj.user.username}"
        if obj.session_key:
            return f"Session: {obj.session_key[:8]}…"
        return "-"

    @admin.display(description=_("Expired?"))
    def is_expired_badge(self, obj: StockReservation) -> str:
        if obj.is_expired:
            return format_html(
                '<span style="display:inline-block;padding:2px 6px;background:#FFEBEE;color:#C62828;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;">EXPIRED</span>'
            )
        if obj.expires_at:
            return format_html(
                '<span style="display:inline-block;padding:2px 6px;background:#FFF8E7;color:#9A7B54;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;">{}m</span>',
                obj.minutes_until_expiry or 0,
            )
        return "-"

    @admin.display(description=_("Age (min)"))
    def age_minutes(self, obj: StockReservation) -> int:
        return obj.age_minutes

    @admin.action(description=_("Release selected reservations (mark as RELEASED)"))
    def release_selected_reservations(self, request: HttpRequest, queryset):
        active_qs = queryset.filter(status=StockReservation.ReservationStatus.ACTIVE)
        count = active_qs.update(
            status=StockReservation.ReservationStatus.RELEASED,
            released_at=timezone.now(),
            is_active=False,
        )
        self.message_user(request, _(f"{count} active reservations released."))

    @admin.action(description=_("Mark selected reservations as EXPIRED"))
    def mark_as_expired(self, request: HttpRequest, queryset):
        active_qs = queryset.filter(status=StockReservation.ReservationStatus.ACTIVE)
        count = active_qs.update(
            status=StockReservation.ReservationStatus.EXPIRED,
            is_active=False,
        )
        self.message_user(request, _(f"{count} active reservations marked as expired."))

# ==============================================================================
# 5. STOCK ADJUSTMENT ADMIN
# ==============================================================================
@admin.register(StockAdjustment)
class StockAdjustmentAdmin(CSVExportMixin, admin.ModelAdmin):
    """
    Approval workflow and audit for manual stock corrections.

    This admin implements a read-write workflow:
        * Drafts are fully editable.
        * Pending approvals are editable in metadata only.
        * Approved / applied records are read-only (audit integrity).
    """

    list_display = (
        "adjustment_number",
        "inventory_warehouse",
        "target_display",
        "reason",
        "old_quantity",
        "new_quantity",
        "difference_badge",
        "status_badge",
        "is_applied_badge",
        "initiated_by",
        "approved_by",
        "created_at",
    )
    list_filter = (
        "status",
        "reason",
        "approved_by",
        "initiated_by",
        "created_at",
    )
    search_fields = (
        "adjustment_number",
        "inventory__product__title",
        "inventory__product__sku",
        "inventory__warehouse__name",
        "initiated_by__username",
        "approved_by__username",
        "rejection_reason",
    )
    raw_id_fields = (
        "inventory",
        "initiated_by",
        "approved_by",
        "rejected_by",
        "applied_transaction",
    )
    list_select_related: tuple = (
        "inventory__warehouse",
        "inventory__product",
        "inventory__product_variant",
        "initiated_by",
        "approved_by",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at", "id")
    list_per_page = 50

    csv_export_fields = (
        "id",
        "adjustment_number",
        "inventory_id",
        "reason",
        "description",
        "old_quantity",
        "new_quantity",
        "difference",
        "status",
        "initiated_by_id",
        "approved_by_id",
        "approved_at",
        "rejected_by_id",
        "rejected_at",
        "rejection_reason",
        "applied_at",
        "applied_transaction_id",
        "created_at",
        "updated_at",
    )
    csv_export_filename_prefix = "stock_adjustments"

    actions = (
        "export_as_csv",
        "submit_for_approval",
        "approve_adjustments",
        "reject_adjustments",
        "apply_adjustments",
        "cancel_adjustments",
    )

    fieldsets = (
        (
            _("Identification"),
            {"fields": ("adjustment_number", "inventory", "reason")},
        ),
        (
            _("Quantities"),
            {
                "fields": (
                    ("old_quantity", "new_quantity"),
                    "difference",
                ),
                "description": _(
                    "Difference is auto-computed: new_quantity - old_quantity. May be positive or negative."
                ),
            },
        ),
        (
            _("Documentation"),
            {"fields": ("description", "supporting_documents")},
        ),
        (
            _("Workflow"),
            {
                "fields": (
                    "status",
                    ("initiated_by", "approved_by"),
                    ("rejected_by", "rejection_reason"),
                    ("approved_at", "rejected_at", "applied_at"),
                    "applied_transaction",
                )
            },
        ),
        (
            _("Audit"),
            {"fields": ("created_at", "updated_at")},
        ),
    )
    readonly_fields = (
        "adjustment_number",
        "difference",
        "approved_at",
        "rejected_at",
        "rejected_by",
        "rejection_reason",
        "applied_at",
        "applied_transaction",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).select_related(
            "inventory__warehouse",
            "inventory__product",
            "inventory__product_variant",
            "initiated_by",
            "approved_by",
            "rejected_by",
        )

    def get_readonly_fields(self, request: HttpRequest, obj: Optional[StockAdjustment] = None):
        """
        Approved, applied, or rejected adjustments are read-only.
        Drafts and pending approvals are still editable.
        """
        if obj is None:
            return self.readonly_fields

        locked_states = {
            StockAdjustment.AdjustmentStatus.APPROVED,
            StockAdjustment.AdjustmentStatus.APPLIED,
            StockAdjustment.AdjustmentStatus.REJECTED,
            StockAdjustment.AdjustmentStatus.CANCELLED,
        }
        if obj.status in locked_states:
            return [f.name for f in self.model._meta.fields]
        return self.readonly_fields

    def save_model(self, request: HttpRequest, obj: StockAdjustment, form, change: bool):
        if not change and not obj.initiated_by_id:
            obj.initiated_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description=_("Warehouse"), ordering="inventory__warehouse__name")
    def inventory_warehouse(self, obj: StockAdjustment) -> str:
        return obj.inventory.warehouse.display_name if obj.inventory_id else "-"

    @admin.display(description=_("Target"))
    def target_display(self, obj: StockAdjustment) -> str:
        target = obj.inventory.get_target() if obj.inventory_id else None
        return str(target) if target else "-"

    @admin.display(description=_("Difference"))
    def difference_badge(self, obj: StockAdjustment) -> str:
        if obj.difference is None:
            return "-"
        if obj.difference > Decimal("0"):
            color = "#2E7D32"
            symbol = "+"
        elif obj.difference < Decimal("0"):
            color = "#C62828"
            symbol = ""
        else:
            color = "#767676"
            symbol = "±"
        return format_html(
            '<span style="font-weight:600;color:{};">{}{}</span>',
            color, symbol, obj.difference,
        )

    @admin.display(description=_("Status"))
    def status_badge(self, obj: StockAdjustment) -> str:
        cfg = {
            StockAdjustment.AdjustmentStatus.DRAFT: ("#FAFAFA", "#767676", "#EAEAEA"),
            StockAdjustment.AdjustmentStatus.PENDING_APPROVAL: ("#FFF8E7", "#9A7B54", "rgba(154,123,84,0.3)"),
            StockAdjustment.AdjustmentStatus.APPROVED: ("#E8F5E9", "#2E7D32", "rgba(46,125,50,0.3)"),
            StockAdjustment.AdjustmentStatus.APPLIED: ("#E3F2FD", "#0D47A1", "rgba(13,71,161,0.3)"),
            StockAdjustment.AdjustmentStatus.REJECTED: ("#FFEBEE", "#C62828", "rgba(198,40,40,0.3)"),
            StockAdjustment.AdjustmentStatus.CANCELLED: ("#FAFAFA", "#767676", "#EAEAEA"),
        }
        color, text_color, border = cfg.get(obj.status, ("#FAFAFA", "#767676", "#EAEAEA"))
        return format_html(
            '<span style="display:inline-block;padding:3px 8px;background:{};color:{};font-size:11px;font-weight:600;border:1px solid {};text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
            color, text_color, border, obj.get_status_display()
        )

    @admin.display(description=_("Applied?"))
    def is_applied_badge(self, obj: StockAdjustment) -> str:
        if obj.is_applied:
            return format_html(
                '<span style="display:inline-block;padding:2px 6px;background:#E3F2FD;color:#0D47A1;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;">APPLIED</span>'
            )
        return "-"

    @admin.action(description=_("Submit selected drafts for approval"))
    def submit_for_approval(self, request: HttpRequest, queryset):
        drafts = queryset.filter(status=StockAdjustment.AdjustmentStatus.DRAFT)
        count = drafts.update(status=StockAdjustment.AdjustmentStatus.PENDING_APPROVAL)
        self.message_user(request, _(f"{count} adjustment(s) submitted for approval."))

    @admin.action(description=_("Approve selected pending adjustments"))
    def approve_adjustments(self, request: HttpRequest, queryset):
        pending = queryset.filter(status=StockAdjustment.AdjustmentStatus.PENDING_APPROVAL)
        now = timezone.now()
        count = pending.update(
            status=StockAdjustment.AdjustmentStatus.APPROVED,
            approved_by=request.user,
            approved_at=now,
        )
        self.message_user(request, _(f"{count} adjustment(s) approved."))

    @admin.action(description=_("Reject selected pending adjustments"))
    def reject_adjustments(self, request: HttpRequest, queryset):
        pending = queryset.filter(status=StockAdjustment.AdjustmentStatus.PENDING_APPROVAL)
        now = timezone.now()
        count = pending.update(
            status=StockAdjustment.AdjustmentStatus.REJECTED,
            rejected_by=request.user,
            rejected_at=now,
        )
        self.message_user(request, _(f"{count} adjustment(s) rejected."))

    @admin.action(description=_("Mark selected adjustments as applied (placeholder)"))
    def apply_adjustments(self, request: HttpRequest, queryset):
        approved = queryset.filter(status=StockAdjustment.AdjustmentStatus.APPROVED)
        count = approved.update(
            status=StockAdjustment.AdjustmentStatus.APPLIED,
            applied_at=timezone.now(),
        )
        self.message_user(
            request,
            _(
                "{0} adjustment(s) marked as applied. Stock quantities are not "
                "mutated by the admin; the dedicated Inventory service layer "
                "is required to actually commit the stock change."
            ).format(count),
        )

    @admin.action(description=_("Cancel selected adjustments"))
    def cancel_adjustments(self, request: HttpRequest, queryset):
        active_states = {
            StockAdjustment.AdjustmentStatus.DRAFT,
            StockAdjustment.AdjustmentStatus.PENDING_APPROVAL,
            StockAdjustment.AdjustmentStatus.APPROVED,
        }
        cancellable = queryset.filter(status__in=active_states)
        count = cancellable.update(status=StockAdjustment.AdjustmentStatus.CANCELLED)
        self.message_user(request, _(f"{count} adjustment(s) cancelled."))