"""
Enterprise-grade Django Admin configuration for the Catalog application.

This module provides CMS-driven product management with strict separation
from inventory concerns. All inventory-related fields, actions, and filters
have been removed to maintain the architectural boundary between Catalog
(product descriptions) and Inventory (stock management).

Key features:
    * Inventory-agnostic product management
    * Read-only inventory summary display
    * Navigation links to inventory records
    * Future-proof extensible design
    * Enterprise-grade performance optimization
    * OWASP-compliant security practices
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Type

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from apps.catalog.models import (
    Artisan,
    CatalogSettings,
    Category,
    Collection,
    EthicalStandard,
    Hue,
    Material,
    Product,
    ProductCollection,
    ProductFAQ,
    ProductGalleryImage,
    ProductHighlight,
    ProductIcon,
    ProductImage,
    ProductLabel,
    ProductSchema,
    ProductSEO,
    ProductSpecification,
    ProductTag,
    ProductVideo,
    RecentlyViewedProduct,
    TrustBadge,
)
from apps.foundation.admin import CMSBaseModelAdmin

logger = logging.getLogger(__name__)

# ==============================================================================
# INVENTORY INTEGRATION HELPERS
# ==============================================================================
def _get_inventory_summary(product_id: int) -> Dict[str, Any]:
    """
    Fetch read-only inventory summary from Inventory service.
    
    This is a placeholder implementation. In production, this would call
    the inventory service layer to fetch current stock information.
    """
    try:
        # This would normally import from inventory selectors
        # For now, we return mock data to demonstrate the pattern
        return {
            "available_quantity": Decimal("0"),
            "reserved_quantity": Decimal("0"),
            "warehouse_count": 0,
            "status": "unknown",
            "low_stock": False,
        }
    except Exception as e:
        logger.warning("Failed to fetch inventory summary for product %s: %s", product_id, e)
        return {
            "available_quantity": Decimal("0"),
            "reserved_quantity": Decimal("0"),
            "warehouse_count": 0,
            "status": "error",
            "low_stock": False,
        }

def _inventory_admin_url(product_id: int) -> str:
    """
    Generate URL to inventory admin for a product.
    """
    try:
        return reverse("admin:inventory_inventory_changelist") + f"?product__id__exact={product_id}"
    except Exception:
        return "#"

# ==============================================================================
# INLINE CLASSES
# ==============================================================================
class ProductImageInline(admin.TabularInline):
    """Manage product gallery images."""
    model = ProductImage
    extra = 1
    fields = (
        "image_preview",
        "title",
        "alt_text",
        "caption",
        "is_primary",
        "position",
    )
    readonly_fields = ("image_preview",)
    ordering = ("position", "id")

    def image_preview(self, obj: ProductImage) -> str:
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 50px; border-radius: 4px; border: 1px solid #eae5e0;" />',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Preview")

class ProductGalleryImageInline(admin.TabularInline):
    """Manage additional product gallery images."""
    model = ProductGalleryImage
    extra = 1
    fields = (
        "image_preview",
        "alt_text",
        "caption",
        "sort_order",
    )
    readonly_fields = ("image_preview",)
    ordering = ("sort_order", "id")

    def image_preview(self, obj: ProductGalleryImage) -> str:
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 50px; border-radius: 4px;" />',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Preview")

class ProductSpecificationInline(admin.TabularInline):
    """Manage product specifications."""
    model = ProductSpecification
    extra = 1
    fields = ("label", "value", "group", "display_order", "is_active")
    ordering = ("display_order", "id")

class ProductFAQInline(admin.TabularInline):
    """Manage product FAQs."""
    model = ProductFAQ
    extra = 1
    fields = ("question", "answer", "display_order", "is_active")
    ordering = ("display_order", "id")

class ProductVideoInline(admin.TabularInline):
    """Manage product videos."""
    model = ProductVideo
    extra = 1
    fields = (
        "title",
        "video_url",
        "thumbnail_preview",
        "duration_seconds",
        "display_order",
        "is_active",
    )
    readonly_fields = ("thumbnail_preview",)
    ordering = ("display_order", "id")

    def thumbnail_preview(self, obj: ProductVideo) -> str:
        if obj.thumbnail:
            return format_html(
                '<img src="{}" style="max-height: 50px; border-radius: 4px;" />',
                obj.thumbnail.url
            )
        return "-"
    thumbnail_preview.short_description = _("Thumbnail")

# ==============================================================================
# MODEL ADMINS
# ==============================================================================
@admin.register(Category)
class CategoryAdmin(CMSBaseModelAdmin):
    """Manage product categories."""
    list_display = (
        "name",
        "parent",
        "is_active",
        "sort_order",
        "image_preview",
    )
    list_filter = ("is_active", "parent")
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("sort_order", "name")
    fieldsets = (
        (_("Basic Info"), {
            "fields": ("name", "slug", "parent", "description", "image")
        }),
        (_("Visibility & Controls"), {
            "fields": ("is_active", "sort_order", "show_on_homepage", "show_in_menu")
        }),
        (_("SEO Meta Tags"), {
            "fields": ("seo_title", "seo_description"),
            "classes": ("collapse",)
        }),
    )

    def image_preview(self, obj: Category) -> str:
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 40px; border-radius: 4px;" />',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Image Preview")

@admin.register(Artisan)
class ArtisanAdmin(CMSBaseModelAdmin):
    """Manage artisans (brands/craftsmen)."""
    list_display = (
        "name",
        "region",
        "is_active",
        "position",
        "image_preview",
    )
    list_filter = ("is_active", "region")
    search_fields = ("name", "bio", "quote", "region")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("position", "name")

    def image_preview(self, obj: Artisan) -> str:
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 40px; border-radius: 4px;" />',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Profile Preview")

@admin.register(Material)
class MaterialAdmin(CMSBaseModelAdmin):
    """Manage product materials."""
    list_display = ("name",)
    search_fields = ("name",)

@admin.register(Hue)
class HueAdmin(CMSBaseModelAdmin):
    """Manage product hues (colors)."""
    list_display = ("name", "color_code", "color_swatch")
    search_fields = ("name", "color_code")

    def color_swatch(self, obj: Hue) -> str:
        if obj.color_code:
            return format_html(
                '<div style="width: 24px; height: 24px; border-radius: 50%; background-color: {}; border: 1px solid #ccc;"></div>',
                obj.color_code
            )
        return "-"
    color_swatch.short_description = _("Swatch")

@admin.register(EthicalStandard)
class EthicalStandardAdmin(CMSBaseModelAdmin):
    """Manage ethical standards."""
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)

@admin.register(ProductHighlight)
class ProductHighlightAdmin(CMSBaseModelAdmin):
    """Manage product highlights."""
    list_display = ("name", "icon_class", "display_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("display_order", "name")

@admin.register(TrustBadge)
class TrustBadgeAdmin(CMSBaseModelAdmin):
    """Manage trust badges."""
    list_display = ("name", "display_order", "image_preview", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("display_order", "name")

    def image_preview(self, obj: TrustBadge) -> str:
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 40px; border-radius: 4px;" />',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Badge Preview")

@admin.register(ProductLabel)
class ProductLabelAdmin(CMSBaseModelAdmin):
    """Manage product labels."""
    list_display = (
        "name",
        "slug",
        "text_color",
        "bg_color",
        "display_order",
        "color_preview",
        "is_active",
    )
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("display_order", "name")

    def color_preview(self, obj: ProductLabel) -> str:
        return format_html(
            '<div style="background-color: {}; color: {}; padding: 4px 8px; border-radius: 4px; display: inline-block; font-weight: bold; border: 1px solid #ddd;">{}</div>',
            obj.bg_color or "#FFFFFF",
            obj.text_color or "#000000",
            obj.name
        )
    color_preview.short_description = _("Visual Preview")

@admin.register(ProductIcon)
class ProductIconAdmin(CMSBaseModelAdmin):
    """Manage product icons."""
    list_display = (
        "name",
        "icon_class",
        "display_order",
        "image_preview",
        "is_active",
    )
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("display_order", "name")

    def image_preview(self, obj: ProductIcon) -> str:
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 40px; border-radius: 4px;" />',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Icon Preview")

@admin.register(Product)
class ProductAdmin(CMSBaseModelAdmin):
    """Manage products with inventory-agnostic approach."""
    list_display = (
        "title",
        "sku",
        "category",
        "artisan",
        "price",
        "original_price",
        "is_active",
        "is_featured",
        "primary_image_preview",
        "inventory_summary",
    )
    list_filter = (
        "is_active",
        "is_featured",
        "status",
        "category",
        "artisan",
        "material",
        "hue",
        "ethical_standards",
        "labels",
        "trust_badges",
        "icons",
    )
    search_fields = (
        "title",
        "sku",
        "barcode",
        "short_description",
        "description",
    )
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = [
        "category",
        "artisan",
        "material",
        "hue",
        "ethical_standards",
        "highlights",
        "trust_badges",
        "labels",
        "icons",
    ]
    filter_horizontal = (
        "related_products",
        "upsell_products",
        "cross_sell_products",
    )
    readonly_fields = (
        "view_count",
        "wishlist_count",
        "primary_image_preview",
        "inventory_summary",
        "inventory_link",
    )
    inlines = [
        ProductImageInline,
        ProductGalleryImageInline,
        ProductSpecificationInline,
        ProductFAQInline,
        ProductVideoInline,
    ]

    fieldsets = (
        (_("Basic Information"), {
            "fields": ("title", "slug", "sku", "barcode", "category", "artisan")
        }),
        (_("Content & Descriptions"), {
            "fields": ("short_description", "description")
        }),
        (_("Artisan Story & Lineage"), {
            "classes": ("collapse",),
            "fields": ("story", "crafting_process", "care_instructions")
        }),
        (_("Pricing & Inventory"), {
            "fields": ("price", "original_price")
        }),
        (_("Shipping & Fulfillment"), {
            "classes": ("collapse",),
            "fields": ("shipping_information", "delivery_promise", "return_policy")
        }),
        (_("Media & Presentation"), {
            "fields": ("primary_image", "hover_image", "video_url")
        }),
        (_("Presentation / Ribbon & Badges"), {
            "fields": (
                "badge_text",
                "secondary_badge_text",
                "ribbon_text",
                "ribbon_bg_color",
                "ribbon_text_color",
            )
        }),
        (_("Marketing Attributes"), {
            "fields": (
                "material",
                "hue",
                "ethical_standards",
                "highlights",
                "trust_badges",
                "labels",
                "icons",
            )
        }),
        (_("Product Recommendations"), {
            "classes": ("collapse",),
            "fields": ("related_products", "upsell_products", "cross_sell_products")
        }),
        (_("Status & Visibility"), {
            "fields": (
                "status",
                "is_active",
                "is_featured",
                "position",
                "published_at",
                "publish_from",
                "publish_until",
            )
        }),
        (_("Metrics & Analytics"), {
            "classes": ("collapse",),
            "fields": ("rating", "reviews_count", "view_count", "wishlist_count")
        }),
        (_("Inventory Summary"), {
            "fields": ("inventory_summary", "inventory_link"),
            "classes": ("collapse",),
            "description": _("Read-only inventory information from the Inventory application.")
        }),
        (_("SEO Configuration"), {
            "classes": ("collapse",),
            "fields": (
                "seo_title",
                "seo_description",
                "seo_keywords",
                "meta_title",
                "meta_description",
                "meta_keywords",
                "canonical_url",
                "robots_directives",
            )
        }),
        (_("Social Graph Metadata"), {
            "classes": ("collapse",),
            "fields": (
                "og_title",
                "og_description",
                "og_image",
                "twitter_title",
                "twitter_description",
                "twitter_image",
            )
        }),
        (_("Structured Schema Metadata"), {
            "classes": ("collapse",),
            "fields": ("structured_data",)
        }),
    )

    actions = [
        "mark_as_active",
        "mark_as_inactive",
        "mark_as_featured",
        "remove_featured_status",
        "mark_as_published",
        "mark_as_archived",
        "duplicate_product",
    ]

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        """Optimize queryset with related objects."""
        return super().get_queryset(request).select_related(
            "category",
            "artisan",
            "hue",
            "material",
        ).prefetch_related(
            "ethical_standards",
            "highlights",
            "trust_badges",
            "labels",
            "icons",
            "related_products",
        )

    def primary_image_preview(self, obj: Product) -> str:
        if obj.primary_image:
            return format_html(
                '<img src="{}" style="max-height: 40px; border-radius: 4px;" />',
                obj.primary_image.url
            )
        return "-"
    primary_image_preview.short_description = _("Primary Image")

    def inventory_summary(self, obj: Product) -> str:
        """Display read-only inventory summary."""
        if not obj.pk:
            return "-"
        
        summary = _get_inventory_summary(obj.pk)
        
        if summary["status"] == "error":
            return format_html(
                '<span style="color: #999;">Inventory unavailable</span>'
            )
        
        available = summary["available_quantity"]
        reserved = summary["reserved_quantity"]
        warehouses = summary["warehouse_count"]
        low_stock = summary["low_stock"]
        
        status_color = "#2E7D32"  # Green
        if low_stock:
            status_color = "#C62828"  # Red
        elif available <= 0:
            status_color = "#9E9E9E"  # Gray
            
        return format_html(
            '<div style="font-size: 12px;">'
            '<span style="color: {}; font-weight: bold;">{} available</span> '
            '<span style="color: #757575;">({} reserved)</span><br>'
            '<span style="color: #757575;">{} warehouse(s)</span>'
            '</div>',
            status_color,
            available,
            reserved,
            warehouses,
        )
    inventory_summary.short_description = _("Inventory")

    def inventory_link(self, obj: Product) -> str:
        """Provide link to inventory records."""
        if not obj.pk:
            return "-"
        
        url = _inventory_admin_url(obj.pk)
        return format_html(
            '<a href="{}" target="_blank" class="button">View Inventory Records</a>',
            url
        )
    inventory_link.short_description = _("Inventory Management")

    @admin.action(description=_("Mark selected products as Active"))
    def mark_as_active(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(is_active=True)
        self.message_user(
            request,
            _("Successfully activated %d product(s).") % updated
        )

    @admin.action(description=_("Mark selected products as Inactive"))
    def mark_as_inactive(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(is_active=False)
        self.message_user(
            request,
            _("Successfully deactivated %d product(s).") % updated
        )

    @admin.action(description=_("Mark selected products as Featured"))
    def mark_as_featured(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(is_featured=True)
        self.message_user(
            request,
            _("Successfully marked %d product(s) as featured.") % updated
        )

    @admin.action(description=_("Remove Featured status from selected products"))
    def remove_featured_status(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(is_featured=False)
        self.message_user(
            request,
            _("Successfully removed featured status from %d product(s).") % updated
        )

    @admin.action(description=_("Publish selected products"))
    def mark_as_published(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(
            status=Product.ProductStatus.PUBLISHED,
            is_active=True
        )
        self.message_user(
            request,
            _("Successfully published %d product(s).") % updated
        )

    @admin.action(description=_("Archive selected products"))
    def mark_as_archived(self, request: HttpRequest, queryset: QuerySet) -> None:
        updated = queryset.update(
            status=Product.ProductStatus.ARCHIVED,
            is_active=False
        )
        self.message_user(
            request,
            _("Successfully archived %d product(s).") % updated
        )

    @admin.action(description=_("Duplicate selected products"))
    def duplicate_product(self, request: HttpRequest, queryset: QuerySet) -> None:
        count = 0
        for obj in queryset:
            # Clone related objects
            original_specs = list(obj.specifications.all())
            original_faqs = list(obj.faqs.all())
            original_videos = list(obj.videos.all())
            original_images = list(obj.gallery_images.all())
            original_galleries = list(obj.additional_galleries.all())

            # Reset primary key to create new instance
            obj.pk = None
            obj.id = None
            obj.title = f"{obj.title} (Copy)"
            
            # Generate unique slug
            base_slug = f"{obj.slug or 'product'}-copy"
            counter = 1
            new_slug = base_slug
            while Product.objects.filter(slug=new_slug).exists():
                new_slug = f"{base_slug}-{counter}"
                counter += 1
            obj.slug = new_slug

            # Clear inventory-related fields
            obj.sku = None
            obj.barcode = None
            obj.status = Product.ProductStatus.DRAFT
            obj.is_active = False
            obj.save()

            # Clone specifications
            for spec in original_specs:
                spec.pk = None
                spec.id = None
                spec.product = obj
                spec.save()

            # Clone FAQs
            for faq in original_faqs:
                faq.pk = None
                faq.id = None
                faq.product = obj
                faq.save()

            # Clone videos
            for video in original_videos:
                video.pk = None
                video.id = None
                video.product = obj
                video.save()

            # Clone gallery images
            for image in original_images:
                image.pk = None
                image.id = None
                image.product = obj
                image.save()

            # Clone additional galleries
            for gallery in original_galleries:
                gallery.pk = None
                gallery.id = None
                gallery.product = obj
                gallery.save()

            count += 1

        self.message_user(
            request,
            _("Successfully duplicated %d product(s).") % count
        )

@admin.register(ProductTag)
class ProductTagAdmin(CMSBaseModelAdmin):
    """Manage product tags."""
    list_display = ("name", "slug", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("products",)

@admin.register(ProductCollection)
class ProductCollectionAdmin(CMSBaseModelAdmin):
    """Manage product collections."""
    list_display = ("name", "slug", "is_active", "image_preview")
    list_filter = ("is_active",)
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}

    def image_preview(self, obj: ProductCollection) -> str:
        if obj.image:
            return format_html(
                '<img src="{}" style="max-height: 40px; border-radius: 4px;" />',
                obj.image.url
            )
        return "-"
    image_preview.short_description = _("Collection Image")

@admin.register(ProductSEO)
class ProductSEOAdmin(CMSBaseModelAdmin):
    """Manage product SEO configurations."""
    list_display = ("product", "meta_title")
    search_fields = ("product__title", "meta_title")
    raw_id_fields = ("product",)

@admin.register(ProductSchema)
class ProductSchemaAdmin(CMSBaseModelAdmin):
    """Manage product schema configurations."""
    list_display = ("product", "schema_type", "is_active")
    list_filter = ("is_active", "schema_type")
    search_fields = ("product__title", "schema_type")
    raw_id_fields = ("product",)

@admin.register(RecentlyViewedProduct)
class RecentlyViewedProductAdmin(CMSBaseModelAdmin):
    """Manage recently viewed products."""
    list_display = ("product", "user_id", "session_key", "viewed_at")
    list_filter = ("viewed_at",)
    search_fields = ("session_key", "product__title")
    readonly_fields = ("product", "user_id", "session_key", "viewed_at")

@admin.register(CatalogSettings)
class CatalogSettingsAdmin(CMSBaseModelAdmin):
    """Manage catalog-wide settings."""
    list_display = (
        "__str__",
        "default_items_per_page",
        "price_filter_min",
        "price_filter_max",
        "show_stock_warning_threshold",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return not CatalogSettings.objects.exists()

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: Optional[CatalogSettings] = None
    ) -> bool:
        return False