from __future__ import annotations

import logging
from typing import Any, List, Optional, Tuple, Type

from django.contrib import admin
from django.db.models import Model, QuerySet
from django.db.models.signals import post_delete, post_save
from django.http import HttpRequest
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from nested_admin import NestedModelAdmin, NestedTabularInline

from .models import (
    ContactEmail,
    ContactOfficeHour,
    ContactPage,
    ContactPhone,
    ContactSocialLink,
    DigitalBusinessCard,
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

logger = logging.getLogger(__name__)

def _render_image_preview(
    image: Any,
    *,
    max_height: str = "90px",
    max_width: str = "160px",
    object_fit: str = "contain",
    padding: str = "4px",
    placeholder: str = "-",
) -> Any:
    """
    Renders image preview thumbnails with an alpha-transparency checkered pattern,
    ensuring transparent PNGs and WEBPs do not appear to have solid white background boxes.
    """
    try:
        if not image:
            return placeholder
        url = getattr(image, "url", None)
        if not url:
            return placeholder
        return format_html(
            '<div style="display: inline-block; background-image: linear-gradient(45deg, #eAEAEA 25%, transparent 25%), linear-gradient(-45deg, #eAEAEA 25%, transparent 25%), linear-gradient(45deg, transparent 75%, #eAEAEA 75%), linear-gradient(-45deg, transparent 75%, #eAEAEA 75%); background-size: 12px 12px; background-position: 0 0, 0 6px, 6px -6px, -6px 0px; background-color: #ffffff; border: 1px solid #ccc; border-radius: 4px; padding: {};"><img src="{}" style="max-height: {}; max-width: {}; object-fit: {}; display: block;" /></div>',
            padding, url, max_height, max_width, object_fit,
        )
    except Exception:
        return placeholder

class CMSImagePreviewMixin:
    image_preview_field: str = "logo"
    image_preview_max_height: str = "90px"
    image_preview_max_width: str = "160px"
    image_preview_object_fit: str = "contain"
    image_preview_padding: str = "4px"
    image_preview_placeholder: str = "-"

    def get_image_preview(self, obj: Any) -> Any:
        try:
            if obj is None:
                return self.image_preview_placeholder
            image = getattr(obj, self.image_preview_field, None)
        except Exception:
            return self.image_preview_placeholder
        return _render_image_preview(
            image,
            max_height=self.image_preview_max_height,
            max_width=self.image_preview_max_width,
            object_fit=self.image_preview_object_fit,
            padding=self.image_preview_padding,
            placeholder=self.image_preview_placeholder,
        )

class CMSSingletonMixin:
    singleton_model: Optional[Type[Model]] = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        if self.singleton_model is None:
            return super().has_add_permission(request)
        try:
            return not self.singleton_model.objects.exists()
        except Exception:
            return super().has_add_permission(request)

    def has_delete_permission(self, request: HttpRequest, obj: Optional[Model] = None) -> bool:
        if self.singleton_model is None:
            return super().has_delete_permission(request, obj)
        return False

class CMSTimestampMixin:
    cms_timestamp_fields: Tuple[str, ...] = ("created_at", "updated_at")

    def get_readonly_fields(self, request: HttpRequest, obj: Optional[Model] = None) -> Any:
        readonly = list(super().get_readonly_fields(request, obj))
        for field_name in self.cms_timestamp_fields:
            if field_name not in readonly and hasattr(self.model, field_name):
                readonly.append(field_name)
        return tuple(readonly)

class CMSSearchMixin:
    cms_search_fields: Tuple[str, ...] = ()

    def get_search_fields(self, request: HttpRequest) -> Tuple[str, ...]:
        return self.cms_search_fields if self.cms_search_fields else tuple(super().get_search_fields(request))

class CMSOrderingMixin:
    cms_ordering: Any = None

    def get_ordering(self, request: HttpRequest) -> Tuple[str, ...]:
        if self.cms_ordering is not None:
            return (self.cms_ordering,) if isinstance(self.cms_ordering, str) else tuple(self.cms_ordering)
        return tuple(super().get_ordering(request))

class CMSPermissionMixin:
    def has_module_permission(self, request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        return bool(user and getattr(user, "is_active", False) and getattr(user, "is_staff", False))

    def has_view_permission(self, request: HttpRequest, obj: Optional[Model] = None) -> bool:
        return self.has_module_permission(request)

    def has_change_permission(self, request: HttpRequest, obj: Optional[Model] = None) -> bool:
        return self.has_module_permission(request)

    def has_delete_permission(self, request: HttpRequest, obj: Optional[Model] = None) -> bool:
        return self.has_module_permission(request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return self.has_module_permission(request)

class CacheInvalidationMixin:
    def invalidate_foundation_cache(self) -> None:
        try:
            from .services import invalidate_foundation_cms_cache
            invalidate_foundation_cms_cache()
        except Exception as exc:
            logger.warning("Foundation CMS cache invalidation failed: %s", exc)

    def save_model(self, request: HttpRequest, obj: Model, form: Any, change: bool) -> None:
        super().save_model(request, obj, form, change)
        self.invalidate_foundation_cache()

    def delete_model(self, request: HttpRequest, obj: Model) -> None:
        super().delete_model(request, obj)
        self.invalidate_foundation_cache()

    def save_related(self, request: HttpRequest, form: Any, formsets: Any, change: bool) -> None:
        super().save_related(request, form, formsets, change)
        self.invalidate_foundation_cache()

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet) -> None:
        super().delete_queryset(request, queryset)
        self.invalidate_foundation_cache()

class CMSBaseModelAdmin(CacheInvalidationMixin, CMSTimestampMixin, CMSPermissionMixin, admin.ModelAdmin):
    pass

class NavbarChildInline(NestedTabularInline):
    model = NavbarItem
    fk_name = "parent"
    extra = 0
    fields = (
        "label", "slug", "link_url", "open_in_new_tab", "menu_type", "position",
        "icon_key", "badge_text", "badge_style", "visibility_scope", "requires_authentication",
        "start_date", "end_date", "featured_is_visible", "featured_image", "featured_title",
        "featured_text", "featured_cta_text", "featured_cta_url", "featured_start_date", "featured_end_date",
    )
    show_change_link = True

class NavbarMegaMenuLinkInline(NestedTabularInline):
    model = NavbarMegaMenuLink
    extra = 1
    fields = ("label", "link_url", "icon_key", "open_in_new_tab", "is_featured", "position", "visibility_scope", "is_active")

class NavbarMegaMenuColumnInline(NestedTabularInline):
    model = NavbarMegaMenuColumn
    extra = 1
    fields = ("heading", "position", "visibility_scope", "is_active")
    inlines = [NavbarMegaMenuLinkInline]

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

class FooterLinkInline(admin.TabularInline):
    model = FooterLink
    extra = 3
    ordering = ("position", "id")
    fields = ("label", "route", "link_type", "action", "position")

class ContactPhoneInline(admin.TabularInline):
    model = ContactPhone
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "phone_number", "position", "is_visible")

class ContactEmailInline(admin.TabularInline):
    model = ContactEmail
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "email_address", "position", "is_visible")

class ContactSocialLinkInline(admin.TabularInline):
    model = ContactSocialLink
    extra = 1
    ordering = ("position", "id")
    fields = ("platform", "url", "icon_key", "icon_class", "position", "is_visible")

class ContactOfficeHourInline(admin.TabularInline):
    model = ContactOfficeHour
    extra = 1
    ordering = ("position", "id")
    fields = ("day", "opening_time", "closing_time", "status", "position", "is_visible")

@admin.register(SiteSettings)
class SiteSettingsAdmin(CacheInvalidationMixin, CMSSingletonMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "brand_title",
        "company_notification_email",
        "sender_email_address",
        "logo_preview",
        "updated_at",
    )
    readonly_fields = ("logo_preview", "default_featured_preview", "created_at", "updated_at")
    search_fields = (
        "brand_title",
        "brand_subtitle",
        "brand_url",
        "logo_alt_text",
        "company_notification_email",
        "sender_email_address",
    )
    singleton_model = SiteSettings
    fieldsets = (
        (_("Brand Identity"), {"fields": ("logo", "logo_preview", "mobile_logo", "logo_alt_text", "logo_link", "brand_title", "brand_subtitle", "brand_url")}),
        (_("Email & Notification Configurations"), {
            "fields": (
                "company_notification_email",
                "sender_email_address",
                "sender_display_name",
            ),
            "description": _("Configure automated email addresses for customer notifications and company admin registration alerts."),
        }),
        (_("Search & Cart Settings"), {"fields": ("search_placeholder", "search_button_label", "cart_button_label", "cart_badge_count")}),
        (_("Mega Menu Media Fallbacks"), {"fields": ("default_featured_image", "default_featured_preview", "default_featured_title", "default_featured_text")}),
        (_("Feature Flags"), {"fields": ("enable_customer_registration", "enable_guest_checkout", "enable_social_login", "enable_google_login", "enable_facebook_login", "enable_github_login")}),
        (_("System Meta"), {"fields": ("created_at", "updated_at")}),
    )

    def logo_preview(self, obj: Any) -> Any:
        return _render_image_preview(getattr(obj, "logo", None) if obj else None, max_width="220px", object_fit="contain", padding="6px")

    def default_featured_preview(self, obj: Any) -> Any:
        return _render_image_preview(getattr(obj, "default_featured_image", None) if obj else None, max_width="160px", object_fit="cover", padding="4px")

@admin.register(NavbarSettings)
class NavbarSettingsAdmin(CacheInvalidationMixin, CMSSingletonMixin, admin.ModelAdmin):
    list_display = ("id", "is_enabled", "is_sticky", "desktop_behavior", "mobile_behavior", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    singleton_model = NavbarSettings
    fieldsets = (
        (_("Global Navigation Controls"), {"fields": ("is_enabled", "is_sticky", "desktop_behavior", "mobile_behavior")}),
        (_("System Meta"), {"fields": ("created_at", "updated_at")}),
    )

@admin.register(NavbarItem)
class NavbarItemAdmin(CacheInvalidationMixin, NestedModelAdmin):
    list_display = ("label", "menu_type", "parent", "position", "visibility_scope", "requires_authentication", "badge_text", "featured_image_preview", "updated_at")
    list_editable = ("position",)
    list_filter = ("menu_type", "visibility_scope", "requires_authentication", "parent")
    search_fields = ("label", "slug", "link_url", "badge_text", "featured_title")
    ordering = ("position", "label", "id")
    inlines = (NavbarChildInline, NavbarMegaMenuColumnInline)
    readonly_fields = ("featured_image_preview", "created_at", "updated_at")

    fieldsets = (
        (_("Navigation Structure"), {"fields": ("parent", "label", "slug", "link_url", "open_in_new_tab", "menu_type", "position", "visibility_scope", "requires_authentication")}),
        (_("Activation Schedule"), {"fields": ("start_date", "end_date"), "classes": ("collapse",)}),
        (_("Badge & Icon"), {"fields": ("icon_key", "badge_text", "badge_style")}),
        (_("Mega Menu Content"), {"fields": ("featured_is_visible", "featured_image", "featured_image_preview", "featured_title", "featured_text", "featured_cta_text", "featured_cta_url", "featured_start_date", "featured_end_date")}),
        (_("System Meta"), {"fields": ("created_at", "updated_at")}),
    )

    def featured_image_preview(self, obj: Any) -> Any:
        return _render_image_preview(getattr(obj, "featured_image", None) if obj else None, max_width="160px", object_fit="cover", padding="4px")

@admin.register(HeaderBar)
class HeaderBarAdmin(CacheInvalidationMixin, CMSSingletonMixin, admin.ModelAdmin):
    list_display = ("id", "is_enabled", "is_sticky", "rotator_interval_ms", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    inlines = [HeaderAnnouncementInline, HeaderUtilityLinkInline, HeaderCurrencyInline, HeaderLanguageInline, HeaderCountryInline]
    singleton_model = HeaderBar
    fieldsets = (
        (_("Global Visibility Controls"), {"fields": ("is_enabled", "is_sticky", "show_on_desktop", "show_on_mobile")}),
        (_("Top Header Base Config"), {"fields": ("rotator_interval_ms",)}),
        (_("System Meta"), {"fields": ("created_at", "updated_at")}),
    )

@admin.register(FooterSettings)
class FooterSettingsAdmin(CacheInvalidationMixin, CMSSingletonMixin, admin.ModelAdmin):
    list_display = ("id", "brand_name", "newsletter_heading", "copyright_template", "updated_at")
    readonly_fields = ("logo_preview", "created_at", "updated_at")
    search_fields = ("brand_name", "newsletter_heading", "copyright_template")
    singleton_model = FooterSettings
    fieldsets = (
        (_("Brand Statement Profile"), {"fields": ("logo", "logo_preview", "brand_name", "fair_trade_statement")}),
        (_("Newsletter Integration Config"), {"fields": ("newsletter_heading", "newsletter_subtext", "newsletter_endpoint", "newsletter_placeholder")}),
        (_("Legal Compliance & Information Templates"), {"fields": ("copyright_template",)}),
        (_("System Meta Records"), {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def logo_preview(self, obj: Any) -> Any:
        return _render_image_preview(getattr(obj, "logo", None) if obj else None, max_width="220px", object_fit="contain", padding="6px")

@admin.register(FooterSection)
class FooterSectionAdmin(CacheInvalidationMixin, admin.ModelAdmin):
    list_display = ("title", "position", "updated_at")
    list_editable = ("position",)
    search_fields = ("title",)
    ordering = ("position", "id")
    inlines = (FooterLinkInline,)
    readonly_fields = ("created_at", "updated_at")

@admin.register(FooterLink)
class FooterLinkAdmin(CacheInvalidationMixin, admin.ModelAdmin):
    list_display = ("label", "section", "route", "link_type", "position", "updated_at")
    list_editable = ("section", "position")
    list_filter = ("section", "link_type")
    search_fields = ("label", "route", "action")
    ordering = ("section", "position", "id")
    readonly_fields = ("created_at", "updated_at")

@admin.register(FooterSocialLink)
class FooterSocialLinkAdmin(CacheInvalidationMixin, admin.ModelAdmin):
    list_display = ("platform", "url", "icon_key", "icon_class", "position", "is_visible", "updated_at")
    list_editable = ("position", "is_visible")
    list_filter = ("is_visible",)
    search_fields = ("platform", "url", "icon_key", "icon_class")
    ordering = ("position", "id")
    readonly_fields = ("created_at", "updated_at")

@admin.register(FooterPaymentMethod)
class FooterPaymentMethodAdmin(CacheInvalidationMixin, admin.ModelAdmin):
    list_display = ("method_name", "icon_key", "position", "updated_at")
    list_editable = ("position",)
    search_fields = ("method_name", "icon_key")
    ordering = ("position", "id")
    readonly_fields = ("created_at", "updated_at")

@admin.register(FooterTrustBadge)
class FooterTrustBadgeAdmin(CacheInvalidationMixin, admin.ModelAdmin):
    list_display = ("badge_name", "icon_key", "position", "updated_at")
    list_editable = ("position",)
    search_fields = ("badge_name", "icon_key")
    ordering = ("position", "id")
    readonly_fields = ("created_at", "updated_at")

@admin.register(ContactPage)
class ContactPageAdmin(CacheInvalidationMixin, CMSSingletonMixin, admin.ModelAdmin):
    list_display = ("id", "hero_title", "hero_subtitle", "hero_image_preview", "updated_at")
    readonly_fields = ("hero_image_preview", "created_at", "updated_at")
    search_fields = ("hero_title", "hero_subtitle", "intro_heading", "seo_meta_title")
    inlines = [ContactPhoneInline, ContactEmailInline, ContactSocialLinkInline, ContactOfficeHourInline]
    singleton_model = ContactPage
    fieldsets = (
        (_("Hero Banner Settings"), {"fields": ("hero_title", "hero_subtitle", "hero_description", "hero_image", "hero_image_preview")}),
        (_("Introductory Narrative"), {"fields": ("intro_heading", "intro_text")}),
        (_("Physical Location Details"), {"fields": ("address_heading", "physical_address")}),
        (_("Google Maps Integration"), {"fields": ("map_heading", "map_embed_url")}),
        (_("Operating Hours Section Header"), {"fields": ("hours_heading", "hours_description")}),
        (_("Interactive Contact Form Setup"), {"fields": ("form_heading", "form_subheading", "form_submit_button_label", "form_success_message")}),
        (_("Search Engine Optimization (SEO)"), {"fields": ("seo_meta_title", "seo_meta_description", "seo_meta_keywords")}),
        (_("System Meta Records"), {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def hero_image_preview(self, obj: Any) -> Any:
        return _render_image_preview(getattr(obj, "hero_image", None) if obj else None, max_width="160px", object_fit="cover", padding="4px")

@admin.register(DigitalBusinessCard)
class DigitalBusinessCardAdmin(admin.ModelAdmin):
    list_display = ("full_name", "company_name", "phone_number", "email", "qr_code_preview", "download_qr_link", "is_active")
    list_filter = ("is_active", "company_name")
    search_fields = ("full_name", "company_name", "phone_number", "email", "slug")
    prepopulated_fields = {"slug": ("full_name",)}
    readonly_fields = ("qr_code_preview_large", "public_url_display", "created_at", "updated_at")

    fieldsets = (
        (_("Card Owner Identification"), {
            "fields": ("full_name", "title_or_role", "company_name", "slug", "avatar", "is_active")
        }),
        (_("Contact Information"), {
            "fields": ("phone_number", "whatsapp_number", "email", "website", "address", "bio")
        }),
        (_("Social Links"), {
            "fields": ("facebook_url", "instagram_url", "linkedin_url"),
            "classes": ("collapse",)
        }),
        (_("QR Code & Links"), {
            "fields": ("public_url_display", "qr_code_preview_large")
        }),
    )

    actions = ["regenerate_qr_codes_action"]

    def qr_code_preview(self, obj):
        if obj.qr_code_image:
            return format_html('<img src="{}" style="height: 50px; width: 50px;" />', obj.qr_code_image.url)
        return "-"
    qr_code_preview.short_description = _("QR Code")

    def qr_code_preview_large(self, obj):
        if obj.qr_code_image:
            return format_html('<img src="{}" style="height: 200px; width: 200px; border: 1px solid #ccc; padding: 5px;" />', obj.qr_code_image.url)
        return _("Save to generate QR Code")
    qr_code_preview_large.short_description = _("Generated QR Code Image")

    def download_qr_link(self, obj):
        if obj.qr_code_image:
            return format_html(
                '<a class="button" href="{}" download="{}_qr.png" style="background:#B88A44; color:#fff; font-weight:bold; padding: 6px 12px; border-radius: 4px; text-decoration: none;">Download PNG</a>',
                obj.qr_code_image.url,
                obj.slug
            )
        return "-"
    download_qr_link.short_description = _("Visiting Card PNG")

    def public_url_display(self, obj):
        if obj.slug:
            url = f"/card/{obj.slug}/"
            return format_html('<a href="{}" target="_blank">{}</a>', url, url)
        return "-"
    public_url_display.short_description = _("Public Card URL")

    @admin.action(description=_("Regenerate QR Codes for Selected Cards"))
    def regenerate_qr_codes_action(self, request, queryset):
        for card in queryset:
            card.generate_qr_code()
        self.message_user(request, _("QR Codes regenerated successfully."))

CMS_MODELS: List[Type[Model]] = [
    ContactEmail, ContactOfficeHour, ContactPage, ContactPhone, ContactSocialLink,
    DigitalBusinessCard, FooterLink, FooterPaymentMethod, FooterSection, FooterSettings,
    FooterSocialLink, FooterTrustBadge, HeaderBar, HeaderAnnouncement, HeaderCountry,
    HeaderCurrency, HeaderLanguage, HeaderUtilityLink, NavbarItem, NavbarMegaMenuColumn,
    NavbarMegaMenuLink, NavbarSettings, SiteSettings,
]

__all__ = [
    "CMSImagePreviewMixin", "CMSSingletonMixin", "CMSTimestampMixin", "CMSSearchMixin",
    "CMSOrderingMixin", "CMSPermissionMixin", "CacheInvalidationMixin", "CMSBaseModelAdmin",
    "NavbarChildInline", "NavbarMegaMenuLinkInline", "NavbarMegaMenuColumnInline",
    "HeaderAnnouncementInline", "HeaderCurrencyInline", "HeaderLanguageInline", "HeaderCountryInline",
    "HeaderUtilityLinkInline", "FooterLinkInline", "ContactPhoneInline", "ContactEmailInline",
    "ContactSocialLinkInline", "ContactOfficeHourInline", "SiteSettingsAdmin", "NavbarSettingsAdmin",
    "NavbarItemAdmin", "HeaderBarAdmin", "FooterSettingsAdmin", "FooterSectionAdmin",
    "FooterLinkAdmin", "FooterSocialLinkAdmin", "FooterPaymentMethodAdmin", "FooterTrustBadgeAdmin",
    "ContactPageAdmin", "DigitalBusinessCardAdmin", "CMS_MODELS",
]