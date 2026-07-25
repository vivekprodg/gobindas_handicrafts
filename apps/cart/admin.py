"""
Enterprise-grade Django Admin configuration for the Cart application.
Read-only surface for inventory metadata.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from django.conf import settings
from django.contrib import admin
from django.db import models
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from .models import Cart, CartItem

logger = logging.getLogger(__name__)

_DEFAULT_PER_PAGE_CART = 50
_DEFAULT_PER_PAGE_ITEM = 100
_DEFAULT_INVENTORY_LOW_STOCK = 5

def get_admin_per_page_cart() -> int:
    try:
        return max(1, int(getattr(settings, "CART_ADMIN_PER_PAGE", _DEFAULT_PER_PAGE_CART)))
    except (TypeError, ValueError):
        return _DEFAULT_PER_PAGE_CART

def get_admin_per_page_item() -> int:
    try:
        return max(1, int(getattr(settings, "CART_ADMIN_ITEM_PER_PAGE", _DEFAULT_PER_PAGE_ITEM)))
    except (TypeError, ValueError):
        return _DEFAULT_PER_PAGE_ITEM

def _safe_placeholder() -> str:
    return mark_safe('<span style="color:#999;font-style:italic;">&mdash;</span>')

def _safe_status_badge(status_key: Optional[str], label: str) -> str:
    safe_key = (status_key or "").lower()
    color_map = {
        "active": ("#2E7D32", "ACTIVE"),
        "in_stock": ("#2E7D32", "IN STOCK"),
        "abandoned": ("#9A7B54", "ABANDONED"),
        "converted": ("#0D47A1", "CONVERTED"),
        "expired": ("#9E9E9E", "EXPIRED"),
        "merged": ("#0D47A1", "MERGED"),
        "saved": ("#9A7B54", "SAVED"),
        "removed": ("#A94442", "REMOVED"),
        "out_of_stock": ("#A94442", "OUT OF STOCK"),
        "low_stock": ("#9A7B54", "LOW STOCK"),
    }
    color, text = color_map.get(safe_key, ("#767676", label.upper()))
    return format_html(
        '<span style="display:inline-block;padding:3px 8px;background:{};'
        'color:#fff;font-size:11px;font-weight:600;border-radius:2px;'
        'text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
        color, text,
    )

def _safe_admin_url(name: str, args: Optional[Sequence[Any]] = None) -> str:
    try:
        return reverse(name, args=args or [])
    except (NoReverseMatch, Exception):
        return "#"

def _admin_link(url: str, label: str) -> str:
    if not url or url == "#":
        return _safe_placeholder()
    return format_html('<a href="{}">{}</a>', url, label)

class CartItemInline(admin.TabularInline):
    model = CartItem
    extra = 0
    fields = (
        "product",
        "variant",
        "quantity",
        "unit_price_snapshot",
        "status",
        "saved_reason",
    )
    readonly_fields = (
        "product_name_snapshot",
        "product_sku_snapshot",
        "variant_name_snapshot",
        "line_subtotal_display",
        "added_at",
        "updated_at",
    )
    raw_id_fields = ("product", "variant", "cart")
    show_change_link = True
    classes = ["collapse"]
    ordering = ("added_at",)

    @admin.display(description=_("Line Subtotal"))
    def line_subtotal_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        return f"{obj.line_subtotal} {obj.currency_snapshot or ''}"

@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "get_owner",
        "status_badge",
        "is_active",
        "total_items_display",
        "subtotal_display",
        "currency",
        "last_activity_at",
        "created_at",
    )
    list_filter = (
        "status",
        "is_active",
        "currency",
        "created_at",
        "last_activity_at",
    )
    search_fields = (
        "id",
        "session_key",
        "anonymous_token",
        "customer__email",
        "customer__first_name",
        "customer__last_name",
        "customer__username",
        "coupon_code",
    )
    raw_id_fields = ("customer", "preferred_warehouse")
    date_hierarchy = "created_at"
    ordering = ("-last_activity_at",)
    list_select_related = ("customer", "preferred_warehouse")
    list_per_page = get_admin_per_page_cart()
    inlines = [CartItemInline]
    actions = ("mark_as_abandoned", "mark_as_active", "clear_selected_carts")

    readonly_fields = (
        "anonymous_token",
        "created_at",
        "updated_at",
        "last_activity_at",
        "last_merged_at",
        "recovered_at",
        "subtotal_display",
        "total_items_display",
        "estimated_tax_display",
        "estimated_shipping_display",
        "grand_total_display",
    )

    fieldsets = (
        (_("General"), {"fields": ("customer", "session_key", "status", "is_active")}),
        (_("Cart Details"), {"fields": ("currency", "coupon_code", "customer_note", "preferred_warehouse")}),
        (
            _("Computed Totals"),
            {
                "fields": (
                    "subtotal_display",
                    "estimated_tax_display",
                    "estimated_shipping_display",
                    "grand_total_display",
                    "total_items_display",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Lifecycle & Audit"),
            {
                "fields": ("anonymous_token", "recovered_at", "expires_at", "last_activity_at", "last_merged_at", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).select_related("customer", "preferred_warehouse")

    @admin.display(description=_("Owner"), ordering="customer__email")
    def get_owner(self, obj: Optional[Cart]) -> str:
        if obj is None:
            return "—"
        if obj.customer_id:
            name = obj.customer.get_full_name() or obj.customer.username
            email = getattr(obj.customer, "email", "")
            return f"{name} <{email}>" if email else name
        return f"Guest ({obj.anonymous_token[:8]})" if obj.anonymous_token else "Guest"

    @admin.display(description=_("Status"))
    def status_badge(self, obj: Optional[Cart]) -> str:
        if not obj or not obj.status:
            return "—"
        return _safe_status_badge(obj.status, obj.get_status_display())

    @admin.display(description=_("Items (Active)"))
    def total_items_display(self, obj: Optional[Cart]) -> str:
        return str(obj.total_items_count if obj else 0)

    @admin.display(description=_("Subtotal"))
    def subtotal_display(self, obj: Optional[Cart]) -> str:
        return f"{obj.subtotal} {obj.currency or ''}" if obj else "—"

    @admin.display(description=_("Est. Tax"))
    def estimated_tax_display(self, obj: Optional[Cart]) -> str:
        return f"{obj.estimated_tax} {obj.currency or ''}" if obj else "—"

    @admin.display(description=_("Est. Shipping"))
    def estimated_shipping_display(self, obj: Optional[Cart]) -> str:
        return f"{obj.estimated_shipping} {obj.currency or ''}" if obj else "—"

    @admin.display(description=_("Grand Total"))
    def grand_total_display(self, obj: Optional[Cart]) -> str:
        return f"{obj.grand_total} {obj.currency or ''}" if obj else "—"

    @admin.action(description=_("Mark selected carts as abandoned"))
    def mark_as_abandoned(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(status=Cart.CartStatus.ABANDONED, is_active=False)
        self.message_user(request, _("%(count)d carts marked as abandoned.") % {"count": updated})

    @admin.action(description=_("Mark selected carts as active again"))
    def mark_as_active(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(status=Cart.CartStatus.ACTIVE, is_active=True)
        self.message_user(request, _("%(count)d carts reactivated.") % {"count": updated})

    @admin.action(description=_("Clear all items from selected carts"))
    def clear_selected_carts(self, request: HttpRequest, queryset: QuerySet) -> None:
        count, _ = CartItem.objects.filter(cart__in=queryset).delete()
        self.message_user(request, _("Cleared %(count)d items from selected carts.") % {"count": count})

@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "get_cart_owner",
        "product_name_snapshot",
        "variant_name_snapshot",
        "quantity",
        "unit_price_snapshot",
        "line_subtotal_display",
        "status",
        "added_at",
    )
    list_filter = ("status", "saved_reason", "added_at")
    search_fields = (
        "id",
        "product_name_snapshot",
        "product_sku_snapshot",
        "cart__session_key",
        "cart__customer__email",
    )
    raw_id_fields = ("cart", "product", "variant", "inventory", "reservation", "warehouse")
    date_hierarchy = "added_at"
    ordering = ("-updated_at",)
    list_per_page = get_admin_per_page_item()

    readonly_fields = (
        "added_at",
        "updated_at",
        "saved_at",
        "moved_to_save_at",
        "line_subtotal_display",
    )

    @admin.display(description=_("Cart"))
    def get_cart_owner(self, obj: Optional[CartItem]) -> str:
        if not obj or not obj.cart:
            return "—"
        url = _safe_admin_url("admin:cart_cart_change", args=[obj.cart.pk])
        return _admin_link(url, f"Cart #{obj.cart.pk}")

    @admin.display(description=_("Line Subtotal"))
    def line_subtotal_display(self, obj: Optional[CartItem]) -> str:
        return f"{obj.line_subtotal} {obj.currency_snapshot or ''}" if obj else "—"

__all__ = ["CartItemInline", "CartAdmin", "CartItemAdmin"]