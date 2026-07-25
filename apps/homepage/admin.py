from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    ArtisanStorySection,
    CategorySection,
    HeroCTA,
    HeroSection,
    HomepageSettings,
    SocialProofImage,
    SocialProofSection,
    TrendingProduct,
    TrendingSection,
    TrustBarItem,
    TrustBarSection,
)

def _admin_img_preview(image_field, max_h=70, max_w=70):
    """Reusable admin image preview generator."""
    if not image_field:
        return "-"
    return format_html(
        '<img src="{}" style="max-height: {}px; max-width: {}px; object-fit: cover; border: 1px solid #ddd; padding: 2px; background: #fff;" />',
        image_field.url, max_h, max_w
    )

@admin.register(HomepageSettings)
class HomepageSettingsAdmin(admin.ModelAdmin):
    list_display = ("page_title", "is_active", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("Global Configuration", {"fields": ("page_title", "is_active")}),
        ("System Meta", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def has_add_permission(self, request):
        return not HomepageSettings.objects.exists()

class HeroCTAInline(admin.TabularInline):
    model = HeroCTA
    extra = 2
    fields = ("label", "url", "style", "position")
    ordering = ("position", "id")

@admin.register(HeroSection)
class HeroSectionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "title", "subtitle", "background_preview", "updated_at")
    readonly_fields = ("background_preview", "created_at", "updated_at")
    inlines = [HeroCTAInline]
    fieldsets = (
        ("Hero Content", {"fields": ("subtitle", "title", "background_media", "background_preview")}),
        ("System Meta", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def has_add_permission(self, request):
        return not HeroSection.objects.exists()

    def background_preview(self, obj):
        return _admin_img_preview(obj.background_media if obj else None, max_h=90, max_w=220)
    background_preview.short_description = "Background Preview"

@admin.register(HeroCTA)
class HeroCTAAdmin(admin.ModelAdmin):
    list_display = ("label", "hero_section", "style", "position", "updated_at")
    list_editable = ("position", "style")
    list_filter = ("style",)
    search_fields = ("label", "url")
    ordering = ("position", "id")

class TrustBarItemInline(admin.TabularInline):
    model = TrustBarItem
    extra = 3
    fields = ("icon", "text", "position")
    ordering = ("position", "id")

@admin.register(TrustBarSection)
class TrustBarSectionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_active", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    inlines = [TrustBarItemInline]

    def has_add_permission(self, request):
        return not TrustBarSection.objects.exists()

@admin.register(TrustBarItem)
class TrustBarItemAdmin(admin.ModelAdmin):
    list_display = ("text", "icon", "position", "updated_at")
    list_editable = ("position", "icon")
    search_fields = ("text", "icon")
    ordering = ("position", "id")

@admin.register(CategorySection)
class CategorySectionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "heading", "is_active", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("Category Section Content", {"fields": ("heading", "description", "is_active")}),
        ("System Meta", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def has_add_permission(self, request):
        return not CategorySection.objects.exists()

class TrendingProductInline(admin.TabularInline):
    model = TrendingProduct
    extra = 4
    fields = ("product", "title", "price", "image", "badge", "position")
    ordering = ("position", "id")

@admin.register(TrendingSection)
class TrendingSectionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "heading", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    inlines = [TrendingProductInline]

    def has_add_permission(self, request):
        return not TrendingSection.objects.exists()

@admin.register(TrendingProduct)
class TrendingProductAdmin(admin.ModelAdmin):
    list_display = ("title", "price", "badge", "image_preview", "position", "updated_at")
    list_editable = ("position", "price", "badge")
    search_fields = ("title", "badge")
    ordering = ("position", "id")
    readonly_fields = ("image_preview", "created_at", "updated_at")

    def image_preview(self, obj):
        return _admin_img_preview(obj.image if obj else None)
    image_preview.short_description = "Image Preview"

@admin.register(ArtisanStorySection)
class ArtisanStorySectionAdmin(admin.ModelAdmin):
    list_display = ("artisan_name", "image_preview", "button_text", "updated_at")
    readonly_fields = ("image_preview", "created_at", "updated_at")
    fieldsets = (
        ("Artisan Identity", {"fields": ("artisan", "artisan_name", "image", "image_preview")}),
        ("Story Content", {"fields": ("quote", "bio")}),
        ("Call to Action", {"fields": ("button_text", "target_url")}),
        ("System Meta", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    def has_add_permission(self, request):
        return not ArtisanStorySection.objects.exists()

    def image_preview(self, obj):
        return _admin_img_preview(obj.image if obj else None, max_h=90, max_w=90)
    image_preview.short_description = "Artisan Image Preview"

class SocialProofImageInline(admin.TabularInline):
    model = SocialProofImage
    extra = 4
    fields = ("image", "position")
    ordering = ("position", "id")

@admin.register(SocialProofSection)
class SocialProofSectionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "heading", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    inlines = [SocialProofImageInline]

    def has_add_permission(self, request):
        return not SocialProofSection.objects.exists()

@admin.register(SocialProofImage)
class SocialProofImageAdmin(admin.ModelAdmin):
    list_display = ("__str__", "image_preview", "position", "updated_at")
    list_editable = ("position",)
    ordering = ("position", "id")
    readonly_fields = ("image_preview", "created_at", "updated_at")

    def image_preview(self, obj):
        return _admin_img_preview(obj.image if obj else None)
    image_preview.short_description = "UGC Image Preview"