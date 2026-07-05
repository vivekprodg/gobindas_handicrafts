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
    ProductHighlight,
    TrustBadge,
    ProductLabel,
    ProductIcon,
    ProductSpecification,
    ProductFAQ,
    ProductVideo,
    RecentlyViewedProduct,
    ProductGalleryImage,
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

try:
    from .models import ProductSEO
except ImportError:
    ProductSEO = None

try:
    from .models import ProductSchema
except ImportError:
    ProductSchema = None

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

class ProductGalleryImageInline(admin.TabularInline):
    model = ProductGalleryImage
    extra = 1
    fields = ("image", "alt_text", "caption", "sort_order", "image_preview")
    readonly_fields = ("image_preview",)
    ordering = ("sort_order", "id")

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="max-height: 50px; border-radius: 4px; border: 1px solid #eae5e0;" />', obj.image.url)
        return "-"
    image_preview.short_description = _("Preview")

class ProductSpecificationInline(admin.TabularInline):
    model = ProductSpecification
    extra = 1
    fields = ("label", "value", "display_order")
    ordering = ("display_order", "id")

class ProductFAQInline(admin.TabularInline):
    model = ProductFAQ
    extra = 1
    fields = ("question", "answer", "display_order", "is_active")
    ordering = ("display_order", "id")

class ProductVideoInline(admin.TabularInline):
    model = ProductVideo
    extra = 1
    fields = ("title", "video_url", "thumbnail", "display_order", "thumbnail_preview")
    readonly_fields = ("thumbnail_preview",)
    ordering = ("display_order", "id")

    def thumbnail_preview(self, obj):
        if obj.thumbnail:
            return format_html('<img src="{}" style="max-height: 50px; border-radius: 4px;" />', obj.thumbnail.url)
        return "-"
    thumbnail_preview.short_description = _("Thumbnail Preview")

if ProductVariant:
    class ProductVariantInline(admin.TabularInline):
        model = ProductVariant
        extra = 1
        fields = ("product", "name", "sku", "barcode", "price_override", "compare_price", "stock_quantity", "is_active")
        ordering = ("id",)

if ProductSEO:
    class ProductSEOInline(admin.StackedInline):
        model = ProductSEO
        can_delete = False
        verbose_name_plural = _("SEO Profile Config")

if ProductSchema:
    class ProductSchemaInline(admin.StackedInline):
        model = ProductSchema
        can_delete = False
        verbose_name_plural = _("Schema Config")

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

@admin.register(ProductHighlight)
class ProductHighlightAdmin(admin.ModelAdmin):
    list_display = ("name", "icon_class", "display_order")
    search_fields = ("name",)
    ordering = ("display_order", "name")

@admin.register(TrustBadge)
class TrustBadgeAdmin(admin.ModelAdmin):
    list_display = ("name", "display_order", "image_preview")
    search_fields = ("name",)
    ordering = ("display_order", "name")

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="max-height: 40px; border-radius: 4px;" />', obj.image.url)
        return "-"
    image_preview.short_description = _("Badge Preview")

@admin.register(ProductLabel)
class ProductLabelAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "text_color", "bg_color", "display_order", "color_preview")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("display_order", "name")

    def color_preview(self, obj):
        return format_html(
            '<div style="background-color: {}; color: {}; padding: 4px 8px; border-radius: 4px; display: inline-block; font-weight: bold; border: 1px solid #ddd;">{}</div>',
            obj.bg_color, obj.text_color, obj.name
        )
    color_preview.short_description = _("Visual Preview")

@admin.register(ProductIcon)
class ProductIconAdmin(admin.ModelAdmin):
    list_display = ("name", "icon_class", "display_order", "image_preview")
    search_fields = ("name",)
    ordering = ("display_order", "name")

    def image_preview(self, obj):
        if obj.image:
            return format_html('<img src="{}" style="max-height: 40px; border-radius: 4px;" />', obj.image.url)
        return "-"
    image_preview.short_description = _("Icon Preview")

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("title", "sku", "category", "artisan", "price", "original_price", "stock_status", "is_active", "is_featured", "primary_image_preview")
    list_filter = ("is_active", "is_featured", "status", "category", "artisan", "material", "ethical_standards", "labels", "trust_badges", "icons")
    search_fields = ("title", "sku", "barcode", "short_description", "description")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ["category", "artisan", "material", "hue", "ethical_standards", "highlights", "trust_badges", "labels", "icons"]
    filter_horizontal = ("related_products", "upsell_products", "cross_sell_products")
    readonly_fields = ("view_count", "wishlist_count", "primary_image_preview")

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
            "fields": ("price", "original_price", "stock_status", "stock_text")
        }),
        (_("Shipping & Fulfillment"), {
            "classes": ("collapse",),
            "fields": ("shipping_information", "delivery_promise", "return_policy")
        }),
        (_("Media & Presentation"), {
            "fields": ("primary_image", "hover_image")
        }),
        (_("Presentation / Ribbon & Badges"), {
            "fields": ("badge_text", "secondary_badge_text", "ribbon_text", "ribbon_bg_color", "ribbon_text_color")
        }),
        (_("Marketing Attributes"), {
            "fields": ("material", "hue", "ethical_standards", "highlights", "trust_badges", "labels", "icons")
        }),
        (_("Product Recommendations"), {
            "classes": ("collapse",),
            "fields": ("related_products", "upsell_products", "cross_sell_products")
        }),
        (_("Status & Visibility"), {
            "fields": ("status", "is_active", "is_featured", "position", "published_at", "publish_from", "publish_until")
        }),
        (_("Metrics & Analytics"), {
            "classes": ("collapse",),
            "fields": ("rating", "reviews_count", "view_count", "wishlist_count")
        }),
        (_("SEO Configuration"), {
            "classes": ("collapse",),
            "fields": (
                "seo_title", "seo_description", "seo_keywords",
                "meta_title", "meta_description", "meta_keywords",
                "canonical_url", "robots_directives"
            )
        }),
        (_("Social Graph Metadata"), {
            "classes": ("collapse",),
            "fields": (
                "og_title", "og_description", "og_image",
                "twitter_title", "twitter_description", "twitter_image"
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
        "duplicate_product"
    ]

    def get_inlines(self, request, obj):
        inlines = [
            ProductImageInline,
            ProductGalleryImageInline,
            ProductSpecificationInline,
            ProductFAQInline,
            ProductVideoInline,
        ]
        if ProductVariant:
            inlines.append(ProductVariantInline)
        if ProductSEO:
            inlines.append(ProductSEOInline)
        if ProductSchema:
            inlines.append(ProductSchemaInline)
        return inlines

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "category", "artisan", "hue", "material"
        ).prefetch_related(
            "ethical_standards", "highlights", "trust_badges", "labels", "icons", "related_products"
        )

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

    @admin.action(description=_("Publish selected products"))
    def mark_as_published(self, request, queryset):
        updated = queryset.update(status=Product.ProductStatus.PUBLISHED, is_active=True)
        self.message_user(request, _("Successfully published %d product(s).") % updated)

    @admin.action(description=_("Archive selected products"))
    def mark_as_archived(self, request, queryset):
        updated = queryset.update(status=Product.ProductStatus.ARCHIVED, is_active=False)
        self.message_user(request, _("Successfully archived %d product(s).") % updated)

    @admin.action(description=_("Duplicate selected products"))
    def duplicate_product(self, request, queryset):
        count = 0
        for obj in queryset:
            original_specs = list(obj.specifications.all())
            original_faqs = list(obj.faqs.all())
            original_videos = list(obj.videos.all())

            ethical_standards = list(obj.ethical_standards.all())
            highlights = list(obj.highlights.all())
            trust_badges = list(obj.trust_badges.all())
            labels = list(obj.labels.all())
            icons = list(obj.icons.all())
            related_products = list(obj.related_products.all())
            upsell_products = list(obj.upsell_products.all())
            cross_sell_products = list(obj.cross_sell_products.all())

            obj.pk = None
            obj.id = None
            obj.title = f"{obj.title} (Copy)"

            base_slug = f"{obj.slug or 'product'}-copy"
            new_slug = base_slug
            counter = 1
            while Product.objects.filter(slug=new_slug).exists():
                new_slug = f"{base_slug}-{counter}"
                counter += 1
            obj.slug = new_slug

            if obj.sku:
                base_sku = f"{obj.sku}-copy"
                new_sku = base_sku
                counter = 1
                while Product.objects.filter(sku=new_sku).exists():
                    new_sku = f"{base_sku}-{counter}"
                    counter += 1
                obj.sku = new_sku

            if obj.barcode:
                base_barcode = f"{obj.barcode}-copy"
                new_barcode = base_barcode
                counter = 1
                while Product.objects.filter(barcode=new_barcode).exists():
                    new_barcode = f"{base_barcode}-{counter}"
                    counter += 1
                obj.barcode = new_barcode

            obj.status = Product.ProductStatus.DRAFT
            obj.is_active = False
            obj.save()

            obj.ethical_standards.set(ethical_standards)
            obj.highlights.set(highlights)
            obj.trust_badges.set(trust_badges)
            obj.labels.set(labels)
            obj.icons.set(icons)
            obj.related_products.set(related_products)
            obj.upsell_products.set(upsell_products)
            obj.cross_sell_products.set(cross_sell_products)

            for spec in original_specs:
                spec.pk = None
                spec.id = None
                spec.product = obj
                spec.save()

            for faq in original_faqs:
                faq.pk = None
                faq.id = None
                faq.product = obj
                faq.save()

            for video in original_videos:
                video.pk = None
                video.id = None
                video.product = obj
                video.save()

            count += 1

        self.message_user(request, _("Successfully duplicated %d product(s).") % count)

@admin.register(RecentlyViewedProduct)
class RecentlyViewedProductAdmin(admin.ModelAdmin):
    list_display = ("product", "user_id", "session_key", "viewed_at")
    list_filter = ("viewed_at",)
    search_fields = ("session_key", "product__title")
    readonly_fields = ("product", "user_id", "session_key", "viewed_at")

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