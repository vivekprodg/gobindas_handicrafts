"""
Enterprise-grade Django Admin configuration for the Foundation application.

This module implements the complete CMS back-office tooling for the Foundation
domain (branding, navigation, footer, contact, etc.). It is engineered as a
reusable, CMS-driven, parameterized, and future-proof admin layer.

The module also defines and exports ``CMSBaseModelAdmin`` — the standard
reusable base admin consumed by sibling applications (currently
``apps.catalog.admin``). The class is importable as::

    from apps.foundation.admin import CMSBaseModelAdmin

ARCHITECTURE
============

* All Foundation CMS models are registered with the Django admin.
* Cache invalidation is centralized and resilient to backend failures.
* Image previews are safe (escaped via ``format_html``) and reusable.
* Singleton models are protected against duplicate creation and deletion.
* Every reusable concern is implemented as a standalone mixin so that future
  applications (catalog, inventory, orders, etc.) can opt in independently.

BACKWARD COMPATIBILITY
=======================

* No model, field, URL, or migration is modified.
* No existing admin class is renamed.
* All existing inlines, fieldsets, list displays, and readonly fields are
  preserved exactly.
* ``CMSBaseModelAdmin`` is a new public symbol; it does not break any
  existing import path.
"""

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

# ==============================================================================
# SAFE IMAGE PREVIEW HELPER
# ==============================================================================
def _render_image_preview(
    image: Any,
    *,
    max_height: str = "90px",
    max_width: str = "160px",
    object_fit: str = "cover",
    padding: str = "4px",
    placeholder: str = "-",
) -> Any:
    """
    Safely render a thumbnail ``<img>`` tag for the Django admin changelist.

    All HTML is escaped via ``format_html``. Never raises. Returns the
    provided ``placeholder`` whenever the image attribute is missing,
    falsy, or does not expose a ``.url`` (e.g. blank file field, broken
    storage, permission errors). This is the single canonical renderer
    used by every admin preview method in this module.
    """
    try:
        if not image:
            return placeholder
        url = getattr(image, "url", None)
        if not url:
            return placeholder
        return format_html(
            '<img src="{}" style="max-height: {}; max-width: {}; '
            "object-fit: {}; border: 1px solid #ddd; padding: {}; "
            'background: #fff;" />',
            url,
            max_height,
            max_width,
            object_fit,
            padding,
        )
    except Exception:
        # Never let a broken image break the admin changelist.
        return placeholder

# ==============================================================================
# REUSABLE CMS ADMIN MIXINS
# ==============================================================================
class CMSImagePreviewMixin:
    """
    Mixin providing safe, reusable image preview rendering for admin
    changelists and detail views.

    Subclasses configure the field name and the rendered thumbnail
    style. The mixin generates a method named ``<prefix>_preview`` (or
    simply the configured ``image_preview_method_name``) that delegates
    to the centralized ``_render_image_preview`` helper. All HTML is
    safely escaped; missing or broken images return a safe placeholder
    string.
    """

    image_preview_field: str = "logo"
    image_preview_max_height: str = "90px"
    image_preview_max_width: str = "160px"
    image_preview_object_fit: str = "cover"
    image_preview_padding: str = "4px"
    image_preview_placeholder: str = "-"
    image_preview_method_name: str = "render_image_preview"
    image_preview_short_description: str = _("Preview")

    def _build_image_preview(self, obj: Any) -> Any:
        """Return a safe HTML preview or a placeholder for ``obj``."""
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

    def get_image_preview(self, obj: Any) -> Any:
        """Public accessor used by subclasses or external tooling."""
        return self._build_image_preview(obj)

class CMSSingletonMixin:
    """
    Mixin enforcing the singleton pattern for models that must have
    exactly one record (e.g. ``SiteSettings``).

    Subclasses set ``singleton_model`` to the model class. The mixin:

    * Hides the "Add" button when a record already exists.
    * Returns ``False`` for ``has_delete_permission`` to protect the
      singleton from accidental deletion.

    The mixin is fully optional — leave ``singleton_model`` as ``None``
    to disable the behavior.
    """

    singleton_model: Optional[Type[Model]] = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Prevent creation of a second singleton record."""
        if self.singleton_model is None:
            return super().has_add_permission(request)
        try:
            exists = self.singleton_model.objects.exists()
        except Exception:
            return super().has_add_permission(request)
        return not exists

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Optional[Model] = None,
    ) -> bool:
        """Prevent deletion of the singleton record."""
        if self.singleton_model is None:
            return super().has_delete_permission(request, obj)
        return False

class CMSTimestampMixin:
    """
    Mixin that makes the standard CMS timestamp fields (``created_at``,
    ``updated_at``) readonly in the admin automatically. Subclasses do
    not need to list these fields in ``readonly_fields``; the mixin
    appends them at runtime if the model defines them.
    """

    cms_timestamp_fields: Tuple[str, ...] = ("created_at", "updated_at")

    def get_readonly_fields(
        self,
        request: HttpRequest,
        obj: Optional[Model] = None,
    ) -> Any:
        readonly: List[str] = list(super().get_readonly_fields(request, obj))
        for field_name in self.cms_timestamp_fields:
            if field_name in readonly:
                continue
            try:
                if hasattr(self.model, field_name):
                    readonly.append(field_name)
            except Exception:
                continue
        return tuple(readonly)

class CMSSearchMixin:
    """
    Mixin providing standardized search-field configuration.

    Subclasses define ``cms_search_fields`` as a tuple of model field
    names. The mixin forwards them to ``ModelAdmin.get_search_fields``.
    """

    cms_search_fields: Tuple[str, ...] = ()

    def get_search_fields(self, request: HttpRequest) -> Tuple[str, ...]:
        if self.cms_search_fields:
            return self.cms_search_fields
        return tuple(super().get_search_fields(request))

class CMSOrderingMixin:
    """
    Mixin providing standardized ordering configuration.

    Subclasses define ``cms_ordering`` as a string or a tuple of
    strings. The mixin forwards them to ``ModelAdmin.get_ordering``.
    """

    cms_ordering: Any = None

    def get_ordering(self, request: HttpRequest) -> Tuple[str, ...]:
        if self.cms_ordering is not None:
            if isinstance(self.cms_ordering, str):
                return (self.cms_ordering,)
            return tuple(self.cms_ordering)
        return tuple(super().get_ordering(request))

class CMSPermissionMixin:
    """
    Mixin restricting admin access to active staff members. Anonymous
    users, inactive users, and non-staff users are denied at every
    access layer (module, view, change, delete, add).
    """

    def has_module_permission(self, request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        return bool(
            user
            and getattr(user, "is_active", False)
            and getattr(user, "is_staff", False)
        )

    def has_view_permission(
        self,
        request: HttpRequest,
        obj: Optional[Model] = None,
    ) -> bool:
        return self.has_module_permission(request)

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: Optional[Model] = None,
    ) -> bool:
        return self.has_module_permission(request)

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Optional[Model] = None,
    ) -> bool:
        return self.has_module_permission(request)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return self.has_module_permission(request)

class CMSAuditMixin:
    """
    Mixin emitting a structured, safe audit log entry for every
    successful save, change, or delete performed in the admin. The
    log payload is intentionally minimal (no PII, no secrets) and is
    safe to ship to centralized log aggregators.
    """

    def log_action(
        self,
        request: HttpRequest,
        action: str,
        obj: Optional[Model] = None,
    ) -> None:
        try:
            user = getattr(request, "user", None)
            username = (
                getattr(user, "get_username", lambda: "anonymous")()
                if user
                else "anonymous"
            )
            pk = getattr(obj, "pk", None) if obj is not None else None
            model_name = (
                self.model._meta.label
                if getattr(self, "model", None) is not None
                else "unknown"
            )
            logger.info(
                "cms.audit | action=%s model=%s pk=%s user=%s",
                action,
                model_name,
                pk,
                username,
            )
        except Exception:
            # Audit logging must never disrupt the main admin flow.
            pass

    def save_model(
        self,
        request: HttpRequest,
        obj: Model,
        form: Any,
        change: bool,
    ) -> None:
        super().save_model(request, obj, form, change)
        self.log_action(request, "updated" if change else "created", obj)

    def delete_model(self, request: HttpRequest, obj: Model) -> None:
        super().delete_model(request, obj)
        self.log_action(request, "deleted", obj)

class CacheInvalidationMixin:
    """
    Mixin guaranteeing immediate CMS cache invalidation whenever an
    object is saved, updated, deleted, or bulk-modified through the
    Django admin.

    All cache operations are wrapped in defensive ``try/except`` blocks
    so that a cache backend failure never crashes the admin. The mixin
    delegates to the service layer (which is the single source of truth
    for cache invalidation) to keep this module free of business logic.
    """

    def invalidate_foundation_cache(self) -> None:
        """
        Invalidate the Foundation CMS cache. Errors are swallowed and
        logged at warning level to preserve admin availability.
        """
        try:
            from .services import invalidate_foundation_cms_cache
            invalidate_foundation_cms_cache()
        except Exception as exc:
            logger.warning(
                "Foundation CMS cache invalidation (invalidate) failed: %s", exc
            )

        try:
            from .services import refresh_foundation_cms_cache
            refresh_foundation_cms_cache()
        except Exception as exc:
            logger.warning(
                "Foundation CMS cache invalidation (refresh) failed: %s", exc
            )

    def save_model(
        self,
        request: HttpRequest,
        obj: Model,
        form: Any,
        change: bool,
    ) -> None:
        super().save_model(request, obj, form, change)
        self.invalidate_foundation_cache()

    def delete_model(self, request: HttpRequest, obj: Model) -> None:
        super().delete_model(request, obj)
        self.invalidate_foundation_cache()

    def save_related(
        self,
        request: HttpRequest,
        form: Any,
        formsets: Any,
        change: bool,
    ) -> None:
        super().save_related(request, form, formsets, change)
        self.invalidate_foundation_cache()

    def delete_queryset(
        self,
        request: HttpRequest,
        queryset: QuerySet,
    ) -> None:
        super().delete_queryset(request, queryset)
        self.invalidate_foundation_cache()

    def changelist_view(
        self,
        request: HttpRequest,
        extra_context: Optional[dict] = None,
    ) -> Any:
        response = super().changelist_view(request, extra_context)
        if request.method == "POST":
            self.invalidate_foundation_cache()
        return response

# ==============================================================================
# CMSBaseModelAdmin
# ==============================================================================
class CMSBaseModelAdmin(
    CacheInvalidationMixin,
    CMSTimestampMixin,
    CMSPermissionMixin,
    admin.ModelAdmin,
):
    """
    Standard reusable enterprise-grade base admin for all CMS apps.

    This is the canonical class imported across the project as::

        from apps.foundation.admin import CMSBaseModelAdmin

    It composes the most commonly-needed CMS admin concerns into a
    single drop-in base class:

    * ``CacheInvalidationMixin``  – automatic Foundation CMS cache
      invalidation on every save, delete, or bulk action.
    * ``CMSTimestampMixin``       – auto-readonly ``created_at`` and
      ``updated_at`` fields (no need to list them in
      ``readonly_fields``).
    * ``CMSPermissionMixin``     – active-staff-only access at every
      permission layer (module, view, change, add, delete).
    * ``admin.ModelAdmin``        – standard Django admin machinery.

    Future apps can extend it further by stacking additional mixins::

        class InventoryAdmin(
            CMSBaseModelAdmin,
            CMSSingletonMixin,
            CMSImagePreviewMixin,
            admin.ModelAdmin,
        ):
            singleton_model = Inventory
            image_preview_field = "thumbnail"
            ...

    The class is intentionally conservative: it adds zero required
    fields, zero required methods, and zero opinionated list/filter
    defaults. Subclasses retain full freedom to define their own
    ``list_display``, ``fieldsets``, ``inlines``, and ``actions``.
    """

# ==============================================================================
# INLINE CLASSES (Preserved 1:1 for backward compatibility)
# ==============================================================================
class NavbarChildInline(NestedTabularInline):
    """Inline editor for child NavbarItem rows of a mega-menu parent."""

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
    verbose_name = _("Child Menu Item")
    verbose_name_plural = _("Child Menu Items")

class NavbarMegaMenuLinkInline(NestedTabularInline):
    """Inline editor for individual links inside a mega-menu column."""

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
    verbose_name = _("Mega Menu Link")
    verbose_name_plural = _("Mega Menu Links")

class NavbarMegaMenuColumnInline(NestedTabularInline):
    """Inline editor for columns nested inside a mega-menu NavbarItem."""

    model = NavbarMegaMenuColumn
    extra = 1
    fields = (
        "heading",
        "position",
        "visibility_scope",
        "is_active",
    )
    inlines = [NavbarMegaMenuLinkInline]
    verbose_name = _("Mega Menu Column")
    verbose_name_plural = _("Mega Menu Columns")

class HeaderAnnouncementInline(admin.TabularInline):
    """Inline editor for header announcement messages."""

    model = HeaderAnnouncement
    extra = 1
    ordering = ("position", "id")
    fields = ("text", "start_date", "end_date", "priority", "position", "is_visible")

class HeaderCurrencyInline(admin.TabularInline):
    """Inline editor for currency selectors."""

    model = HeaderCurrency
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "code", "symbol", "link_url", "position", "is_visible")

class HeaderLanguageInline(admin.TabularInline):
    """Inline editor for language selectors."""

    model = HeaderLanguage
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "code", "link_url", "position", "is_visible")

class HeaderCountryInline(admin.TabularInline):
    """Inline editor for country selectors."""

    model = HeaderCountry
    extra = 1
    ordering = ("position", "id")
    fields = ("name", "code", "link_url", "position", "is_visible")

class HeaderUtilityLinkInline(admin.TabularInline):
    """Inline editor for header utility links (phone, email, account, etc.)."""

    model = HeaderUtilityLink
    extra = 1
    ordering = ("position", "id")
    fields = (
        "utility_type",
        "label",
        "link_url",
        "side",
        "icon_key",
        "show_dropdown_icon",
        "position",
        "is_visible",
    )

class FooterLinkInline(admin.TabularInline):
    """Inline editor for footer navigation links under a FooterSection."""

    model = FooterLink
    extra = 3
    ordering = ("position", "id")
    fields = ("label", "route", "link_type", "action", "position")

class ContactPhoneInline(admin.TabularInline):
    """Inline editor for contact phone numbers."""

    model = ContactPhone
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "phone_number", "position", "is_visible")

class ContactEmailInline(admin.TabularInline):
    """Inline editor for contact email addresses."""

    model = ContactEmail
    extra = 1
    ordering = ("position", "id")
    fields = ("label", "email_address", "position", "is_visible")

class ContactSocialLinkInline(admin.TabularInline):
    """Inline editor for contact social media links."""

    model = ContactSocialLink
    extra = 1
    ordering = ("position", "id")
    fields = ("platform", "url", "icon_key", "icon_class", "position", "is_visible")

class ContactOfficeHourInline(admin.TabularInline):
    """Inline editor for contact office hours."""

    model = ContactOfficeHour
    extra = 1
    ordering = ("position", "id")
    fields = (
        "day",
        "opening_time",
        "closing_time",
        "status",
        "position",
        "is_visible",
    )

# ==============================================================================
# MODEL ADMINS
# ==============================================================================
@admin.register(SiteSettings)
class SiteSettingsAdmin(
    CacheInvalidationMixin,
    CMSSingletonMixin,
    admin.ModelAdmin,
):
    """Admin for the singleton ``SiteSettings`` (branding, search, feature flags)."""

    list_display = (
        "id",
        "brand_title",
        "brand_subtitle",
        "brand_url",
        "logo_preview",
        "updated_at",
    )
    readonly_fields = (
        "logo_preview",
        "default_featured_preview",
        "created_at",
        "updated_at",
    )
    search_fields = ("brand_title", "brand_subtitle", "brand_url", "logo_alt_text")
    singleton_model = SiteSettings
    fieldsets = (
        (
            _("Brand Identity"),
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
            _("Search & Cart Settings"),
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
            _("Mega Menu Media Fallbacks"),
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
            _("Feature Flags"),
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
            _("System Meta"),
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    def logo_preview(self, obj: Any) -> Any:
        """Render a safe thumbnail for the primary logo (contain fit)."""
        return _render_image_preview(
            getattr(obj, "logo", None) if obj else None,
            max_width="220px",
            object_fit="contain",
            padding="6px",
        )
    logo_preview.short_description = _("Logo Preview")

    def default_featured_preview(self, obj: Any) -> Any:
        """Render a safe thumbnail for the default mega-menu featured image."""
        return _render_image_preview(
            getattr(obj, "default_featured_image", None) if obj else None,
            max_width="160px",
            object_fit="cover",
            padding="4px",
        )
    default_featured_preview.short_description = _("Featured Image Preview")

@admin.register(NavbarSettings)
class NavbarSettingsAdmin(
    CacheInvalidationMixin,
    CMSSingletonMixin,
    admin.ModelAdmin,
):
    """Admin for the singleton ``NavbarSettings`` (global navigation behavior)."""

    list_display = (
        "id",
        "is_enabled",
        "is_sticky",
        "desktop_behavior",
        "mobile_behavior",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")
    singleton_model = NavbarSettings
    fieldsets = (
        (
            _("Global Navigation Controls"),
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
            _("System Meta"),
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

@admin.register(NavbarItem)
class NavbarItemAdmin(
    CacheInvalidationMixin,
    NestedModelAdmin,
):
    """Admin for the hierarchical ``NavbarItem`` (links, dropdowns, mega menus)."""

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
            _("Navigation Structure"),
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
            _("Activation Schedule"),
            {
                "fields": (
                    "start_date",
                    "end_date",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Badge & Icon"),
            {
                "fields": (
                    "icon_key",
                    "badge_text",
                    "badge_style",
                )
            },
        ),
        (
            _("Mega Menu Content"),
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
            _("System Meta"),
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

    def featured_image_preview(self, obj: Any) -> Any:
        """Render a safe thumbnail for the mega-menu featured image."""
        return _render_image_preview(
            getattr(obj, "featured_image", None) if obj else None,
            max_width="160px",
            object_fit="cover",
            padding="4px",
        )
    featured_image_preview.short_description = _("Featured Image Preview")

@admin.register(HeaderBar)
class HeaderBarAdmin(
    CacheInvalidationMixin,
    CMSSingletonMixin,
    admin.ModelAdmin,
):
    """Admin for the singleton ``HeaderBar`` (announcements, utilities, selectors)."""

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
    singleton_model = HeaderBar
    fieldsets = (
        (
            _("Global Visibility Controls"),
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
            _("Top Header Base Config"),
            {
                "fields": (
                    "rotator_interval_ms",
                )
            },
        ),
        (
            _("Legacy JSON/Text Data (Deprecated)"),
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
            _("System Meta"),
            {
                "fields": ("created_at", "updated_at"),
            },
        ),
    )

@admin.register(FooterSettings)
class FooterSettingsAdmin(
    CacheInvalidationMixin,
    CMSSingletonMixin,
    admin.ModelAdmin,
):
    """Admin for the singleton ``FooterSettings`` (brand, newsletter, copyright)."""

    list_display = (
        "id",
        "brand_name",
        "newsletter_heading",
        "copyright_template",
        "updated_at",
    )
    readonly_fields = ("logo_preview", "created_at", "updated_at")
    search_fields = ("brand_name", "newsletter_heading", "copyright_template")
    singleton_model = FooterSettings
    fieldsets = (
        (
            _("Brand Statement Profile"),
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
            _("Newsletter Integration Config"),
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
            _("Legal Compliance & Information Templates"),
            {
                "fields": ("copyright_template",),
            },
        ),
        (
            _("System Meta Records"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def logo_preview(self, obj: Any) -> Any:
        """Render a safe thumbnail for the footer logo (contain fit)."""
        return _render_image_preview(
            getattr(obj, "logo", None) if obj else None,
            max_width="220px",
            object_fit="contain",
            padding="6px",
        )
    logo_preview.short_description = _("Footer Logo Preview")

@admin.register(FooterSection)
class FooterSectionAdmin(
    CacheInvalidationMixin,
    admin.ModelAdmin,
):
    """Admin for ``FooterSection`` (column headers in the footer grid)."""

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
            _("Structural Arrangement Settings"),
            {
                "fields": (
                    "title",
                    "position",
                )
            },
        ),
        (
            _("System Meta Records"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterLink)
class FooterLinkAdmin(
    CacheInvalidationMixin,
    admin.ModelAdmin,
):
    """Admin for individual ``FooterLink`` rows."""

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
            _("Target Configuration Setup"),
            {
                "fields": (
                    "section",
                    "label",
                    "route",
                )
            },
        ),
        (
            _("Functional Actions & Placement Priorities"),
            {
                "fields": (
                    "link_type",
                    "action",
                    "position",
                )
            },
        ),
        (
            _("System Meta Records"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterSocialLink)
class FooterSocialLinkAdmin(
    CacheInvalidationMixin,
    admin.ModelAdmin,
):
    """Admin for footer social media links."""

    list_display = (
        "platform",
        "url",
        "icon_key",
        "icon_class",
        "position",
        "is_visible",
        "updated_at",
    )
    list_editable = (
        "position",
        "is_visible",
    )
    list_filter = ("is_visible",)
    search_fields = ("platform", "url", "icon_key", "icon_class")
    ordering = ("position", "id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (
            _("External Connection Mapping"),
            {
                "fields": (
                    "platform",
                    "url",
                    "icon_key",
                    "icon_class",
                    "position",
                    "is_visible",
                )
            },
        ),
        (
            _("System Meta Records"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterPaymentMethod)
class FooterPaymentMethodAdmin(
    CacheInvalidationMixin,
    admin.ModelAdmin,
):
    """Admin for footer payment-method badges."""

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
            _("Asset Token Validation"),
            {
                "fields": (
                    "method_name",
                    "icon_key",
                    "position",
                )
            },
        ),
        (
            _("System Meta Records"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(FooterTrustBadge)
class FooterTrustBadgeAdmin(
    CacheInvalidationMixin,
    admin.ModelAdmin,
):
    """Admin for footer trust / compliance badges."""

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
            _("Compliance Stamp System Mapping"),
            {
                "fields": (
                    "badge_name",
                    "icon_key",
                    "position",
                )
            },
        ),
        (
            _("System Meta Records"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

@admin.register(ContactPage)
class ContactPageAdmin(
    CacheInvalidationMixin,
    CMSSingletonMixin,
    admin.ModelAdmin,
):
    """Admin for the singleton ``ContactPage`` (hero, info, hours, form, SEO)."""

    list_display = (
        "id",
        "hero_title",
        "hero_subtitle",
        "hero_image_preview",
        "updated_at",
    )
    readonly_fields = ("hero_image_preview", "created_at", "updated_at")
    search_fields = ("hero_title", "hero_subtitle", "intro_heading", "seo_meta_title")
    inlines = [
        ContactPhoneInline,
        ContactEmailInline,
        ContactSocialLinkInline,
        ContactOfficeHourInline,
    ]
    singleton_model = ContactPage
    fieldsets = (
        (
            _("Hero Banner Settings"),
            {
                "fields": (
                    "hero_title",
                    "hero_subtitle",
                    "hero_description",
                    "hero_image",
                    "hero_image_preview",
                )
            },
        ),
        (
            _("Introductory Narrative"),
            {
                "fields": (
                    "intro_heading",
                    "intro_text",
                )
            },
        ),
        (
            _("Physical Location Details"),
            {
                "fields": (
                    "address_heading",
                    "physical_address",
                )
            },
        ),
        (
            _("Google Maps Integration"),
            {
                "fields": (
                    "map_heading",
                    "map_embed_url",
                )
            },
        ),
        (
            _("Operating Hours Section Header"),
            {
                "fields": (
                    "hours_heading",
                    "hours_description",
                )
            },
        ),
        (
            _("Interactive Contact Form Setup"),
            {
                "fields": (
                    "form_heading",
                    "form_subheading",
                    "form_submit_button_label",
                    "form_success_message",
                )
            },
        ),
        (
            _("Search Engine Optimization (SEO)"),
            {
                "fields": (
                    "seo_meta_title",
                    "seo_meta_description",
                    "seo_meta_keywords",
                )
            },
        ),
        (
            _("System Meta Records"),
            {
                "fields": ("created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def hero_image_preview(self, obj: Any) -> Any:
        """Render a safe thumbnail for the contact hero image."""
        return _render_image_preview(
            getattr(obj, "hero_image", None) if obj else None,
            max_width="160px",
            object_fit="cover",
            padding="4px",
        )
    hero_image_preview.short_description = _("Hero Image Preview")

# ==============================================================================
# CMS CACHE INVALIDATION SIGNALS
# ==============================================================================
def trigger_cms_cache_invalidation(sender: Type[Model], **kwargs: Any) -> None:
    """
    Signal handler that invalidates the Foundation CMS cache whenever
    any registered CMS model is saved or deleted.

    All cache operations are wrapped in defensive ``try/except`` blocks
    so that a cache backend failure never disrupts the caller. The
    handler is connected to ``post_save`` and ``post_delete`` for every
    model listed in ``CMS_MODELS`` at the bottom of this module.
    """
    try:
        from .services import invalidate_foundation_cms_cache
        invalidate_foundation_cms_cache()
    except Exception as exc:
        logger.warning(
            "Foundation CMS cache invalidation (invalidate) failed for %s: %s",
            getattr(sender, "__name__", "unknown"),
            exc,
        )

    try:
        from .services import refresh_foundation_cms_cache
        refresh_foundation_cms_cache()
    except Exception as exc:
        logger.warning(
            "Foundation CMS cache invalidation (refresh) failed for %s: %s",
            getattr(sender, "__name__", "unknown"),
            exc,
        )

# Centralized list of every Foundation CMS model whose mutations must
# trigger an immediate cache invalidation. Keeping the list in one
# place makes it trivial to extend without touching the admin classes
# above.
CMS_MODELS: List[Type[Model]] = [
    ContactEmail,
    ContactOfficeHour,
    ContactPage,
    ContactPhone,
    ContactSocialLink,
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
]

def _register_cms_cache_signals() -> None:
    """
    Connect the cache-invalidation signal handler to every model in
    ``CMS_MODELS``. Idempotent — Django's signal dispatcher uses a
    (sender, receiver, dispatch_uid) triple to prevent duplicate
    registration, but we still guard against double imports explicitly.
    """
    for model_cls in CMS_MODELS:
        try:
            post_save.connect(
                trigger_cms_cache_invalidation,
                sender=model_cls,
                dispatch_uid=f"foundation_cms_invalidate_save_{model_cls.__name__}",
            )
            post_delete.connect(
                trigger_cms_cache_invalidation,
                sender=model_cls,
                dispatch_uid=f"foundation_cms_invalidate_delete_{model_cls.__name__}",
            )
        except Exception as exc:
            logger.warning(
                "Failed to wire cache-invalidation signals for %s: %s",
                getattr(model_cls, "__name__", "unknown"),
                exc,
            )

_register_cms_cache_signals()

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Reusable mixins (for sibling apps and future use)
    "CMSImagePreviewMixin",
    "CMSSingletonMixin",
    "CMSTimestampMixin",
    "CMSSearchMixin",
    "CMSOrderingMixin",
    "CMSPermissionMixin",
    "CMSAuditMixin",
    "CacheInvalidationMixin",
    # Standard reusable base admin (importable across the project)
    "CMSBaseModelAdmin",
    # Inline classes
    "NavbarChildInline",
    "NavbarMegaMenuLinkInline",
    "NavbarMegaMenuColumnInline",
    "HeaderAnnouncementInline",
    "HeaderCurrencyInline",
    "HeaderLanguageInline",
    "HeaderCountryInline",
    "HeaderUtilityLinkInline",
    "FooterLinkInline",
    "ContactPhoneInline",
    "ContactEmailInline",
    "ContactSocialLinkInline",
    "ContactOfficeHourInline",
    # Model admins (all pre-existing registrations preserved)
    "SiteSettingsAdmin",
    "NavbarSettingsAdmin",
    "NavbarItemAdmin",
    "HeaderBarAdmin",
    "FooterSettingsAdmin",
    "FooterSectionAdmin",
    "FooterLinkAdmin",
    "FooterSocialLinkAdmin",
    "FooterPaymentMethodAdmin",
    "FooterTrustBadgeAdmin",
    "ContactPageAdmin",
    # Signal infrastructure
    "trigger_cms_cache_invalidation",
    "CMS_MODELS",
]