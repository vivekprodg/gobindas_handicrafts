"""
Rich Django Admin interfaces for managing Coupons, CMS Settings, and Redemption Ledger.
"""
from __future__ import annotations

from typing import Any
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .forms import CouponAdminForm
from .models import Coupon, CouponCMSSetting, CouponUsageRecord

class CouponUsageRecordInline(admin.TabularInline):
    model = CouponUsageRecord
    extra = 0
    readonly_fields = ["user", "order", "discount_amount", "used_at", "is_reversed", "reversal_reason"]
    can_delete = False
    ordering = ["-used_at"]

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False

@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    form = CouponAdminForm
    list_display = [
        "code",
        "title",
        "discount_type",
        "discount_value",
        "min_subtotal",
        "times_used",
        "usage_limit_total",
        "valid_from",
        "valid_to",
        "is_active",
        "is_public",
        "auto_apply",
    ]
    list_filter = [
        "discount_type",
        "target_scope",
        "customer_scope",
        "is_active",
        "is_public",
        "auto_apply",
        "stackable",
        "exclude_sale_items",
    ]
    search_fields = ["code", "title", "description", "promo_badge_text"]
    ordering = ["-created_at"]
    readonly_fields = ["times_used", "created_at", "updated_at"]
    inlines = [CouponUsageRecordInline]
    filter_horizontal = [
        "target_categories",
        "target_products",
        "target_artisans",
        "target_collections",
        "target_customers",
    ]

    fieldsets = (
        (_("Core Identification"), {
            "fields": ("code", "title", "description", "promo_badge_text", "is_active", "is_public", "auto_apply")
        }),
        (_("Discount Rules & Constraints"), {
            "fields": ("discount_type", "discount_value", "max_discount_amount", "min_subtotal", "stackable", "exclude_sale_items")
        }),
        (_("Targeting Scope & Restrictions"), {
            "fields": ("target_scope", "target_categories", "target_products", "target_artisans", "target_collections")
        }),
        (_("Customer Eligibility"), {
            "fields": ("customer_scope", "target_customers")
        }),
        (_("Validity Window & Usage Limits"), {
            "fields": ("valid_from", "valid_to", "usage_limit_total", "usage_limit_per_user", "times_used")
        }),
        (_("System Timestamps"), {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at")
        }),
    )

@admin.register(CouponUsageRecord)
class CouponUsageRecordAdmin(admin.ModelAdmin):
    list_display = ["coupon", "user", "order", "discount_amount", "used_at", "is_reversed"]
    list_filter = ["is_reversed", "used_at"]
    search_fields = ["coupon__code", "user__username", "user__email", "order__order_number"]
    readonly_fields = ["coupon", "user", "order", "discount_amount", "used_at", "is_reversed", "reversal_reason"]
    ordering = ["-used_at"]

@admin.register(CouponCMSSetting)
class CouponCMSSettingAdmin(admin.ModelAdmin):
    list_display = ["public_section_title", "enable_coupon_system", "show_public_coupons_in_cart", "auto_apply_best_coupon"]

    def has_add_permission(self, request: Any) -> bool:
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return False