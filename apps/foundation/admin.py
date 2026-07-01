from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html
from nested_admin import NestedModelAdmin, NestedTabularInline

from .models import (
    FooterLink,
    FooterPaymentMethod,
    FooterSection,
    FooterSettings,
    FooterSocialLink,
    FooterTrustBadge,
    HeaderBar,
    HeaderAnnouncement,
    HeaderCountry,
    HeaderCurrency,
    HeaderLanguage,
    HeaderUtilityLink,
    NavbarItem,
    NavbarMegaMenuColumn,
    NavbarMegaMenuLink,
    NavbarSettings,
    SiteSettings,
)

@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "brand_title",
        "brand_subtitle",
        "brand_url",
        "logo_preview",
        "updated_at",
    )
    readonly_fields = ("logo_preview", "default_featured_preview", "created_at", "updated_at")
    search_fields = ("brand_title", "brand_subtitle", "brand_url", "logo_alt_text")
    fieldsets = (
        (
            "Brand Identity",
            {
                "fields": (
                    "logo",
                    "logo_preview",
                    "mobile_logo",
                    "logo_alt_text",
                    "logo_link",
                    "brand_title",
                    "brand_subtitle",
                    "brand_url",
                )
            },
        ),
        (
            "Search & Cart Settings",
            {
                "fields": (
                    "search_placeholder",
                    "search_button_label",
                    "cart_button_label",
                    "cart_badge_count",
                )
            },
        ),
        (
            "Mega Menu Media Fallbacks",
            {
                "fields": (
                    "default_featured_image",
                    "default_featured_preview",
                    "default_featured_title",
                    "default_featured_text",
                )
            },
        ),
        (
            "Feature Flags",
            {
                "fields": (
                    "enable_customer_registration",
                    "enable_guest_checkout",
                    "enable_social_login",
                    "enable_google_login",
                    "enable_facebook_login",
                    "enable_github_login",
                )
            },
        ),
        (
            "System Meta",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def logo_preview(self, obj):
        if not obj or not obj.logo:
            return "-"
        return format_html(
            '<img src="{}" style="max-height: 90px; max-width: 220px; object-fit: contain; border: 1px solid #ddd; padding: 6px; background: #fff;" />',
            obj.logo.url,
        )
    logo_preview.short_description = "Logo Preview"

    def default_featured_preview(self, obj):
        if not obj or not obj.default_featured_image:
            return "-"
        return format_html(
            '<img src="{}" style="max-height: 90px; max-width: 160px; object-fit: cover; border: 1px solid #ddd; padding: 4px; background: #fff;" />',
            obj.default_featured_image.url,
        )
    default_featured_preview.short_description = "Featured Image Preview"

@admin.register(NavbarSettings)
class NavbarSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "is_enabled",
        "is_sticky",
        "desktop_behavior",
        "mobile_behavior",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Global Navigation Controls",
            {
                "fields": (
                    "is_enabled",
                    "is_sticky",
                    "desktop_behavior",
                    "mobile_behavior",
                )
            },
        ),
        (
            "System Meta",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    def has_add_permission(self, request):
        return not NavbarSettings.objects.exists()

class NavbarChildInline(NestedTabularInline):
    model = NavbarItem
    fk_name = "parent"
    extra = 0
    fields = (
        "label",
        "slug",
        "link_url",
        "open_in_new_tab",
        "menu_type",
        "position",
        "icon_key",
        "badge_text",
        "badge_style",
        "visibility_scope",
        "requires_authentication",
        "start_date",
        "end_date",
        "featured_is_visible",
        "featured_image",
        "featured_title",
        "featured_text",
        "featured_cta_text",
        "featured_cta_url",
        "featured_start_date",
        "featured_end_date",
    )
    show_change_link = True
    verbose_name = "Child Menu Item"
    verbose_name_plural = "Child Menu Items"

class NavbarMegaMenuLinkInline(NestedTabularInline):
    model = NavbarMegaMenuLink
    extra = 1
    fields = (
        "label",
        "link_url",
        "icon_key",
        "open_in_new_tab",
        "is_featured",
        "position",
        "visibility_scope",
        "is_active",
    )
    verbose_name = "Mega Menu Link"
    verbose_name_plural = "Mega Menu Links"

class NavbarMegaMenuColumnInline(NestedTabularInline):
    model = NavbarMegaMenuColumn
    extra = 1
    fields = (
        "heading",
        "position",
        "visibility_scope",
        "is_active",
    )
    inlines = [NavbarMegaMenuLinkInline]
    verbose_name = "Mega Menu Column"
    verbose_name_plural = "Mega Menu Columns"

@admin.register(NavbarItem)
class NavbarItemAdmin(NestedModelAdmin):
    list_display = (
        "label",
        "menu_type",
        "parent",
        "position",
        "visibility_scope",
        "requires_authentication",
        "badge_text",
        "featured_image_preview",
        "updated_at",
    )
    list_editable = ("position",)
    list_filter = ("menu_type", "visibility_scope", "requires_authentication", "parent")
    search_fields = ("label", "slug", "link_url", "badge_text", "featured_title")
    ordering = ("position", "label", "id")
    inlines = (NavbarChildInline, NavbarMegaMenuColumnInline)
    readonly_fields = ("featured_image_preview", "created_at", "updated_at")

    fieldsets = (
        (
            "Navigation Structure",
            {
                "fields": (
                    "parent",
                    "label",
                    "slug",
                    "link_url",
                    "open_in_new_tab",
                    "menu_type",
                    "position",
                    "visibility_scope",
                    "requires_authentication",
                )
            },
        ),
        (
            "Activation Schedule",
            {
                "fields": (
                    "start_date",
                    "end_date",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "Badge & Icon",
            {
                "fields": (
                    "icon_key",
                    "badge_text",
                    "badge_style",
                )
            },
        ),
        (
            "Mega Menu Content",
            {
                "fields": (
                    "featured_is_visible",
                    "featured_image",
                    "featured_image_preview",
                    "featured_title",
                    "featured_text",
                    "featured_cta_text",
                    "featured_cta_url",
                    "featured_start_date",
                    "featured_end_date",
                )
            },
        ),
        (
            "System Meta",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    def featured_image_preview(self, obj):
        if not obj or not obj.featured_image:
            return "-"
        return format_html(
            '<img src="{}" style="max-height: 90px; max-width: 160px; object-fit: cover; border: 1px solid #ddd; padding: 4px; background: #fff;" />',
            obj.featured_image.url,
        )

    featured_image_preview.short_description = "Featured Image Preview"

class HeaderAnnouncementInline(admin.TabularInline):
    model = HeaderAnnouncement
    extra = 1
    ordering = ("position", "id")
    fields = ("text", "start_date", "end_date", "priority", "position", "is_visible")

class HeaderCurrencyInline(admin.TabularInline):
    model = HeaderCurrency
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "code", "symbol", "link_url", "position", "is_visible")

class HeaderLanguageInline(admin.TabularInline):
    model = HeaderLanguage
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "code", "link_url", "position", "is_visible")

class HeaderCountryInline(admin.TabularInline):
    model = HeaderCountry
    extra = 1
    ordering = ("position", "id")
    fields = ("name", "code", "link_url", "position", "is_visible")

class HeaderUtilityLinkInline(admin.TabularInline):
    model = HeaderUtilityLink
    extra = 1
    ordering = ("position", "id")
    fields = ("utility_type", "label", "link_url", "side", "icon_key", "show_dropdown_icon", "position", "is_visible")

@admin.register(HeaderBar)
class HeaderBarAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "is_enabled",
        "is_sticky",
        "rotator_interval_ms",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")
    inlines = [
        HeaderAnnouncementInline,
        HeaderUtilityLinkInline,
        HeaderCurrencyInline,
        HeaderLanguageInline,
        HeaderCountryInline,
    ]
    fieldsets = (
        (
            "Global Visibility Controls",
            {
                "fields": (
                    "is_enabled",
                    "is_sticky",
                    "show_on_desktop",
                    "show_on_mobile",
                )
            },
        ),
        (
            "Top Header Base Config",
            {
                "fields": (
                    "rotator_interval_ms",
                )
            },
        ),
        (
            "Legacy JSON/Text Data (Deprecated)",
            {
                "fields": (
                    "currency_label",
                    "language_label",
                    "announcement_messages",
                    "left_utilities",
                    "right_utilities",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "System Meta",
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    def has_add_permission(self, request):
        return not HeaderBar.objects.exists()

# =========================================
# CMS DYNAMIC FOOTER ADMIN REGISTRATION
# =========================================
@admin.register(FooterSettings)
class FooterSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "brand_name",
        "newsletter_heading",
        "copyright_template",
        "updated_at",
    )
    readonly_fields = ("logo_preview", "created_at", "updated_at")
    search_fields = ("brand_name", "newsletter_heading", "copyright_template")
    fieldsets = (
        (
            "Brand Statement Profile",
            {
                "fields": (
                    "logo",
                    "logo_preview",
                    "brand_name",
                    "fair_trade_statement",
                )
            },
        ),
        (
            "Newsletter Integration Config",
            {
                "fields": (
                    "newsletter_heading",
                    "newsletter_subtext",
                    "newsletter_endpoint",
                    "newsletter_placeholder",
                )
            },
        ),
        (
            "Legal Compliance & Information Templates",
            {
                "fields": ("copyright_template",),
            },
        ),
        (
            "System Meta Records",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def has_add_permission(self, request):
        return not FooterSettings.objects.exists()

    def logo_preview(self, obj):
        if not obj or not obj.logo:
            return "-"
        return format_html(
            '<img src="{}" style="max-height: 90px; max-width: 220px; object-fit: contain; border: 1px solid #ddd; padding: 6px; background: #fff;" />',
            obj.logo.url,
        )

    logo_preview.short_description = "Footer Logo Preview"

class FooterLinkInline(admin.TabularInline):
    model = FooterLink
    extra = 3
    ordering = ("position", "id")
    fields = ("label", "route", "link_type", "action", "position")

@admin.register(FooterSection)
class FooterSectionAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "position",
        "updated_at",
    )
    list_editable = ("position",)
    search_fields = ("title",)
    ordering = ("position", "id")
    inlines = (FooterLinkInline,)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Structural Arrangement Settings",
            {
                "fields": (
                    "title",
                    "position",
                )
            },
        ),
        (
            "System Meta Records",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterLink)
class FooterLinkAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "section",
        "route",
        "link_type",
        "position",
        "updated_at",
    )
    list_editable = (
        "section",
        "position",
    )
    list_filter = ("section", "link_type")
    search_fields = ("label", "route", "action")
    ordering = ("section", "position", "id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Target Configuration Setup",
            {
                "fields": (
                    "section",
                    "label",
                    "route",
                )
            },
        ),
        (
            "Functional Actions & Placement Priorities",
            {
                "fields": (
                    "link_type",
                    "action",
                    "position",
                )
            },
        ),
        (
            "System Meta Records",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterSocialLink)
class FooterSocialLinkAdmin(admin.ModelAdmin):
    list_display = (
        "platform",
        "url",
        "icon_key",
        "position",
        "updated_at",
    )
    list_editable = ("position",)
    search_fields = ("platform", "url", "icon_key")
    ordering = ("position", "id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "External Connection Mapping",
            {
                "fields": (
                    "platform",
                    "url",
                    "icon_key",
                    "position",
                )
            },
        ),
        (
            "System Meta Records",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterPaymentMethod)
class FooterPaymentMethodAdmin(admin.ModelAdmin):
    list_display = (
        "method_name",
        "icon_key",
        "position",
        "updated_at",
    )
    list_editable = ("position",)
    search_fields = ("method_name", "icon_key")
    ordering = ("position", "id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Asset Token Validation",
            {
                "fields": (
                    "method_name",
                    "icon_key",
                    "position",
                )
            },
        ),
        (
            "System Meta Records",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterTrustBadge)
class FooterTrustBadgeAdmin(admin.ModelAdmin):
    list_display = (
        "badge_name",
        "icon_key",
        "position",
        "updated_at",
    )
    list_editable = ("position",)
    search_fields = ("badge_name", "icon_key")
    ordering = ("position", "id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            "Compliance Stamp System Mapping",
            {
                "fields": (
                    "badge_name",
                    "icon_key",
                    "position",
                )
            },
        ),
        (
            "System Meta Records",
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )