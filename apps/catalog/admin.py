from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from .models import (
    CatalogSettings,
    Category,
    Artisan,
    Material,
    Hue,
    EthicalStandard,
    Product,
    ProductImage,
)

try:
    from .models import ProductVariant
except ImportError:
    ProductVariant = None

try:
    from .models import ProductTag
except ImportError:
    ProductTag = None

try:
    from .models import ProductCollection
except ImportError:
    ProductCollection = None


class SubcategoryInline(admin.TabularInline):
    model = Category
    fk_name = "parent"
    extra = 1
    prepopulated_fields = {"slug": ("name",)}
    verbose_name = _("Subcategory")
    verbose_name_plural = _("Subcategories")


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1
    fields = ("image", "title", "alt_text", "caption", "is_primary", "position", "image_preview")
    readonly_fields = ("image_preview",)
    ordering = ("position", "id")

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="max-height: 50px; border-radius: 4px; border: 1px solid #eae5e0;" />', obj.image.url)
        return "-"
    image_preview.short_description = _("Preview")


if ProductVariant:
    class ProductVariantInline(admin.TabularInline):
        model = ProductVariant
        extra = 1
        fields = ("product", "name", "sku", "barcode", "price_override", "compare_price", "stock_quantity", "is_active")
        ordering = ("id",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "parent", "is_active", "sort_order", "image_preview")
    list_filter = ("is_active", "parent")
    search_fields = ("name", "description")
    prepopulated_fields = {"slug": ("name",)}
    inlines = [SubcategoryInline]
    ordering = ("sort_order", "name")
    fieldsets = (
        (_("Basic Info"), {"fields": ("name", "slug", "parent", "description", "image")}),
        (_("Visibility & Controls"), {"fields": ("is_active", "sort_order")}),
        (_("SEO Meta Tags"), {"fields": ("seo_title", "seo_description"), "classes": ("collapse",)}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent")

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="max-height: 40px; border-radius: 4px;" />', obj.image.url)
        return "-"
    image_preview.short_description = _("Image Preview")


@admin.register(Artisan)
class ArtisanAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "region", "is_active", "position", "image_preview")
    list_filter = ("is_active", "region")
    search_fields = ("name", "bio", "quote", "region")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("position", "name")

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="max-height: 40px; border-radius: 4px;" />', obj.image.url)
        return "-"
    image_preview.short_description = _("Profile Preview")


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Hue)
class HueAdmin(admin.ModelAdmin):
    list_display = ("name", "color_code", "color_swatch")
    search_fields = ("name", "color_code")

    def color_swatch(self, obj):
        if obj.color_code:
            return format_html(
                '<div style="width: 24px; height: 24px; border-radius: 50%; background-color: {}; border: 1px solid #ccc;"></div>',
                obj.color_code
            )
        return "-"
    color_swatch.short_description = _("Swatch")


@admin.register(EthicalStandard)
class EthicalStandardAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("title", "sku", "category", "artisan", "price", "original_price", "stock_status", "is_active", "is_featured", "primary_image_preview")
    list_filter = ("is_active", "is_featured", "status", "category", "artisan", "material", "ethical_standards")
    search_fields = ("title", "sku", "barcode", "short_description", "description")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ["category", "artisan", "material", "hue", "ethical_standards"]
    filter_horizontal = ("related_products",)
    fieldsets = (
        (_("Core Information"), {"fields": ("title", "slug", "sku", "barcode", "category", "artisan", "short_description", "description")}),
        (_("Pricing & Inventory"), {"fields": ("price", "original_price", "stock_status", "stock_text")}),
        (_("Media & Presentation"), {"fields": ("primary_image", "hover_image", "badge_text", "secondary_badge_text")}),
        (_("Attributes & Metadata"), {"fields": ("material", "hue", "ethical_standards", "related_products")}),
        (_("Status & Visibility"), {"fields": ("status", "is_active", "is_featured", "position", "published_at", "publish_from", "publish_until")}),
        (_("Metrics"), {"fields": ("rating", "reviews_count")}),
        (_("SEO Configurations"), {"fields": ("seo_title", "seo_description", "meta_keywords"), "classes": ("collapse",)}),
    )
    actions = ["mark_as_active", "mark_as_inactive", "mark_as_featured", "remove_featured_status"]

    def get_inlines(self, request, obj):
        inlines = [ProductImageInline]
        if ProductVariant:
            inlines.append(ProductVariantInline)
        return inlines

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("category", "artisan", "hue", "material").prefetch_related("ethical_standards", "related_products")

    def primary_image_preview(self, obj):
        if obj.primary_image:
            return format_html('<img src="{}" style="max-height: 40px; border-radius: 4px;" />', obj.primary_image.url)
        return "-"
    primary_image_preview.short_description = _("Primary Image")

    @admin.action(description=_("Mark selected products as Active"))
    def mark_as_active(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description=_("Mark selected products as Inactive"))
    def mark_as_inactive(self, request, queryset):
        queryset.update(is_active=False)

    @admin.action(description=_("Mark selected products as Featured"))
    def mark_as_featured(self, request, queryset):
        queryset.update(is_featured=True)

    @admin.action(description=_("Remove Featured status from selected products"))
    def remove_featured_status(self, request, queryset):
        queryset.update(is_featured=False)


if ProductTag:
    @admin.register(ProductTag)
    class ProductTagAdmin(admin.ModelAdmin):
        list_display = ("name", "slug", "is_active")
        list_filter = ("is_active",)
        search_fields = ("name", "description")
        prepopulated_fields = {"slug": ("name",)}
        filter_horizontal = ("products",)


if ProductCollection:
    @admin.register(ProductCollection)
    class ProductCollectionAdmin(admin.ModelAdmin):
        list_display = ("name", "slug", "is_active", "image_preview")
        list_filter = ("is_active",)
        search_fields = ("name", "description")
        prepopulated_fields = {"slug": ("name",)}

        def image_preview(self, obj):
            if obj.image:
                return format_html('<img src="{}" style="max-height: 40px; border-radius: 4px;" />', obj.image.url)
            return "-"
        image_preview.short_description = _("Collection Image")


@admin.register(CatalogSettings)
class CatalogSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "default_items_per_page", "price_filter_min", "price_filter_max", "show_stock_warning_threshold")

    def has_add_permission(self, request):
        return not CatalogSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False