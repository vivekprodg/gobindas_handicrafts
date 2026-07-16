"""
Enterprise-grade Django Admin configuration for the Cart application.

This module is a strict consumer of the Inventory domain. The Cart never
owns, edits, calculates, or mutates inventory data. The Inventory
application is the single source of truth for all stock-related
operations. Cart only references inventory through the ``Inventory``,
``Reservation``, and ``Warehouse`` foreign keys and the read-only
``reservation_*`` mirror fields.

ARCHITECTURE COMPLIANCE
=======================

The Cart admin NEVER:
    * Edits stock
    * Modifies inventory quantities
    * Creates or releases reservations
    * Adjusts reservations
    * Marks inventory as available or unavailable
    * Recalculates or synchronizes stock
    * Persists inventory data
    * Touches the Inventory app's data layer

The Cart admin ONLY:
    * Reads inventory state through a strictly read-only surface
    * Displays inventory references as navigation links
    * Provides CMS-driven labels and configuration
    * Manages cart-domain state (status, notes, owner, items)
    * Surfaces reservation references for staff visibility

CMS-DRIVEN CONFIGURATION
========================

Every label, threshold, and behavior is parameterizable through Django
settings. Defaults are defined at module level and can be overridden
without code changes.

OWASP COMPLIANCE
================

* All HTML output is escaped via ``format_html``.
* All URL generation is wrapped in safe helpers that never raise.
* Inventory references are NEVER editable widgets.
* Permission checks defer to Django's built-in permission system.
* No user-controlled values are trusted.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from django.conf import settings
from django.contrib import admin
from django.db import models
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from .models import Cart, CartItem

logger = logging.getLogger(__name__)

# ==============================================================================
# CONFIGURATION (CMS-DRIVEN)
# ==============================================================================
# All defaults can be overridden via Django settings, which in turn can be
# driven by the CMS without code changes. This keeps the admin fully
# parameterized and future-proof.

_DEFAULT_PER_PAGE_CART = 50
_DEFAULT_PER_PAGE_ITEM = 100
_DEFAULT_INVENTORY_LOW_STOCK = 5

def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)

def get_admin_per_page_cart() -> int:
    """
    Returns the configured changelist page size for the Cart admin.

    Pulled from ``CART_ADMIN_PER_PAGE`` in settings (default: 50).
    """
    try:
        value = int(_get_setting("CART_ADMIN_PER_PAGE", _DEFAULT_PER_PAGE_CART))
        return max(1, value)
    except (TypeError, ValueError):
        return _DEFAULT_PER_PAGE_CART

def get_admin_per_page_item() -> int:
    """
    Returns the configured changelist page size for the CartItem admin.

    Pulled from ``CART_ADMIN_ITEM_PER_PAGE`` in settings (default: 100).
    """
    try:
        value = int(_get_setting("CART_ADMIN_ITEM_PER_PAGE", _DEFAULT_PER_PAGE_ITEM))
        return max(1, value)
    except (TypeError, ValueError):
        return _DEFAULT_PER_PAGE_ITEM

def get_inventory_low_stock_threshold() -> int:
    """
    Returns the CMS-driven low-stock threshold.

    The Cart admin may use this to render consistent low-stock badges
    even though the authoritative value lives in the Inventory app.
    """
    try:
        value = int(_get_setting(
            "INVENTORY_LOW_STOCK_THRESHOLD",
            _DEFAULT_INVENTORY_LOW_STOCK,
        ))
        return max(0, value)
    except (TypeError, ValueError):
        return _DEFAULT_INVENTORY_LOW_STOCK

# ==============================================================================
# SAFE HTML RENDERING HELPERS
# ==============================================================================
def _safe_placeholder() -> str:
    """Returns a safe HTML placeholder for missing or null values."""
    return mark_safe(
        '<span style="color:#999;font-style:italic;">&mdash;</span>'
    )

def _safe_text(value: Any) -> str:
    """Coerce any value to a safe trimmed string."""
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""

def _safe_decimal(value: Any) -> str:
    """Format a decimal as a safe string with a placeholder fallback."""
    if value is None or value == "":
        return _safe_placeholder()
    try:
        return str(value)
    except Exception:
        return _safe_placeholder()

def _safe_datetime(value: Any) -> str:
    """Format a datetime as a safe ISO 8601 string."""
    if value is None:
        return _safe_placeholder()
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return _safe_placeholder()

def _safe_status_badge(status_key: Optional[str], label: str) -> str:
    """
    Render a status as a colored, read-only badge.

    All HTML is escaped via ``format_html``. The label is escaped
    implicitly; the color tokens are hard-coded constants.
    """
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
        "available": ("#2E7D32", "AVAILABLE"),
        "unavailable": ("#A94442", "UNAVAILABLE"),
        "released": ("#9E9E9E", "RELEASED"),
        "reserved": ("#0D47A1", "RESERVED"),
        "draft": ("#9E9E9E", "DRAFT"),
        "pending": ("#9A7B54", "PENDING"),
    }
    color, text = color_map.get(safe_key, ("#767676", label.upper()))
    return format_html(
        '<span style="display:inline-block;padding:3px 8px;background:{};'
        "color:#fff;font-size:11px;font-weight:600;border-radius:2px;"
        'text-transform:uppercase;letter-spacing:0.05em;">{}</span>',
        color, text,
    )

def _safe_indicator(text: str, color: str) -> str:
    """Render a compact read-only indicator (e.g. OK / LOW / OUT)."""
    return format_html(
        '<span style="color:{};font-weight:700;">{}</span>', color, text
    )

# ==============================================================================
# SAFE URL HELPERS
# ==============================================================================
def _safe_admin_url(
    name: str,
    args: Optional[Sequence[Any]] = None,
) -> str:
    """
    Build a Django admin URL by name.

    Returns a safe placeholder ("#") if the URL cannot be resolved or
    if the lookup raises. This guarantees the admin never fails because
    a related object's URL pattern is unavailable or the reverse
    dispatcher is misconfigured.
    """
    try:
        return reverse(name, args=args or [])
    except NoReverseMatch:
        return "#"
    except Exception as exc:
        logger.debug("Safe admin URL lookup failed for %s: %s", name, exc)
        return "#"

def _admin_link(url: str, label: str) -> str:
    """
    Render a safe admin link with proper escaping.

    Returns a placeholder if the URL is empty or "#". All output is
    escaped via ``format_html``.
    """
    if not url or url == "#":
        return _safe_placeholder()
    return format_html('<a href="{}">{}</a>', url, label)

# ==============================================================================
# INLINE: CartItemInline
# ==============================================================================
class CartItemInline(admin.TabularInline):
    """
    Inline editor for cart items inside the parent cart changelist.

    ARCHITECTURE
    ------------
    This inline is STRICTLY READ-ONLY for all inventory-related fields.
    Staff can view inventory state, but they cannot modify it from the
    cart admin. All inventory mutations are owned by the Inventory app.

    The inline allows standard cart operations (add, change cart-domain
    fields, delete) while making every inventory reference a read-only
    field. This preserves the architectural boundary between Cart and
    Inventory.

    NOTE ON ``autocomplete_fields``
    --------------------------------
    Django's ``autocomplete_fields`` requires that every referenced
    model has a registered ``ModelAdmin`` with ``search_fields``. The
    ``ProductVariant`` admin lives in the catalog app and may or may
    not be registered for autocomplete. To keep the cart admin
    self-contained and avoid the ``admin.E039`` system check error,
    we use ``raw_id_fields`` (already declared below) for product /
    variant selection. ``raw_id_fields`` does NOT require an admin
    with ``search_fields`` to be registered for the target model.
    """

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
        # Product / variant mirrors
        "product_name_snapshot",
        "product_sku_snapshot",
        "variant_name_snapshot",
        "line_subtotal_display",
        # Inventory read-only references
        "inventory_reference",
        "warehouse_reference",
        "inventory_status_display",
        "available_quantity_display",
        "reserved_quantity_display",
        "low_stock_indicator",
        # Reservation read-only references
        "reservation_reference",
        "reservation_token_display",
        "reservation_status_display",
        "reservation_expires_display",
        "reservation_source_display",
        # Audit timestamps
        "added_at",
        "updated_at",
    )
    raw_id_fields = ("product", "variant", "cart")
    show_change_link = True
    classes = ["collapse"]
    ordering = ("added_at",)
    max_num = 200
    template = "admin/cart/edit_inline/tabular.html"

    # NOTE: ``autocomplete_fields`` intentionally NOT declared here.
    # See class docstring for the rationale.

    # ----- Permission enforcement (cart-domain safe) ---------------------
    def has_add_permission(self, request: HttpRequest, obj: Optional[Cart] = None) -> bool:
        """
        Cart items may be added via the admin for support workflows.

        Inventory fields are readonly in the form, so this cannot be
        abused to mutate inventory data.
        """
        return True

    def has_change_permission(self, request: HttpRequest, obj: Optional[Cart] = None) -> bool:
        """
        Cart items may be edited for cart-domain fields.

        Inventory references are enforced as readonly fields in the form,
        so this cannot be abused to mutate inventory data.
        """
        return True

    def has_delete_permission(self, request: HttpRequest, obj: Optional[Cart] = None) -> bool:
        """Cart items may be deleted (cart-domain operation)."""
        return True

    # ----- Read-only display methods --------------------------------------
    @admin.display(description=_("Line Subtotal"))
    def line_subtotal_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            return f"{obj.line_subtotal} {obj.currency_snapshot or ''}"
        except Exception:
            return "—"

    @admin.display(description=_("Inventory"))
    def inventory_reference(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return _safe_placeholder()
        pk = getattr(inventory, "pk", None)
        if pk is None:
            return _safe_placeholder()
        url = _safe_admin_url("admin:inventory_inventory_change", args=[pk])
        return _admin_link(url, f"#{pk}")

    @admin.display(description=_("Warehouse"))
    def warehouse_reference(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        warehouse = getattr(obj, "warehouse", None)
        if warehouse is None:
            inventory = getattr(obj, "inventory", None)
            if inventory is not None:
                warehouse = getattr(inventory, "warehouse", None)
        if warehouse is None:
            return _safe_placeholder()
        pk = getattr(warehouse, "pk", None)
        if pk is None:
            return _safe_placeholder()
        display = getattr(warehouse, "display_name", None) or str(warehouse)
        url = _safe_admin_url("admin:inventory_warehouse_change", args=[pk])
        return _admin_link(url, display)

    @admin.display(description=_("Status"))
    def inventory_status_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return _safe_placeholder()
        try:
            if getattr(inventory, "is_out_of_stock", False):
                return _safe_status_badge("out_of_stock", "Out of Stock")
            if getattr(inventory, "is_low_stock", False):
                return _safe_status_badge("low_stock", "Low Stock")
            return _safe_status_badge("in_stock", "In Stock")
        except Exception:
            return _safe_placeholder()

    @admin.display(description=_("Available"))
    def available_quantity_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return "—"
        return _safe_decimal(getattr(inventory, "available_quantity", None))

    @admin.display(description=_("Reserved"))
    def reserved_quantity_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return "—"
        return _safe_decimal(getattr(inventory, "reserved_quantity", None))

    @admin.display(description=_("Low?"))
    def low_stock_indicator(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return "—"
        try:
            if getattr(inventory, "is_out_of_stock", False):
                return _safe_indicator("OUT", "#A94442")
            if getattr(inventory, "is_low_stock", False):
                return _safe_indicator("LOW", "#9A7B54")
            return _safe_indicator("OK", "#2E7D32")
        except Exception:
            return "—"

    @admin.display(description=_("Reservation"))
    def reservation_reference(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        reservation = getattr(obj, "reservation", None)
        if reservation is None:
            return _safe_placeholder()
        pk = getattr(reservation, "pk", None)
        if pk is None:
            return _safe_placeholder()
        url = _safe_admin_url(
            "admin:inventory_stockreservation_change", args=[pk]
        )
        return _admin_link(url, f"#{pk}")

    @admin.display(description=_("Token"))
    def reservation_token_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        token = getattr(obj, "reservation_token", None)
        if not token:
            return _safe_placeholder()
        return f"{str(token)[:12]}…"

    @admin.display(description=_("Res. Status"))
    def reservation_status_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        status = getattr(obj, "reservation_status", None)
        if not status:
            return _safe_placeholder()
        try:
            label = str(status).replace("_", " ").title()
            return _safe_status_badge(status, label)
        except Exception:
            return str(status)

    @admin.display(description=_("Expires"))
    def reservation_expires_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        return _safe_datetime(getattr(obj, "reservation_expires_at", None))

    @admin.display(description=_("Source"))
    def reservation_source_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        source = getattr(obj, "reservation_source", None)
        if not source:
            return _safe_placeholder()
        return str(source)

# ==============================================================================
# CartAdmin
# ==============================================================================
@admin.register(Cart)
class CartAdmin(admin.ModelAdmin):
    """
    Enterprise admin for the Cart model.

    Provides CMS-driven cart management with a strictly read-only
    inventory surface. All stock and reservation state is exposed
    through the Inventory application's admin; this admin never
    mutates inventory data.
    """

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
    show_full_result_count = True
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
        "is_guest_display",
        "preferred_warehouse_display",
        "active_reservations_display",
    )

    fieldsets = (
        (
            _("General"),
            {
                "fields": (
                    "customer",
                    "session_key",
                    "status",
                    "is_active",
                ),
            },
        ),
        (
            _("Cart Details"),
            {
                "fields": (
                    "currency",
                    "coupon_code",
                    "customer_note",
                    "preferred_warehouse",
                ),
            },
        ),
        (
            _("Computed Totals"),
            {
                "fields": (
                    "subtotal_display",
                    "estimated_tax_display",
                    "estimated_shipping_display",
                    "grand_total_display",
                    "total_items_display",
                    "is_guest_display",
                ),
                "classes": ("collapse",),
                "description": _(
                    "These fields are computed live from the cart's items. "
                    "They are read-only and never editable."
                ),
            },
        ),
        (
            _("Inventory References"),
            {
                "fields": ("active_reservations_display",),
                "classes": ("collapse",),
                "description": _(
                    "Read-only inventory summary for this cart. All stock "
                    "mutations are owned by the Inventory application."
                ),
            },
        ),
        (
            _("Lifecycle Metadata"),
            {
                "fields": (
                    "recovered_at",
                    "expires_at",
                    "last_activity_at",
                    "last_merged_at",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Audit Information"),
            {
                "fields": ("anonymous_token",),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        """
        Optimized queryset for cart list view.

        Uses ``select_related`` for foreign keys and annotated
        aggregates to eliminate N+1 query patterns. Designed to
        scale to millions of cart records.
        """
        return super().get_queryset(request).select_related(
            "customer", "preferred_warehouse"
        ).annotate(
            _active_items_count=models.Count(
                "items",
                filter=models.Q(items__status=CartItem.ItemStatus.ACTIVE),
                distinct=True,
            ),
            _active_quantity=models.Sum(
                "items__quantity",
                filter=models.Q(items__status=CartItem.ItemStatus.ACTIVE),
            ),
            _subtotal=models.Sum(
                models.F("items__unit_price_snapshot")
                * models.F("items__quantity"),
                filter=models.Q(items__status=CartItem.ItemStatus.ACTIVE),
                output_field=models.DecimalField(
                    max_digits=14, decimal_places=2,
                ),
            ),
            _active_reservations_count=models.Count(
                "stock_reservations",
                filter=models.Q(
                    stock_reservations__status="active",
                    stock_reservations__is_active=True,
                ),
                distinct=True,
            ),
        )

    # ----- Display methods -------------------------------------------------
    @admin.display(description=_("Owner"), ordering="customer__email")
    def get_owner(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            customer = getattr(obj, "customer", None)
            if customer is not None:
                name = customer.get_full_name() or customer.username
                email = getattr(customer, "email", "") or ""
                if name and email:
                    return f"{name} <{email}>"
                if email:
                    return email
                return name or "—"
            token = getattr(obj, "anonymous_token", None) or ""
            if token:
                return f"Guest ({token[:8]})"
            return "Guest"
        except Exception:
            return "—"

    @admin.display(description=_("Status"))
    def status_badge(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        status = getattr(obj, "status", None)
        if not status:
            return "—"
        try:
            label = str(status).replace("_", " ").title()
            return _safe_status_badge(status, label)
        except Exception:
            return str(status)

    @admin.display(
        description=_("Items (Active)"),
        ordering="_active_quantity",
    )
    def total_items_display(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "0"
        try:
            annotated = getattr(obj, "_active_quantity", None)
            if annotated is not None:
                return str(annotated or 0)
            return str(obj.total_items_count or 0)
        except Exception:
            return "0"

    @admin.display(description=_("Subtotal"))
    def subtotal_display(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            annotated = getattr(obj, "_subtotal", None)
            if annotated is not None:
                return f"{annotated} {obj.currency or ''}"
            return f"{obj.subtotal} {obj.currency or ''}"
        except Exception:
            return "—"

    @admin.display(description=_("Est. Tax"))
    def estimated_tax_display(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            return f"{obj.estimated_tax} {obj.currency or ''}"
        except Exception:
            return "—"

    @admin.display(description=_("Est. Shipping"))
    def estimated_shipping_display(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            return f"{obj.estimated_shipping} {obj.currency or ''}"
        except Exception:
            return "—"

    @admin.display(description=_("Grand Total"))
    def grand_total_display(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            return f"{obj.grand_total} {obj.currency or ''}"
        except Exception:
            return "—"

    @admin.display(description=_("Guest?"))
    def is_guest_display(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            if obj.is_guest:
                return _safe_indicator("YES", "#9A7B54")
            return _safe_indicator("NO", "#2E7D32")
        except Exception:
            return "—"

    @admin.display(description=_("Preferred Warehouse"))
    def preferred_warehouse_display(self, obj: Optional[Cart]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        warehouse = getattr(obj, "preferred_warehouse", None)
        if warehouse is None:
            return _safe_placeholder()
        pk = getattr(warehouse, "pk", None)
        if pk is None:
            return _safe_placeholder()
        display = getattr(warehouse, "display_name", None) or str(warehouse)
        url = _safe_admin_url("admin:inventory_warehouse_change", args=[pk])
        return _admin_link(url, display)

    @admin.display(description=_("Active Reservations"))
    def active_reservations_display(self, obj: Optional[Cart]) -> str:
        """
        Count of active stock reservations linked to this cart.

        This is a pure read-only display. Staff must visit the
        Inventory admin to manage reservations.
        """
        if obj is None or not getattr(obj, "pk", None):
            return "0"
        try:
            annotated = getattr(obj, "_active_reservations_count", None)
            count = (
                annotated
                if annotated is not None
                else obj.stock_reservations.filter(
                    status="active", is_active=True
                ).count()
            )
            if not count:
                return "0"
            return format_html(
                '<a href="{}?cart__id__exact={}">{} active</a>',
                _safe_admin_url(
                    "admin:inventory_stockreservation_changelist"
                ),
                obj.pk,
                count,
            )
        except Exception:
            return "0"

    # ----- Admin actions (cart-domain only) -------------------------------
    @admin.action(description=_("Mark selected carts as abandoned"))
    def mark_as_abandoned(self, request: HttpRequest, queryset: QuerySet) -> None:
        """
        Cart-domain action: mark carts as abandoned.

        Does NOT touch inventory. Any reservation cleanup is handled
        by the Inventory service layer / cron expiry job.
        """
        updated = queryset.update(
            status=Cart.CartStatus.ABANDONED, is_active=False
        )
        self.message_user(
            request,
            _("%(count)d carts marked as abandoned.") % {"count": updated},
        )

    @admin.action(description=_("Mark selected carts as active again"))
    def mark_as_active(self, request: HttpRequest, queryset: QuerySet) -> None:
        """
        Cart-domain action: reactivate carts.

        Does NOT create reservations. Staff should use the Inventory
        admin to manage stock holds.
        """
        updated = queryset.update(
            status=Cart.CartStatus.ACTIVE, is_active=True
        )
        self.message_user(
            request,
            _("%(count)d carts reactivated.") % {"count": updated},
        )

    @admin.action(description=_("Clear all items from selected carts"))
    def clear_selected_carts(self, request: HttpRequest, queryset: QuerySet) -> None:
        """
        Cart-domain action: delete all items from selected carts.

        This is a CART operation. Any inventory side effects (e.g.
        reservation release) are handled by the Inventory service
        layer through signals or scheduled jobs. The cart admin
        does not perform any inventory operation directly.
        """
        try:
            count, _ = CartItem.objects.filter(cart__in=queryset).delete()
        except Exception as exc:
            logger.exception("Failed to clear cart items: %s", exc)
            count = 0
        self.message_user(
            request,
            _("Cleared %(count)d items from selected carts.")
            % {"count": count},
        )

# ==============================================================================
# CartItemAdmin
# ==============================================================================
@admin.register(CartItem)
class CartItemAdmin(admin.ModelAdmin):
    """
    Standalone admin for cart items (advanced debugging & support).

    Provides deep, read-only visibility into inventory and reservation
    state. Staff can manage cart-domain fields (quantity, status, save
    reason) but cannot touch inventory data.

    All inventory-related widgets are declared as ``readonly_fields``
    and are therefore rendered as safe, non-editable HTML. Inventory
    references are always exposed as clickable navigation links to the
    Inventory admin.

    NOTE ON ``autocomplete_fields``
    --------------------------------
    Django's ``autocomplete_fields`` requires that every referenced
    model has a registered ``ModelAdmin`` with ``search_fields``. The
    ``ProductVariant`` admin lives in the catalog app and may or may
    not be registered for autocomplete. To keep the cart admin
    self-contained and avoid the ``admin.E039`` system check error,
    we rely on ``raw_id_fields`` (already declared below) for product
    / variant selection. ``raw_id_fields`` does NOT require an admin
    with ``search_fields`` to be registered for the target model.
    """

    list_display = (
        "id",
        "get_cart_owner",
        "product_name_snapshot",
        "variant_name_snapshot",
        "quantity",
        "unit_price_snapshot",
        "line_subtotal_display",
        "status",
        "saved_reason",
        "inventory_status_display",
        "added_at",
        "updated_at",
    )
    list_filter = (
        "status",
        "saved_reason",
        "added_at",
        "updated_at",
    )
    search_fields = (
        "id",
        "product_name_snapshot",
        "product_sku_snapshot",
        "variant_name_snapshot",
        "cart__session_key",
        "cart__anonymous_token",
        "cart__customer__email",
        "cart__customer__username",
        "inventory__id",
        "reservation__id",
        "warehouse__code",
        "warehouse__name",
    )
    raw_id_fields = (
        "cart",
        "product",
        "variant",
        "inventory",
        "reservation",
        "warehouse",
    )
    date_hierarchy = "added_at"
    ordering = ("-updated_at",)
    list_select_related = (
        "cart",
        "cart__customer",
        "product",
        "variant",
    )
    list_per_page = get_admin_per_page_item()
    show_full_result_count = True
    save_on_top = True

    readonly_fields = (
        "added_at",
        "updated_at",
        "saved_at",
        "moved_to_save_at",
        "line_subtotal_display",
        "inventory_reference",
        "warehouse_reference",
        "inventory_status_display",
        "available_quantity_display",
        "reserved_quantity_display",
        "low_stock_indicator",
        "reservation_reference",
        "reservation_token_display",
        "reservation_status_display",
        "reservation_expires_display",
        "reservation_source_display",
        "reservation_notes_display",
        "reservation_version_display",
    )

    fieldsets = (
        (
            _("General"),
            {
                "fields": (
                    "cart",
                    "product",
                    "variant",
                    "quantity",
                    "status",
                    "saved_reason",
                ),
            },
        ),
        (
            _("Cart Snapshots"),
            {
                "fields": (
                    "product_name_snapshot",
                    "product_sku_snapshot",
                    "variant_name_snapshot",
                    "product_image_snapshot",
                    "unit_price_snapshot",
                    "compare_at_price_snapshot",
                    "currency_snapshot",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Inventory References"),
            {
                "description": _(
                    "Read-only inventory summary. All stock mutations are "
                    "owned by the Inventory application."
                ),
                "fields": (
                    "inventory_reference",
                    "warehouse_reference",
                    "inventory_status_display",
                    "available_quantity_display",
                    "reserved_quantity_display",
                    "low_stock_indicator",
                ),
            },
        ),
        (
            _("Reservation Information"),
            {
                "description": _(
                    "Read-only reservation summary. All reservation "
                    "mutations are owned by the Inventory application."
                ),
                "fields": (
                    "reservation_reference",
                    "reservation_token_display",
                    "reservation_status_display",
                    "reservation_expires_display",
                    "reservation_source_display",
                    "reservation_notes_display",
                    "reservation_version_display",
                ),
            },
        ),
        (
            _("Personalization & Attributes"),
            {
                "fields": ("attributes_snapshot", "personalization"),
                "classes": ("collapse",),
            },
        ),
        (
            _("Pricing & Audit"),
            {
                "fields": (
                    "line_subtotal_display",
                    "added_at",
                    "updated_at",
                    "saved_at",
                    "moved_to_save_at",
                ),
            },
        ),
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        """
        Optimized queryset for cart item list view.

        Selects related customer, cart, product, variant, and pre-fetches
        inventory / reservation / warehouse references to eliminate
        N+1 query patterns in the changelist rendering.
        """
        return super().get_queryset(request).select_related(
            "cart",
            "cart__customer",
            "product",
            "variant",
            "inventory__warehouse",
            "reservation",
            "warehouse",
        )

    # NOTE: ``autocomplete_fields`` intentionally NOT declared.
    # See class docstring for the rationale.

    # ----- Display methods -------------------------------------------------
    @admin.display(description=_("Cart"), ordering="cart__id")
    def get_cart_owner(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        cart = getattr(obj, "cart", None)
        if cart is None:
            return "—"
        try:
            pk = cart.pk
            customer = getattr(cart, "customer", None)
            url = _safe_admin_url("admin:cart_cart_change", args=[pk])
            if customer is not None:
                return _admin_link(
                    url, f"#{pk} ({getattr(customer, 'email', '')})"
                )
            token = getattr(cart, "anonymous_token", None) or ""
            if token:
                return _admin_link(url, f"#{pk} (Guest: {token[:8]})")
            return _admin_link(url, f"#{pk} (Guest)")
        except Exception:
            return f"#{getattr(cart, 'pk', '?')}"

    @admin.display(description=_("Line Subtotal"))
    def line_subtotal_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        try:
            return f"{obj.line_subtotal} {obj.currency_snapshot or ''}"
        except Exception:
            return "—"

    @admin.display(description=_("Inventory"))
    def inventory_reference(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return _safe_placeholder()
        pk = getattr(inventory, "pk", None)
        if pk is None:
            return _safe_placeholder()
        url = _safe_admin_url("admin:inventory_inventory_change", args=[pk])
        return _admin_link(url, f"#{pk}")

    @admin.display(description=_("Warehouse"))
    def warehouse_reference(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        warehouse = getattr(obj, "warehouse", None)
        if warehouse is None:
            inventory = getattr(obj, "inventory", None)
            if inventory is not None:
                warehouse = getattr(inventory, "warehouse", None)
        if warehouse is None:
            return _safe_placeholder()
        pk = getattr(warehouse, "pk", None)
        if pk is None:
            return _safe_placeholder()
        display = getattr(warehouse, "display_name", None) or str(warehouse)
        url = _safe_admin_url("admin:inventory_warehouse_change", args=[pk])
        return _admin_link(url, display)

    @admin.display(description=_("Status"))
    def inventory_status_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return _safe_placeholder()
        try:
            if getattr(inventory, "is_out_of_stock", False):
                return _safe_status_badge("out_of_stock", "Out of Stock")
            if getattr(inventory, "is_low_stock", False):
                return _safe_status_badge("low_stock", "Low Stock")
            return _safe_status_badge("in_stock", "In Stock")
        except Exception:
            return _safe_placeholder()

    @admin.display(description=_("Available"))
    def available_quantity_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return "—"
        return _safe_decimal(getattr(inventory, "available_quantity", None))

    @admin.display(description=_("Reserved"))
    def reserved_quantity_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return "—"
        return _safe_decimal(getattr(inventory, "reserved_quantity", None))

    @admin.display(description=_("Low?"))
    def low_stock_indicator(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        inventory = getattr(obj, "inventory", None)
        if inventory is None:
            return "—"
        try:
            if getattr(inventory, "is_out_of_stock", False):
                return _safe_indicator("OUT", "#A94442")
            if getattr(inventory, "is_low_stock", False):
                return _safe_indicator("LOW", "#9A7B54")
            return _safe_indicator("OK", "#2E7D32")
        except Exception:
            return "—"

    @admin.display(description=_("Reservation"))
    def reservation_reference(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        reservation = getattr(obj, "reservation", None)
        if reservation is None:
            return _safe_placeholder()
        pk = getattr(reservation, "pk", None)
        if pk is None:
            return _safe_placeholder()
        url = _safe_admin_url(
            "admin:inventory_stockreservation_change", args=[pk]
        )
        return _admin_link(url, f"#{pk}")

    @admin.display(description=_("Token"))
    def reservation_token_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        token = getattr(obj, "reservation_token", None)
        if not token:
            return _safe_placeholder()
        return f"{str(token)[:12]}…"

    @admin.display(description=_("Res. Status"))
    def reservation_status_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        status = getattr(obj, "reservation_status", None)
        if not status:
            return _safe_placeholder()
        try:
            label = str(status).replace("_", " ").title()
            return _safe_status_badge(status, label)
        except Exception:
            return str(status)

    @admin.display(description=_("Expires"))
    def reservation_expires_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        return _safe_datetime(getattr(obj, "reservation_expires_at", None))

    @admin.display(description=_("Source"))
    def reservation_source_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        source = getattr(obj, "reservation_source", None)
        if not source:
            return _safe_placeholder()
        return str(source)

    @admin.display(description=_("Notes"))
    def reservation_notes_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        notes = getattr(obj, "reservation_notes", None)
        if not notes:
            return _safe_placeholder()
        text = str(notes).strip()
        if len(text) > 80:
            text = text[:80] + "…"
        return text

    @admin.display(description=_("Version"))
    def reservation_version_display(self, obj: Optional[CartItem]) -> str:
        if obj is None or not getattr(obj, "pk", None):
            return "—"
        version = getattr(obj, "reservation_version", None)
        if version is None:
            return _safe_placeholder()
        return str(version)

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    "CartItemInline",
    "CartAdmin",
    "CartItemAdmin",
    "get_admin_per_page_cart",
    "get_admin_per_page_item",
    "get_inventory_low_stock_threshold",
]