from __future__ import annotations

import uuid
from pathlib import Path

from django.db import models
from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from apps.foundation.services import optimize_uploaded_image
from apps.catalog.models import Category, Product, Artisan

def _upload_to_homepage_media(instance, filename: str) -> str:
    """
    Stores homepage media files in a dedicated CMS path.
    (Note: This acts as a fallback. The optimize_uploaded_image service handles the primary file naming now).
    """
    suffix = Path(filename).suffix.lower() or ".webp"
    return f"homepage/media/{uuid.uuid4().hex}{suffix}"


# =========================================
# GLOBAL HOMEPAGE SETTINGS
# =========================================
class HomepageSettings(SingletonCMSModel):
    """
    Global configuration for the homepage metadata or general toggles.
    """
    page_title = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Page Title",
        help_text="Title tag for the homepage."
    )
    is_active = models.BooleanField(
        default=True,
        blank=True, 
        null=True,
        verbose_name="Is Homepage Active"
    )

    class Meta:
        verbose_name = "Homepage Settings"
        verbose_name_plural = "Homepage Settings"

    def __str__(self):
        return "Homepage Global Settings"


# =========================================
# 1. DYNAMIC HERO MODULE
# =========================================
class HeroSection(SingletonCMSModel):
    subtitle = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Hero Subtitle"
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name="Hero Title"
    )
    background_media = models.ImageField(
        upload_to=_upload_to_homepage_media,
        blank=True,
        null=True,
        verbose_name="Background Image"
    )

    class Meta:
        verbose_name = "Hero Section"
        verbose_name_plural = "Hero Section"

    def save(self, *args, **kwargs):
        # Intercept and optimize hero background uploads
        if self.background_media and not getattr(self.background_media, '_committed', True):
            try:
                optimized_result = optimize_uploaded_image(
                    self.background_media.file,
                    target_max_bytes=500 * 1024,
                    max_width=1920, # Wider allowance for full-screen hero banners
                    min_width=1024,
                    filename_prefix="homepage/media/hero"
                )
                self.background_media.save(optimized_result.filename, optimized_result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return "Dynamic Hero Configuration"

class HeroCTA(CMSBaseModel):
    class StyleChoices(models.TextChoices):
        PRIMARY = "btn-primary", "Primary (Gold)"
        OUTLINE = "btn-outline", "Outline (White)"

    hero_section = models.ForeignKey(
        HeroSection,
        related_name="ctas",
        on_delete=models.CASCADE,
        blank=True,
        null=True
    )
    label = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Button Label"
    )
    url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Target URL"
    )
    style = models.CharField(
        max_length=20,
        choices=StyleChoices.choices,
        blank=True,
        null=True,
        verbose_name="Button Style"
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Display Order"
    )

    class Meta:
        verbose_name = "Hero CTA Button"
        verbose_name_plural = "Hero CTA Buttons"
        ordering = ["position", "id"]

    def __str__(self):
        return self.label or f"Hero CTA (ID: {self.id})"


# =========================================
# 2. TRUST BAR MODULE
# =========================================
class TrustBarSection(SingletonCMSModel):
    is_active = models.BooleanField(
        default=True,
        blank=True, 
        null=True,
        verbose_name="Show Trust Bar"
    )

    class Meta:
        verbose_name = "Trust Bar Section"
        verbose_name_plural = "Trust Bar Section"

    def __str__(self):
        return "Trust Bar Configuration"

class TrustBarItem(CMSBaseModel):
    trust_bar = models.ForeignKey(
        TrustBarSection,
        related_name="signals",
        on_delete=models.CASCADE,
        blank=True,
        null=True
    )
    icon = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Icon Key",
        help_text="Matches frontend SVG mapping (e.g., 'fair-trade', 'shipping', 'artisan')"
    )
    text = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Trust Signal Text"
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Display Order"
    )

    class Meta:
        verbose_name = "Trust Bar Signal"
        verbose_name_plural = "Trust Bar Signals"
        ordering = ["position", "id"]

    def __str__(self):
        return self.text or f"Trust Signal (ID: {self.id})"


# =========================================
# 3. VISUAL DISCOVERY (CATEGORIES) MODULE
# =========================================
class CategorySection(SingletonCMSModel):
    """
    Only maintains display settings for the category visual discovery module on the homepage.
    The actual categories are dynamically fetched from apps.catalog.models.Category where show_on_homepage=True.
    """
    heading = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Section Heading"
    )
    description = models.TextField(
        blank=True, 
        null=True,
        verbose_name="Section Description"
    )
    is_active = models.BooleanField(
        default=True,
        blank=True, 
        null=True,
        verbose_name="Section Enabled"
    )

    class Meta:
        verbose_name = "Visual Discovery Section"
        verbose_name_plural = "Visual Discovery Section"

    def __str__(self):
        return "Visual Discovery Configuration"


# =========================================
# 4. MERCHANDISING CAROUSEL MODULE
# =========================================
class TrendingSection(SingletonCMSModel):
    heading = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Section Heading"
    )

    class Meta:
        verbose_name = "Merchandising Carousel"
        verbose_name_plural = "Merchandising Carousel"

    def __str__(self):
        return "Trending Artifacts Configuration"

class TrendingProduct(CMSBaseModel):
    trending_section = models.ForeignKey(
        TrendingSection,
        related_name="items",
        on_delete=models.CASCADE,
        blank=True,
        null=True
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        verbose_name="Linked Catalog Product",
        help_text="If set, details like Title, Price, Image and Badge will be resolved dynamically from the catalog product."
    )
    title = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Product Title"
    )
    price = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Display Price",
        help_text="e.g., $120.00"
    )
    image = models.ImageField(
        upload_to=_upload_to_homepage_media,
        blank=True,
        null=True,
        verbose_name="Product Image"
    )
    badge = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Availability Badge",
        help_text="Optional. e.g., 'Only 1 Left', 'New Kiln Run'"
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Display Order"
    )

    class Meta:
        verbose_name = "Trending Product"
        verbose_name_plural = "Trending Products"
        ordering = ["position", "id"]

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized_result = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=500 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="homepage/media/trending"
                )
                self.image.save(optimized_result.filename, optimized_result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title or f"Trending Product (ID: {self.id})"


# =========================================
# 5. MEET THE MAKER (STORY SPLIT) MODULE
# =========================================
class ArtisanStorySection(SingletonCMSModel):
    artisan = models.ForeignKey(
        Artisan,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        verbose_name="Linked Catalog Artisan",
        help_text="If set, name, bio, quote, and image will be resolved dynamically from the catalog artisan."
    )
    artisan_name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Artisan Name"
    )
    image = models.ImageField(
        upload_to=_upload_to_homepage_media,
        blank=True,
        null=True,
        verbose_name="Artisan Image"
    )
    quote = models.TextField(
        blank=True,
        null=True,
        verbose_name="Highlight Quote"
    )
    bio = models.TextField(
        blank=True,
        null=True,
        verbose_name="Biography / Story"
    )
    button_text = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="Button Text"
    )
    target_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Target URL"
    )

    class Meta:
        verbose_name = "Meet The Maker Section"
        verbose_name_plural = "Meet The Maker Section"

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized_result = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=500 * 1024,
                    max_width=1000,
                    min_width=600,
                    filename_prefix="homepage/media/artisan"
                )
                self.image.save(optimized_result.filename, optimized_result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return "Artisan Story Configuration"


# =========================================
# 6. SOCIAL PROOF (UGC GALLERY) MODULE
# =========================================
class SocialProofSection(SingletonCMSModel):
    heading = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name="Section Heading"
    )

    class Meta:
        verbose_name = "Social Proof Section"
        verbose_name_plural = "Social Proof Section"

    def __str__(self):
        return "UGC Gallery Configuration"

class SocialProofImage(CMSBaseModel):
    social_proof_section = models.ForeignKey(
        SocialProofSection,
        related_name="images",
        on_delete=models.CASCADE,
        blank=True,
        null=True
    )
    image = models.ImageField(
        upload_to=_upload_to_homepage_media,
        blank=True,
        null=True,
        verbose_name="UGC Image"
    )
    position = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name="Display Order"
    )

    class Meta:
        verbose_name = "Social Proof Image"
        verbose_name_plural = "Social Proof Images"
        ordering = ["position", "id"]

    def save(self, *args, **kwargs):
        if self.image and not getattr(self.image, '_committed', True):
            try:
                optimized_result = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=500 * 1024,
                    max_width=800,
                    min_width=400,
                    filename_prefix="homepage/media/social"
                )
                self.image.save(optimized_result.filename, optimized_result.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return f"UGC Image (ID: {self.id})"