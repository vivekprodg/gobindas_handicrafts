from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator

from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from apps.foundation.services import optimize_uploaded_image

def _upload_to_catalog_media(instance, filename: str) -> str:
    suffix = Path(filename).suffix.lower() or ".webp"
    return f"catalog/media/{uuid.uuid4().hex}{suffix}"

class CatalogSettings(SingletonCMSModel):
    default_items_per_page = models.PositiveIntegerField(
        default=9,
        blank=True, 
        null=True,
        verbose_name="Default Items Per Page",
        help_text="Number of products shown per page in the category listing view."
    )
    price_filter_min = models.PositiveIntegerField(
        default=500,
        blank=True, 
        null=True,
        verbose_name="Price Filter Minimum (NPR)",
        help_text="Minimum range boundary for the price filter slider."
    )
    price_filter_max = models.PositiveIntegerField(
        default=100000,
        blank=True, 
        null=True,
        verbose_name="Price Filter Maximum (NPR)",
        help_text="Maximum range boundary for the price filter slider."
    )
    show_stock_warning_threshold = models.PositiveIntegerField(
        default=5,
        blank=True, 
        null=True,
        verbose_name="Stock Warning Threshold",
        help_text="When inventory drops below or equals this number, show 'Only X Left' status style."
    )

    class Meta:
        verbose_name = "Catalog Settings"
        verbose_name_plural = "Catalog Settings"

    def __str__(self):
        return "Catalog Settings Configuration"

class Category(CMSBaseModel):
    name = models.CharField(
        max_length=120,
        blank=True, 
        null=True,
        verbose_name="Category Name"
    )
    slug = models.SlugField(
        max_length=150,
        unique=True,
        blank=True, 
        null=True,
        verbose_name="Slug"
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subcategories',
        verbose_name="Parent Category",
        help_text="Leave blank if this is a top-level category."
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name="Category Description"
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name="Category Image"
    )
    
    # Core visibility and control fields
    show_on_homepage = models.BooleanField(
        default=False, 
        blank=True, 
        null=True,
        verbose_name="Show on Homepage"
    )
    show_in_menu = models.BooleanField(
        default=True, 
        blank=True, 
        null=True,
        verbose_name="Show in Menu"
    )
    is_active = models.BooleanField(
        default=True, 
        blank=True, 
        null=True,
        verbose_name="Is Active"
    )
    sort_order = models.PositiveIntegerField(
        default=0, 
        blank=True, 
        null=True,
        verbose_name="Sort Order"
    )

    # SEO
    seo_title = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="SEO Title"
    )
    seo_description = models.TextField(
        blank=True,
        null=True,
        verbose_name="SEO Description"
    )

    class Meta:
        verbose_name = "Category"
        verbose_name_plural = "Categories"
        ordering = ["sort_order", "name"]

    def clean(self):
        super().clean()
        if self.parent and self.parent == self:
            raise ValidationError("A category cannot be its own parent.")
        if self.parent and self.parent.parent:
            raise ValidationError("Nesting categories beyond 2 levels (Category -> Subcategory) is not supported.")

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=300 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="catalog/categories"
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        cat_name = self.name or "Unnamed Category"
        if self.parent:
            parent_name = self.parent.name or "Unnamed Category"
            return f"{parent_name} > {cat_name}"
        return cat_name

class Artisan(CMSBaseModel):
    name = models.CharField(
        max_length=120,
        blank=True, 
        null=True,
        verbose_name="Artisan Name"
    )
    slug = models.SlugField(
        max_length=150,
        unique=True,
        blank=True, 
        null=True,
        verbose_name="Slug"
    )
    bio = models.TextField(
        blank=True,
        null=True,
        verbose_name="Biography / Story"
    )
    quote = models.TextField(
        blank=True,
        null=True,
        verbose_name="Highlight Quote"
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name="Artisan Image"
    )
    region = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Origin Region/Province",
        help_text="e.g. Bhaktapur Prov., Patan Valley"
    )
    is_active = models.BooleanField(
        default=True,
        blank=True, 
        null=True,
        verbose_name="Is Active"
    )
    position = models.PositiveIntegerField(
        default=0,
        blank=True, 
        null=True,
        verbose_name="Display Position"
    )

    class Meta:
        verbose_name = "Artisan"
        verbose_name_plural = "Artisans"
        ordering = ["position", "name"]

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=300 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="catalog/artisans"
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or "Unnamed Artisan"

class Material(CMSBaseModel):
    name = models.CharField(
        max_length=100,
        unique=True,
        blank=True, 
        null=True,
        verbose_name="Material Name"
    )

    class Meta:
        verbose_name = "Material"
        verbose_name_plural = "Materials"
        ordering = ["name"]

    def __str__(self):
        return self.name or "Unnamed Material"

class Hue(CMSBaseModel):
    name = models.CharField(
        max_length=100,
        blank=True, 
        null=True,
        verbose_name="Hue Name",
        help_text="e.g. Deep Mahogany, Gold Leaf"
    )
    color_code = models.CharField(
        max_length=50,
        blank=True, 
        null=True,
        verbose_name="Color Hex Code",
        help_text="e.g. #4E2A14"
    )

    class Meta:
        verbose_name = "Hue Aesthetic"
        verbose_name_plural = "Hue Aesthetics"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name or 'Unnamed Hue'} ({self.color_code or 'No Code'})"

class EthicalStandard(CMSBaseModel):
    name = models.CharField(
        max_length=120,
        unique=True,
        blank=True, 
        null=True,
        verbose_name="Ethical Standard",
        help_text="e.g. 100% Certified Fair Trade, Eco-Friendly Sourced Wood"
    )

    class Meta:
        verbose_name = "Ethical Standard"
        verbose_name_plural = "Ethical Standards"
        ordering = ["name"]

    def __str__(self):
        return self.name or "Unnamed Standard"

class Collection(CMSBaseModel):
    name = models.CharField(max_length=120, verbose_name="Collection Name")
    slug = models.SlugField(max_length=150, unique=True)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to=_upload_to_catalog_media, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        verbose_name = "Collection"
        verbose_name_plural = "Collections"
        ordering = ["name"]
        
    def __str__(self):
        return self.name

class Tag(CMSBaseModel):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True)
    
    class Meta:
        verbose_name = "Tag"
        verbose_name_plural = "Tags"
        ordering = ["name"]
        
    def __str__(self):
        return self.name

class VariantType(CMSBaseModel):
    name = models.CharField(max_length=50, verbose_name="Variant Type", help_text="e.g., Size, Color")
    
    class Meta:
        verbose_name = "Variant Type"
        verbose_name_plural = "Variant Types"
        ordering = ["name"]
        
    def __str__(self):
        return self.name

class VariantOption(CMSBaseModel):
    variant_type = models.ForeignKey(VariantType, on_delete=models.CASCADE, related_name="options")
    value = models.CharField(max_length=50, verbose_name="Option Value", help_text="e.g., Small, Red")
    
    class Meta:
        verbose_name = "Variant Option"
        verbose_name_plural = "Variant Options"
        ordering = ["variant_type__name", "value"]
        
    def __str__(self):
        return f"{self.variant_type.name}: {self.value}"

# ==============================================================================
# PREMIUM ECOMMERCE STRUCTURAL MODELS
# ==============================================================================
class ProductHighlight(CMSBaseModel):
    name = models.CharField(max_length=100, unique=True, verbose_name="Highlight Name")
    icon_class = models.CharField(max_length=100, blank=True, null=True, help_text="e.g., fas fa-leaf", verbose_name="Icon Class")
    display_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Display Order")

    class Meta:
        verbose_name = "Product Highlight"
        verbose_name_plural = "Product Highlights"
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

class TrustBadge(CMSBaseModel):
    name = models.CharField(max_length=100, unique=True, verbose_name="Badge Name")
    image = models.ImageField(upload_to=_upload_to_catalog_media, blank=True, null=True, verbose_name="Badge Image")
    display_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Display Order")

    class Meta:
        verbose_name = "Trust Badge"
        verbose_name_plural = "Trust Badges"
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

class ProductLabel(CMSBaseModel):
    name = models.CharField(max_length=50, unique=True, verbose_name="Label Name")
    slug = models.SlugField(max_length=50, unique=True, verbose_name="Slug")
    text_color = models.CharField(max_length=7, default="#FFFFFF", verbose_name="Text Color")
    bg_color = models.CharField(max_length=7, default="#2C2520", verbose_name="Background Color")
    display_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Display Order")

    class Meta:
        verbose_name = "Product Label"
        verbose_name_plural = "Product Labels"
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

class ProductIcon(CMSBaseModel):
    name = models.CharField(max_length=100, unique=True, verbose_name="Icon Name")
    icon_class = models.CharField(max_length=100, blank=True, null=True, help_text="e.g., fas fa-star", verbose_name="Icon Class")
    image = models.ImageField(upload_to=_upload_to_catalog_media, blank=True, null=True, verbose_name="Icon Image")
    display_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Display Order")

    class Meta:
        verbose_name = "Product Icon"
        verbose_name_plural = "Product Icons"
        ordering = ["display_order", "name"]

    def __str__(self):
        return self.name

# ==============================================================================
# MAIN PRODUCT MASTERPIECE MODEL
# ==============================================================================
class Product(CMSBaseModel):
    class StockChoices(models.TextChoices):
        IN_STOCK = "in", "In Stock"
        LOW_STOCK = "low", "Low Stock"
        OUT_OF_STOCK = "out", "Out of Stock"

    class ProductStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    # --- Core Product Identification ---
    title = models.CharField(
        max_length=150,
        blank=True, 
        null=True,
        verbose_name="Product Title"
    )
    slug = models.SlugField(
        max_length=180,
        unique=True,
        blank=True, 
        null=True,
        verbose_name="Slug"
    )
    sku = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="SKU"
    )
    barcode = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Barcode"
    )

    # --- Content & Descriptions ---
    short_description = models.TextField(
        blank=True, 
        null=True,
        verbose_name="Short Description",
        help_text="Displayed in lists and catalogs."
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name="Full Description"
    )

    # --- Premium Narrative Fields ---
    story = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Artisan / Product Story",
        help_text="Legacy narratives and storyboards."
    )
    crafting_process = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Crafting Process",
        help_text="Detailed explanation of how this masterpiece is crafted."
    )
    care_instructions = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Care Instructions",
        help_text="Detailed care steps to preserve heritage materials."
    )
    shipping_information = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Shipping Information"
    )
    delivery_promise = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Delivery Promise"
    )
    return_policy = models.TextField(
        blank=True, 
        null=True, 
        verbose_name="Return Policy"
    )

    # --- Pricing ---
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True, 
        null=True,
        verbose_name="Current Price (NPR)"
    )
    original_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name="Original Price (NPR)",
        help_text="Optional. Populate to trigger sale pricing and line-through original price."
    )

    # --- Product Relationships ---
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT, 
        null=True,
        blank=True,
        related_name="products",
        verbose_name="Category"
    )
    artisan = models.ForeignKey(
        Artisan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name="Master Craftsman"
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name="Material"
    )
    hue = models.ForeignKey(
        Hue,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name="Hue Aesthetic"
    )
    ethical_standards = models.ManyToManyField(
        EthicalStandard,
        blank=True,
        related_name="products",
        verbose_name="Ethical Standards"
    )
    
    # --- Structural Relational Features ---
    highlights = models.ManyToManyField(
        ProductHighlight,
        blank=True,
        related_name="products",
        verbose_name="Product Highlights"
    )
    trust_badges = models.ManyToManyField(
        TrustBadge,
        blank=True,
        related_name="products",
        verbose_name="Trust Badges"
    )
    labels = models.ManyToManyField(
        ProductLabel,
        blank=True,
        related_name="products",
        verbose_name="Product Labels"
    )
    icons = models.ManyToManyField(
        ProductIcon,
        blank=True,
        related_name="products",
        verbose_name="Product Icons"
    )

    # --- Merchandising Relationships ---
    related_products = models.ManyToManyField(
        'self',
        blank=True,
        symmetrical=False,
        related_name="related_to",
        verbose_name="Related Products"
    )
    upsell_products = models.ManyToManyField(
        'self',
        blank=True,
        symmetrical=False,
        related_name="upsold_by",
        verbose_name="Upsell Products"
    )
    cross_sell_products = models.ManyToManyField(
        'self',
        blank=True,
        symmetrical=False,
        related_name="cross_sold_by",
        verbose_name="Cross Sell Products"
    )

    # --- Core Images (Legacy & Primary) ---
    primary_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True, 
        null=True,
        verbose_name="Primary Image"
    )
    hover_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name="Hover Image"
    )

    # --- Physical Dimensions ---
    length = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name="Length", help_text="Length in cm"
    )
    width = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name="Width", help_text="Width in cm"
    )
    height = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name="Height", help_text="Height in cm"
    )
    weight = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0.00'))],
        verbose_name="Weight", help_text="Weight in kg"
    )

    # --- Merchandising & Validation ---
    badge_text = models.CharField(
        max_length=60,
        blank=True,
        null=True,
        verbose_name="Primary Badge Text",
        help_text="e.g. Hand Carved, Limited Edition"
    )
    secondary_badge_text = models.CharField(
        max_length=60,
        blank=True,
        null=True,
        verbose_name="Secondary Badge Text",
        help_text="e.g. Traditional, Bestseller"
    )
    ribbon_text = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Ribbon Text",
        help_text="e.g. Sale, New Arrival"
    )
    ribbon_bg_color = models.CharField(
        max_length=7,
        default="#C5A880",
        verbose_name="Ribbon Background Color",
        help_text="Hex color code, e.g. #C5A880"
    )
    ribbon_text_color = models.CharField(
        max_length=7,
        default="#FFFFFF",
        verbose_name="Ribbon Text Color",
        help_text="Hex color code, e.g. #FFFFFF"
    )
    rating = models.PositiveIntegerField(
        default=5,
        choices=[(i, str(i)) for i in range(1, 6)],
        blank=True, 
        null=True,
        verbose_name="Rating (1-5)"
    )
    reviews_count = models.PositiveIntegerField(
        default=0,
        blank=True, 
        null=True,
        verbose_name="Reviews Count"
    )
    
    # --- Stock & Inventory ---
    stock_status = models.CharField(
        max_length=10,
        choices=StockChoices.choices,
        default=StockChoices.IN_STOCK,
        blank=True, 
        null=True,
        verbose_name="Stock Status"
    )
    stock_text = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Stock Label Overwrite",
        help_text="Optional. Custom text like 'Only 2 Left'. If blank, defaults based on status."
    )

    # --- Analytics & Customer Interactions ---
    view_count = models.PositiveIntegerField(
        default=0,
        blank=True, 
        null=True,
        db_index=True,
        verbose_name="View Count",
        help_text="Denormalized count for recently viewed and trending analytics."
    )
    wishlist_count = models.PositiveIntegerField(
        default=0,
        blank=True, 
        null=True,
        db_index=True,
        verbose_name="Wishlist Count",
        help_text="Denormalized count of how many users have favorited/wishlisted this product."
    )

    # --- Status & Publishing Management ---
    status = models.CharField(
        max_length=20,
        choices=ProductStatus.choices,
        default=ProductStatus.DRAFT,
        db_index=True,
        verbose_name="Product Status"
    )
    is_featured = models.BooleanField(
        default=False,
        blank=True, 
        null=True,
        db_index=True,
        verbose_name="Is Featured"
    )
    is_active = models.BooleanField(
        default=True,
        blank=True, 
        null=True,
        verbose_name="Is Active"
    )
    position = models.PositiveIntegerField(
        default=0,
        blank=True, 
        null=True,
        verbose_name="Display Position"
    )
    published_at = models.DateTimeField(null=True, blank=True, verbose_name="Published At")
    publish_from = models.DateTimeField(null=True, blank=True, verbose_name="Publish From")
    publish_until = models.DateTimeField(null=True, blank=True, verbose_name="Publish Until")

    # --- SEO & Structured Data ---
    seo_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name="SEO Title"
    )
    seo_description = models.TextField(
        blank=True, null=True, verbose_name="SEO Description"
    )
    seo_keywords = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="SEO Keywords"
    )
    meta_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name="Meta Title"
    )
    meta_description = models.TextField(
        blank=True, null=True, verbose_name="Meta Description"
    )
    meta_keywords = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="Meta Keywords"
    )
    canonical_url = models.URLField(
        blank=True, null=True, verbose_name="Canonical URL"
    )
    robots_directives = models.CharField(
        max_length=255, blank=True, null=True, verbose_name="Robots Directives"
    )
    og_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name="Open Graph Title"
    )
    og_description = models.TextField(
        blank=True, null=True, verbose_name="Open Graph Description"
    )
    og_image = models.ImageField(
        upload_to=_upload_to_catalog_media, blank=True, null=True, verbose_name="Open Graph Image"
    )
    twitter_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name="Twitter Title"
    )
    twitter_description = models.TextField(
        blank=True, null=True, verbose_name="Twitter Description"
    )
    twitter_image = models.ImageField(
        upload_to=_upload_to_catalog_media, blank=True, null=True, verbose_name="Twitter Image"
    )
    structured_data = models.JSONField(
        default=dict, blank=True, null=True, verbose_name="Schema.org Structured Data"
    )

    class Meta:
        verbose_name = "Product Masterpiece"
        verbose_name_plural = "Product Masterpieces"
        ordering = ["position", "-created_at"]
        indexes = [
            models.Index(fields=['status', 'is_active']),
            models.Index(fields=['sku']),
            models.Index(fields=['barcode']),
            models.Index(fields=['-wishlist_count']),
            models.Index(fields=['-view_count']),
        ]

    def clean(self):
        super().clean()
        if self.original_price and self.price and self.original_price <= self.price:
            raise ValidationError("Original price must be greater than current price to represent a discount.")

    def save(self, *args, **kwargs):
        if self.primary_image and not getattr(self.primary_image, '_committed', True):
            try:
                optimized = optimize_uploaded_image(
                    self.primary_image.file,
                    target_max_bytes=400 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="catalog/products/primary"
                )
                self.primary_image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

        if self.hover_image and not getattr(self.hover_image, '_committed', True):
            try:
                optimized = optimize_uploaded_image(
                    self.hover_image.file,
                    target_max_bytes=400 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="catalog/products/hover"
                )
                self.hover_image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

        super().save(*args, **kwargs)

    def __str__(self):
        return self.title or "Unnamed Product"

    # =========================================
    # CUSTOMER INTERACTION HELPER METHODS
    # =========================================
    @property
    def favorite_count(self):
        """Computed property for favorite statistics."""
        return self.wishlist_count or 0

    @property
    def wishlist_total(self):
        """Alias for favorite_count to support diverse template definitions."""
        return self.favorite_count

    def increment_view_count(self, commit=True):
        """Helper to efficiently record a product view without race conditions."""
        self.view_count = models.F('view_count') + 1
        if commit:
            self.save(update_fields=['view_count'])
            self.refresh_from_db(fields=['view_count'])

    def increment_wishlist_count(self, commit=True):
        """Helper to safely increment favorite stats."""
        self.wishlist_count = models.F('wishlist_count') + 1
        if commit:
            self.save(update_fields=['wishlist_count'])
            self.refresh_from_db(fields=['wishlist_count'])

    def decrement_wishlist_count(self, commit=True):
        """Helper to safely decrement favorite stats."""
        if self.wishlist_count and self.wishlist_count > 0:
            self.wishlist_count = models.F('wishlist_count') - 1
            if commit:
                self.save(update_fields=['wishlist_count'])
                self.refresh_from_db(fields=['wishlist_count'])

    def get_recommended_products(self, limit=4):
        """
        Extensibility point to support recommendation engines and personalization.
        Returns curated related products, falling back to popular category items.
        """
        related = self.related_products.filter(is_active=True, status=self.ProductStatus.PUBLISHED)
        if related.exists():
            return related[:limit]
        
        # Fallback to trending items within the same category to provide a personalized feed
        if self.category:
            return Product.objects.filter(
                category=self.category,
                is_active=True,
                status=self.ProductStatus.PUBLISHED
            ).exclude(id=self.id).order_by('-wishlist_count', '-view_count')[:limit]
            
        return Product.objects.none()

    @classmethod
    def get_trending_products(cls, limit=10):
        """Analytics dashboard helper: retrieve trending products safely."""
        return cls.objects.filter(
            status=cls.ProductStatus.PUBLISHED, 
            is_active=True
        ).order_by('-wishlist_count', '-view_count')[:limit]

    @classmethod
    def get_popular_products(cls, limit=10):
        """Analytics dashboard helper: retrieve popular products."""
        return cls.objects.filter(
            status=cls.ProductStatus.PUBLISHED, 
            is_active=True
        ).order_by('-view_count', '-reviews_count')[:limit]

# ==============================================================================
# PRODUCT RELATIONAL SPECIFICATION, FAQ, AND VIDEO MODULES
# ==============================================================================
class ProductSpecification(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="specifications",
        verbose_name="Product"
    )
    label = models.CharField(max_length=100, verbose_name="Specification Label")
    value = models.CharField(max_length=255, verbose_name="Specification Value")
    display_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Display Order")

    class Meta:
        verbose_name = "Product Specification"
        verbose_name_plural = "Product Specifications"
        ordering = ["display_order", "label"]

    def __str__(self):
        return f"{self.label}: {self.value}"

class ProductFAQ(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="faqs",
        verbose_name="Product"
    )
    question = models.CharField(max_length=255, verbose_name="Question")
    answer = models.TextField(verbose_name="Answer")
    display_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Display Order")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Is Active")

    class Meta:
        verbose_name = "Product FAQ"
        verbose_name_plural = "Product FAQs"
        ordering = ["display_order", "id"]

    def __str__(self):
        return self.question

class ProductVideo(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="videos",
        verbose_name="Product"
    )
    title = models.CharField(max_length=150, blank=True, null=True, verbose_name="Video Title")
    video_url = models.URLField(verbose_name="Video URL")
    thumbnail = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name="Video Thumbnail"
    )
    display_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Display Order")

    class Meta:
        verbose_name = "Product Video"
        verbose_name_plural = "Product Videos"
        ordering = ["display_order", "id"]

    def __str__(self):
        return self.title or self.video_url

# ==============================================================================
# RECENTLY VIEWED UTILITY MODEL
# ==============================================================================
class RecentlyViewedProduct(CMSBaseModel):
    user_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="User ID")
    session_key = models.CharField(max_length=40, db_index=True, blank=True, null=True, verbose_name="Session Key")
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="recently_viewed",
        verbose_name="Product"
    )
    viewed_at = models.DateTimeField(auto_now=True, db_index=True, verbose_name="Viewed At")

    class Meta:
        verbose_name = "Recently Viewed Product"
        verbose_name_plural = "Recently Viewed Products"
        ordering = ["-viewed_at"]
        indexes = [
            models.Index(fields=["session_key", "-viewed_at"]),
            models.Index(fields=["user_id", "-viewed_at"]),
        ]

    def __str__(self):
        return f"Viewed Product {self.product_id} at {self.viewed_at}"

# ==============================================================================
# LEGACY & ENTERPRISE COMPATIBILITY MODULES
# ==============================================================================
class ProductVariant(CMSBaseModel):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="variants", verbose_name="Product"
    )
    name = models.CharField(max_length=255, verbose_name="Variant Name")
    sku = models.CharField(
        max_length=100, unique=True, null=True, blank=True, db_index=True, verbose_name="SKU"
    )
    barcode = models.CharField(
        max_length=100, unique=True, null=True, blank=True, db_index=True, verbose_name="Barcode"
    )
    price_override = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Price Override"
    )
    compare_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name="Compare At Price"
    )
    stock_quantity = models.PositiveIntegerField(default=0, verbose_name="Stock Quantity")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Is Active")
    sort_order = models.PositiveIntegerField(default=0, db_index=True, verbose_name="Sort Order")
    attributes = models.JSONField(default=dict, blank=True, verbose_name="Attributes")

    class Meta:
        verbose_name = "Product Variant"
        verbose_name_plural = "Product Variants"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.product.title} - {self.name}"

class ProductImage(CMSBaseModel):
    """
    Maintains compatibility with existing gallery implementation and inline admins
    while enhancing it with enterprise-level metadata.
    """
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        blank=True, 
        null=True,
        related_name="gallery_images",
        verbose_name="Product"
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True, 
        null=True,
        verbose_name="Gallery Image"
    )
    title = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Image Title"
    )
    alt_text = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name="Alt Text"
    )
    caption = models.TextField(
        blank=True,
        null=True,
        verbose_name="Caption"
    )
    is_primary = models.BooleanField(
        default=False,
        verbose_name="Is Primary Display Image"
    )
    position = models.PositiveIntegerField(
        default=0,
        blank=True, 
        null=True,
        verbose_name="Display Position"
    )

    class Meta:
        verbose_name = "Product Gallery Image"
        verbose_name_plural = "Product Gallery Images"
        ordering = ["position", "id"]

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=400 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="catalog/products/gallery"
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        product_title = self.product.title if (self.product and self.product.title) else "Unknown Product"
        return f"Gallery Image for {product_title}"

class ProductGalleryImage(CMSBaseModel):
    """
    Additional structured gallery model strictly as requested for separated gallery handling.
    """
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="additional_galleries",
        verbose_name="Product"
    )
    image = models.ImageField(upload_to=_upload_to_catalog_media, verbose_name="Gallery Image")
    alt_text = models.CharField(max_length=150, blank=True, null=True, verbose_name="Alt Text")
    caption = models.TextField(blank=True, null=True, verbose_name="Caption")
    sort_order = models.PositiveIntegerField(default=0, verbose_name="Sort Order")

    class Meta:
        verbose_name = "Additional Product Gallery"
        verbose_name_plural = "Additional Product Galleries"
        ordering = ["sort_order", "id"]

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=400 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="catalog/products/additional_gallery"
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Gallery Image {self.id} for {self.product.title}"

class ProductTag(CMSBaseModel):
    name = models.CharField(max_length=50, unique=True, verbose_name="Tag Name")
    slug = models.SlugField(max_length=50, unique=True, verbose_name="Slug")
    description = models.TextField(blank=True, null=True, verbose_name="Description")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Is Active")
    products = models.ManyToManyField(Product, related_name="tags", blank=True, verbose_name="Tagged Products")

    class Meta:
        verbose_name = "Product Tag"
        verbose_name_plural = "Product Tags"
        ordering = ["name"]

    def __str__(self):
        return self.name

class ProductCollection(CMSBaseModel):
    name = models.CharField(max_length=120, verbose_name="Collection Name")
    slug = models.SlugField(max_length=150, unique=True, verbose_name="Slug")
    description = models.TextField(blank=True, null=True, verbose_name="Description")
    image = models.ImageField(upload_to=_upload_to_catalog_media, blank=True, null=True, verbose_name="Collection Image")
    is_active = models.BooleanField(default=True, db_index=True, verbose_name="Is Active")
    sort_order = models.PositiveIntegerField(default=0, verbose_name="Sort Order")
    products = models.ManyToManyField(Product, related_name="in_collections", blank=True, verbose_name="Collection Products")

    class Meta:
        verbose_name = "Product Collection"
        verbose_name_plural = "Product Collections"
        ordering = ["sort_order", "name"]

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=400 * 1024,
                    max_width=1200,
                    min_width=600,
                    filename_prefix="catalog/collections"
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class ProductSEO(CMSBaseModel):
    product = models.OneToOneField(
        Product, on_delete=models.CASCADE, related_name="seo_config", verbose_name="Product"
    )
    meta_title = models.CharField(max_length=150, blank=True, null=True, verbose_name="Meta Title")
    meta_description = models.TextField(blank=True, null=True, verbose_name="Meta Description")
    keywords = models.CharField(max_length=255, blank=True, null=True, verbose_name="Keywords")
    canonical_url = models.URLField(blank=True, null=True, verbose_name="Canonical URL")
    robots = models.CharField(max_length=255, blank=True, null=True, verbose_name="Robots Directives")
    
    og_title = models.CharField(max_length=150, blank=True, null=True, verbose_name="Open Graph Title")
    og_description = models.TextField(blank=True, null=True, verbose_name="Open Graph Description")
    og_image = models.ImageField(upload_to=_upload_to_catalog_media, blank=True, null=True, verbose_name="Open Graph Image")
    
    twitter_title = models.CharField(max_length=150, blank=True, null=True, verbose_name="Twitter Title")
    twitter_description = models.TextField(blank=True, null=True, verbose_name="Twitter Description")
    twitter_image = models.ImageField(upload_to=_upload_to_catalog_media, blank=True, null=True, verbose_name="Twitter Image")

    class Meta:
        verbose_name = "Product SEO Profile"
        verbose_name_plural = "Product SEO Profiles"

    def __str__(self):
        return f"SEO for {self.product.title}"

class ProductSchema(CMSBaseModel):
    product = models.OneToOneField(
        Product, on_delete=models.CASCADE, related_name="schema_config", verbose_name="Product"
    )
    schema_type = models.CharField(max_length=100, default="Product", verbose_name="Schema Type")
    schema_data = models.JSONField(default=dict, blank=True, verbose_name="Schema JSON Data")
    is_active = models.BooleanField(default=True, verbose_name="Is Active")

    class Meta:
        verbose_name = "Product Schema"
        verbose_name_plural = "Product Schemas"

    def __str__(self):
        return f"Schema for {self.product.title}"