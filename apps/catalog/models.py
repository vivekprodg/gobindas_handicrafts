"""
Enterprise-grade catalog domain models for the handicraft e-commerce platform.

This module is the SINGLE SOURCE OF TRUTH for product description, taxonomy,
merchandising, SEO, and presentation metadata.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterable, List, Optional, Union

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from apps.foundation.models import CMSBaseModel, SingletonCMSModel
from apps.foundation.services import optimize_uploaded_image

DEFAULT_CATALOG_PAGE_SIZE: int = 12
MIN_RATING: int = 1
MAX_RATING: int = 5
DEFAULT_CURRENCY: str = "USD"
DEFAULT_WEIGHT_UNIT: str = "kg"
DEFAULT_DIMENSION_UNIT: str = "cm"

_CATEGORY_IMAGE_TARGET_BYTES: int = 300 * 1024
_CATEGORY_IMAGE_MAX_WIDTH: int = 800
_CATEGORY_IMAGE_MIN_WIDTH: int = 400

_PRODUCT_IMAGE_TARGET_BYTES: int = 400 * 1024
_PRODUCT_IMAGE_MAX_WIDTH: int = 800
_PRODUCT_IMAGE_MIN_WIDTH: int = 400

_COLLECTION_IMAGE_TARGET_BYTES: int = 400 * 1024
_COLLECTION_IMAGE_MAX_WIDTH: int = 1200
_COLLECTION_IMAGE_MIN_WIDTH: int = 600

_hex_color_validator = RegexValidator(
    regex=r"^#(?:[0-9a-fA-F]{3}){1,2}$",
    message=_("Color must be a valid hex code (e.g. #FFFFFF or #FFF)."),
)

def _upload_to_catalog_media(instance: Any, filename: str) -> str:
    suffix = Path(filename).suffix.lower() or ".webp"
    return f"catalog/media/{uuid.uuid4().hex}{suffix}"

class CategoryQuerySet(models.QuerySet["Category"]):
    def active(self) -> CategoryQuerySet:
        return self.filter(is_active=True)

    def root(self) -> CategoryQuerySet:
        return self.filter(parent__isnull=True)

    def children_of(self, parent: Category | None) -> CategoryQuerySet:
        if parent is None:
            return self.none()
        return self.filter(parent=parent)

    def visible_in_menu(self) -> CategoryQuerySet:
        return self.filter(is_active=True, show_in_menu=True)

    def shown_on_homepage(self) -> CategoryQuerySet:
        return self.filter(is_active=True, show_on_homepage=True)

class CategoryManager(models.Manager["Category"]):
    def get_queryset(self) -> CategoryQuerySet:
        return CategoryQuerySet(self.model, using=self._db)

    def active(self) -> CategoryQuerySet:
        return self.get_queryset().active()

    def root(self) -> CategoryQuerySet:
        return self.get_queryset().root()

    def visible_in_menu(self) -> CategoryQuerySet:
        return self.get_queryset().visible_in_menu()

    def shown_on_homepage(self) -> CategoryQuerySet:
        return self.get_queryset().shown_on_homepage()

class ArtisanQuerySet(models.QuerySet["Artisan"]):
    def active(self) -> ArtisanQuerySet:
        return self.filter(is_active=True)

class ArtisanManager(models.Manager["Artisan"]):
    def get_queryset(self) -> ArtisanQuerySet:
        return ArtisanQuerySet(self.model, using=self._db)

    def active(self) -> ArtisanQuerySet:
        return self.get_queryset().active()

class ProductQuerySet(models.QuerySet["Product"]):
    def active(self) -> ProductQuerySet:
        return self.filter(is_active=True)

    def inactive(self) -> ProductQuerySet:
        return self.filter(is_active=False)

    def drafts(self) -> ProductQuerySet:
        return self.filter(status=Product.ProductStatus.DRAFT)

    def archived(self) -> ProductQuerySet:
        return self.filter(status=Product.ProductStatus.ARCHIVED)

    def published(self) -> ProductQuerySet:
        return self.filter(status=Product.ProductStatus.PUBLISHED, is_active=True)

    def featured(self) -> ProductQuerySet:
        return self.filter(
            is_featured=True,
            is_active=True,
            status=Product.ProductStatus.PUBLISHED,
        )

    def on_sale(self) -> ProductQuerySet:
        return self.filter(
            original_price__isnull=False,
            price__isnull=False,
        ).filter(original_price__gt=F("price"))

    def by_rating(self, min_rating: Optional[Union[int, str]] = None) -> ProductQuerySet:
        if min_rating is None or min_rating == "":
            return self
        try:
            rating_val = int(min_rating)
            if MIN_RATING <= rating_val <= MAX_RATING:
                return self.filter(rating__gte=rating_val)
        except (ValueError, TypeError):
            pass
        return self

    def in_stock_only(self) -> ProductQuerySet:
        return self.filter(
            Q(inventory_records__is_active=True, inventory_records__available_quantity__gt=F("inventory_records__reserved_quantity"))
            | Q(variants__is_active=True, variants__inventory_records__is_active=True, variants__inventory_records__available_quantity__gt=F("variants__inventory_records__reserved_quantity"))
        ).distinct()

    def by_tags(self, tags: Optional[Iterable[Union[str, int]]]) -> ProductQuerySet:
        if not tags:
            return self
        tag_list = [t for t in tags if t]
        if not tag_list:
            return self
        
        if isinstance(tag_list[0], int) or (isinstance(tag_list[0], str) and tag_list[0].isdigit()):
            return self.filter(tags__id__in=tag_list).distinct()
        return self.filter(Q(tags__slug__in=tag_list) | Q(tags__name__in=tag_list)).distinct()

    def by_collections(self, collections: Optional[Iterable[Union[str, int]]]) -> ProductQuerySet:
        if not collections:
            return self
        coll_list = [c for c in collections if c]
        if not coll_list:
            return self
            
        if isinstance(coll_list[0], int) or (isinstance(coll_list[0], str) and coll_list[0].isdigit()):
            return self.filter(in_collections__id__in=coll_list).distinct()
        return self.filter(Q(in_collections__slug__in=coll_list) | Q(in_collections__name__in=coll_list)).distinct()

    def by_discount_percentage(self, min_discount_pct: Optional[Union[int, str, Decimal]] = None) -> ProductQuerySet:
        if min_discount_pct is None or min_discount_pct == "":
            return self
        try:
            min_pct = Decimal(str(min_discount_pct))
            if min_pct <= Decimal("0"):
                return self.on_sale()
            multiplier = Decimal("1.00") - (min_pct / Decimal("100.00"))
            return self.filter(
                original_price__isnull=False,
                price__isnull=False,
                original_price__gt=F("price"),
                price__lte=F("original_price") * multiplier,
            )
        except (InvalidOperation, TypeError, ValueError):
            return self

    def by_price_range(
        self, min_price: Optional[Decimal] = None, max_price: Optional[Decimal] = None
    ) -> ProductQuerySet:
        qs = self
        if min_price is not None:
            try:
                min_p = Decimal(str(min_price))
                if min_p >= Decimal("0"):
                    qs = qs.filter(price__gte=min_p)
            except (InvalidOperation, TypeError, ValueError):
                pass
        if max_price is not None:
            try:
                max_p = Decimal(str(max_price))
                if max_p >= Decimal("0"):
                    qs = qs.filter(price__lte=max_p)
            except (InvalidOperation, TypeError, ValueError):
                pass
        return qs

    def by_variant_attributes(self, attributes: Optional[Dict[str, List[str]]] = None) -> ProductQuerySet:
        if not attributes or not isinstance(attributes, dict):
            return self
        
        qs = self
        for attr_key, attr_values in attributes.items():
            if not attr_key or not attr_values:
                continue
            values = attr_values if isinstance(attr_values, (list, tuple)) else [attr_values]
            values = [str(v).strip() for v in values if v]
            if not values:
                continue
            
            filter_kwargs = {f"variants__attributes__{attr_key}__in": values, "variants__is_active": True}
            qs = qs.filter(**filter_kwargs)
            
        return qs.distinct()

    def visible(self) -> ProductQuerySet:
        now = timezone.now()
        return self.published().filter(
            Q(publish_from__isnull=True) | Q(publish_from__lte=now),
            Q(publish_until__isnull=True) | Q(publish_until__gt=now),
        )

    def in_category(self, category: Category | None) -> ProductQuerySet:
        if category is None:
            return self
        category_ids = [category.id]
        if not category.parent_id:
            sub_ids = list(category.subcategories.filter(is_active=True).values_list("id", flat=True))
            category_ids.extend(sub_ids)
        return self.filter(category_id__in=category_ids)

    def by_artisan(self, artisan: Artisan | None) -> ProductQuerySet:
        if artisan is None:
            return self
        return self.filter(artisan=artisan)

    def by_material(self, material: Material | None) -> ProductQuerySet:
        if material is None:
            return self
        return self.filter(material=material)

    def by_hue(self, hue: Hue | None) -> ProductQuerySet:
        if hue is None:
            return self
        return self.filter(hue=hue)

    def with_ethical_standards(self, *standards: EthicalStandard) -> ProductQuerySet:
        if not standards:
            return self
        return self.filter(ethical_standards__in=standards).distinct()

    def search(self, query: str) -> ProductQuerySet:
        if not query:
            return self
        query = str(query).strip()
        if not query:
            return self
        return self.filter(
            Q(title__icontains=query)
            | Q(sku__icontains=query)
            | Q(barcode__icontains=query)
            | Q(short_description__icontains=query)
            | Q(description__icontains=query)
            | Q(story__icontains=query)
            | Q(category__name__icontains=query)
            | Q(material__name__icontains=query)
            | Q(artisan__name__icontains=query)
            | Q(tags__name__icontains=query)
            | Q(in_collections__name__icontains=query)
        ).distinct()

    def ordered_by_position(self) -> ProductQuerySet:
        return self.order_by("position", "-created_at")

    def ordered_by_popularity(self) -> ProductQuerySet:
        return self.order_by("-wishlist_count", "-view_count")

    def ordered_by_recency(self) -> ProductQuerySet:
        return self.order_by("-created_at")

    def with_related(self) -> ProductQuerySet:
        return self.select_related(
            "category",
            "artisan",
            "material",
            "hue",
        ).prefetch_related(
            "highlights",
            "trust_badges",
            "labels",
            "icons",
            "ethical_standards",
            "tags",
            "in_collections",
        )

class ProductManager(models.Manager["Product"]):
    def get_queryset(self) -> ProductQuerySet:
        return ProductQuerySet(self.model, using=self._db)

    def active(self) -> ProductQuerySet:
        return self.get_queryset().active()

    def published(self) -> ProductQuerySet:
        return self.get_queryset().published()

    def visible(self) -> ProductQuerySet:
        return self.get_queryset().visible()

    def featured(self) -> ProductQuerySet:
        return self.get_queryset().featured()

    def on_sale(self) -> ProductQuerySet:
        return self.get_queryset().on_sale()

    def by_rating(self, min_rating: Optional[Union[int, str]] = None) -> ProductQuerySet:
        return self.get_queryset().by_rating(min_rating)

    def in_stock_only(self) -> ProductQuerySet:
        return self.get_queryset().in_stock_only()

    def by_tags(self, tags: Optional[Iterable[Union[str, int]]]) -> ProductQuerySet:
        return self.get_queryset().by_tags(tags)

    def by_collections(self, collections: Optional[Iterable[Union[str, int]]]) -> ProductQuerySet:
        return self.get_queryset().by_collections(collections)

    def by_discount_percentage(self, min_discount_pct: Optional[Union[int, str, Decimal]] = None) -> ProductQuerySet:
        return self.get_queryset().by_discount_percentage(min_discount_pct)

    def search(self, query: str) -> ProductQuerySet:
        return self.get_queryset().search(query)

class ProductVariantQuerySet(models.QuerySet["ProductVariant"]):
    def active(self) -> ProductVariantQuerySet:
        return self.filter(is_active=True)

    def default_variant(self) -> ProductVariantQuerySet:
        return self.filter(is_default=True, is_active=True)

    def by_sku(self, sku: str) -> ProductVariantQuerySet:
        if not sku:
            return self.none()
        return self.filter(sku__iexact=sku)

class ProductVariantManager(models.Manager["ProductVariant"]):
    def get_queryset(self) -> ProductVariantQuerySet:
        return ProductVariantQuerySet(self.model, using=self._db)

    def active(self) -> ProductVariantQuerySet:
        return self.get_queryset().active()

    def by_sku(self, sku: str) -> ProductVariantQuerySet:
        return self.get_queryset().by_sku(sku)

class CatalogSettings(SingletonCMSModel):
    default_items_per_page = models.PositiveIntegerField(
        default=DEFAULT_CATALOG_PAGE_SIZE,
        blank=True,
        null=True,
        verbose_name=_("Default Items Per Page"),
    )
    price_filter_min = models.PositiveIntegerField(
        default=5,
        blank=True,
        null=True,
        verbose_name=_("Price Filter Minimum"),
    )
    price_filter_max = models.PositiveIntegerField(
        default=5000,
        blank=True,
        null=True,
        verbose_name=_("Price Filter Maximum"),
    )
    show_stock_warning_threshold = models.PositiveIntegerField(
        default=5,
        blank=True,
        null=True,
        verbose_name=_("Stock Warning Threshold"),
    )
    default_currency = models.CharField(
        max_length=10,
        default=DEFAULT_CURRENCY,
        blank=True,
        null=True,
        verbose_name=_("Default Currency Code"),
        help_text=_("System default currency code (e.g., USD, NPR, EUR, GBP). Modifying this in CMS changes the display currency across storefront and checkout."),
    )
    default_weight_unit = models.CharField(
        max_length=10,
        default=DEFAULT_WEIGHT_UNIT,
        blank=True,
        null=True,
        verbose_name=_("Default Weight Unit"),
    )
    default_dimension_unit = models.CharField(
        max_length=10,
        default=DEFAULT_DIMENSION_UNIT,
        blank=True,
        null=True,
        verbose_name=_("Default Dimension Unit"),
    )

    class Meta:
        verbose_name = _("Catalog Settings")
        verbose_name_plural = _("Catalog Settings")
        db_table = "catalog_settings"

    def __str__(self) -> str:
        return str(_("Catalog Settings Configuration"))

class Category(CMSBaseModel):
    name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name=_("Category Name"),
    )
    slug = models.SlugField(
        max_length=150,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Slug"),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="subcategories",
        verbose_name=_("Parent Category"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Category Description"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Category Image"),
    )
    show_on_homepage = models.BooleanField(
        default=False,
        blank=True,
        null=True,
        verbose_name=_("Show on Homepage"),
    )
    show_in_menu = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Show in Menu"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        verbose_name=_("Sort Order"),
    )
    seo_title = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name=_("SEO Title"),
    )
    seo_description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("SEO Description"),
    )

    objects: ClassVar[CategoryManager] = CategoryManager()

    class Meta:
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")
        ordering = ["sort_order", "name"]
        db_table = "catalog_category"
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["parent", "is_active"]),
            models.Index(fields=["is_active", "show_in_menu"]),
            models.Index(fields=["is_active", "show_on_homepage"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_category_unique_slug_when_set",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.parent_id and self.pk and self.parent_id == self.pk:
            raise ValidationError(
                {"parent": _("A category cannot be its own parent.")}
            )
        if self.parent_id and self.parent and self.parent.parent_id:
            raise ValidationError(
                {
                    "parent": _(
                        "Nesting categories beyond 2 levels "
                        "(Category -> Subcategory) is not supported."
                    )
                }
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._optimize_category_image()
        super().save(*args, **kwargs)

    def _optimize_category_image(self) -> None:
        if self.image and not getattr(self.image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=_CATEGORY_IMAGE_TARGET_BYTES,
                    max_width=_CATEGORY_IMAGE_MAX_WIDTH,
                    min_width=_CATEGORY_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/categories",
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

    def __str__(self) -> str:
        cat_name = self.name or _("Unnamed Category")
        if self.parent_id:
            parent_name = self.parent.name if self.parent else _("Unnamed Category")
            return f"{parent_name} > {cat_name}"
        return cat_name

    @cached_property
    def is_root(self) -> bool:
        return self.parent_id is None

    @cached_property
    def is_subcategory(self) -> bool:
        return self.parent_id is not None

class Artisan(CMSBaseModel):
    name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name=_("Artisan Name"),
    )
    slug = models.SlugField(
        max_length=150,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Slug"),
    )
    bio = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Biography / Story"),
    )
    quote = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Highlight Quote"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Artisan Image"),
    )
    region = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Origin Region/Province"),
    )
    website = models.URLField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Website URL"),
    )
    email = models.EmailField(
        max_length=254,
        blank=True,
        null=True,
        verbose_name=_("Contact Email"),
    )
    phone = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        verbose_name=_("Contact Phone"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )
    position = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        verbose_name=_("Display Position"),
    )

    objects: ClassVar[ArtisanManager] = ArtisanManager()

    class Meta:
        verbose_name = _("Artisan")
        verbose_name_plural = _("Artisans")
        ordering = ["position", "name"]
        db_table = "catalog_artisan"
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_active", "position"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_artisan_unique_slug_when_set",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._optimize_artisan_image()
        super().save(*args, **kwargs)

    def _optimize_artisan_image(self) -> None:
        if self.image and not getattr(self.image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=_CATEGORY_IMAGE_TARGET_BYTES,
                    max_width=_CATEGORY_IMAGE_MAX_WIDTH,
                    min_width=_CATEGORY_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/artisans",
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

    def __str__(self) -> str:
        return self.name or _("Unnamed Artisan")

class Material(CMSBaseModel):
    name = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Material Name"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Description"),
    )

    class Meta:
        verbose_name = _("Material")
        verbose_name_plural = _("Materials")
        ordering = ["name"]
        db_table = "catalog_material"
        indexes = [
            models.Index(fields=["name"]),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Material")

class Hue(CMSBaseModel):
    name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Hue Name"),
    )
    color_code = models.CharField(
        max_length=7,
        blank=True,
        null=True,
        validators=[_hex_color_validator],
        verbose_name=_("Color Hex Code"),
    )
    swatch_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Swatch Image"),
    )

    class Meta:
        verbose_name = _("Hue Aesthetic")
        verbose_name_plural = _("Hue Aesthetics")
        ordering = ["name"]
        db_table = "catalog_hue"
        indexes = [
            models.Index(fields=["name"]),
        ]

    def __str__(self) -> str:
        return f"{self.name or _('Unnamed Hue')} ({self.color_code or _('No Code')})"

class EthicalStandard(CMSBaseModel):
    name = models.CharField(
        max_length=120,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Ethical Standard"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Description"),
    )
    icon = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Icon Class"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Ethical Standard")
        verbose_name_plural = _("Ethical Standards")
        ordering = ["name"]
        db_table = "catalog_ethical_standard"
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Standard")

class Collection(CMSBaseModel):
    name = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        verbose_name=_("Collection Name"),
    )
    slug = models.SlugField(
        max_length=150,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Slug"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Description"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Image"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Collection")
        verbose_name_plural = _("Collections")
        ordering = ["name"]
        db_table = "catalog_collection"
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_collection_unique_slug_when_set",
            ),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Collection")

class Tag(CMSBaseModel):
    name = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Tag Name"),
    )
    slug = models.SlugField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Slug"),
    )

    class Meta:
        verbose_name = _("Tag")
        verbose_name_plural = _("Tags")
        ordering = ["name"]
        db_table = "catalog_tag"
        indexes = [
            models.Index(fields=["slug"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_tag_unique_slug_when_set",
            ),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Tag")

class VariantType(CMSBaseModel):
    name = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name=_("Variant Type"),
    )
    display_order = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Variant Type")
        verbose_name_plural = _("Variant Types")
        ordering = ["display_order", "name"]
        db_table = "catalog_variant_type"
        indexes = [
            models.Index(fields=["is_active", "display_order"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["name"],
                condition=Q(name__isnull=False),
                name="catalog_variant_type_unique_name_when_set",
            ),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Variant Type")

class VariantOption(CMSBaseModel):
    variant_type = models.ForeignKey(
        VariantType,
        on_delete=models.CASCADE,
        related_name="options",
        verbose_name=_("Variant Type"),
    )
    value = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name=_("Option Value"),
    )
    color_code = models.CharField(
        max_length=7,
        blank=True,
        null=True,
        validators=[_hex_color_validator],
        verbose_name=_("Color Hex Code"),
    )
    swatch_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Swatch Image"),
    )
    display_order = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Variant Option")
        verbose_name_plural = _("Variant Options")
        ordering = ["variant_type__name", "display_order", "value"]
        db_table = "catalog_variant_option"
        indexes = [
            models.Index(fields=["variant_type", "is_active"]),
        ]

    def __str__(self) -> str:
        type_name = (
            self.variant_type.name if self.variant_type else _("Unknown Type")
        )
        return f"{type_name}: {self.value or _('Unnamed Option')}"

class ProductHighlight(CMSBaseModel):
    name = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Highlight Name"),
    )
    icon_class = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Icon Class"),
    )
    display_order = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product Highlight")
        verbose_name_plural = _("Product Highlights")
        ordering = ["display_order", "name"]
        db_table = "catalog_product_highlight"
        indexes = [
            models.Index(fields=["is_active", "display_order"]),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Highlight")

class TrustBadge(CMSBaseModel):
    name = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Badge Name"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Badge Image"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Description"),
    )
    display_order = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Trust Badge")
        verbose_name_plural = _("Trust Badges")
        ordering = ["display_order", "name"]
        db_table = "catalog_trust_badge"
        indexes = [
            models.Index(fields=["is_active", "display_order"]),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Trust Badge")

class ProductLabel(CMSBaseModel):
    name = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Label Name"),
    )
    slug = models.SlugField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Slug"),
    )
    text_color = models.CharField(
        max_length=7,
        default="#FFFFFF",
        blank=True,
        null=True,
        validators=[_hex_color_validator],
        verbose_name=_("Text Color"),
    )
    bg_color = models.CharField(
        max_length=7,
        default="#2C2520",
        blank=True,
        null=True,
        validators=[_hex_color_validator],
        verbose_name=_("Background Color"),
    )
    display_order = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product Label")
        verbose_name_plural = _("Product Labels")
        ordering = ["display_order", "name"]
        db_table = "catalog_product_label"
        indexes = [
            models.Index(fields=["is_active", "display_order"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_product_label_unique_slug_when_set",
            ),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Label")

class ProductIcon(CMSBaseModel):
    name = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Icon Name"),
    )
    icon_class = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Icon Class"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Icon Image"),
    )
    display_order = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product Icon")
        verbose_name_plural = _("Product Icons")
        ordering = ["display_order", "name"]
        db_table = "catalog_product_icon"
        indexes = [
            models.Index(fields=["is_active", "display_order"]),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Icon")

class Product(CMSBaseModel):
    class ProductStatus(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")
        ARCHIVED = "archived", _("Archived")

    class ProductType(models.TextChoices):
        PHYSICAL = "physical", _("Physical Product")
        DIGITAL = "digital", _("Digital Product")
        SERVICE = "service", _("Service")
        BUNDLE = "bundle", _("Bundle")
        GIFT_CARD = "gift_card", _("Gift Card")
        SUBSCRIPTION = "subscription", _("Subscription")

    title = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Product Title"),
    )
    slug = models.SlugField(
        max_length=255,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_("Slug"),
    )
    sku = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("SKU"),
    )
    barcode = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Barcode"),
    )
    product_type = models.CharField(
        max_length=24,
        choices=ProductType.choices,
        default=ProductType.PHYSICAL,
        blank=True,
        null=True,
        verbose_name=_("Product Type"),
    )
    model_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Model Number"),
    )
    internal_reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Internal Reference"),
    )

    short_description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Short Description"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Full Description"),
    )

    story = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Artisan / Product Story"),
    )
    crafting_process = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Crafting Process"),
    )
    care_instructions = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Care Instructions"),
    )
    shipping_information = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Shipping Information"),
    )
    delivery_promise = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Delivery Promise"),
    )
    return_policy = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Return Policy"),
    )
    warranty_information = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Warranty Information"),
    )
    country_of_origin = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Country of Origin"),
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("Current Price"),
    )
    original_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("Original Price"),
    )
    cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True,
        verbose_name=_("Cost Price"),
    )
    currency = models.CharField(
        max_length=10,
        default=DEFAULT_CURRENCY,
        blank=True,
        null=True,
        verbose_name=_("Currency Code"),
    )
    tax_class = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Tax Class"),
    )

    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="products",
        verbose_name=_("Category"),
    )
    artisan = models.ForeignKey(
        Artisan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name=_("Master Craftsman"),
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name=_("Material"),
    )
    hue = models.ForeignKey(
        Hue,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
        verbose_name=_("Hue Aesthetic"),
    )
    ethical_standards = models.ManyToManyField(
        EthicalStandard,
        blank=True,
        related_name="products",
        verbose_name=_("Ethical Standards"),
    )

    highlights = models.ManyToManyField(
        ProductHighlight,
        blank=True,
        related_name="products",
        verbose_name=_("Product Highlights"),
    )
    trust_badges = models.ManyToManyField(
        TrustBadge,
        blank=True,
        related_name="products",
        verbose_name=_("Trust Badges"),
    )
    labels = models.ManyToManyField(
        ProductLabel,
        blank=True,
        related_name="products",
        verbose_name=_("Product Labels"),
    )
    icons = models.ManyToManyField(
        ProductIcon,
        blank=True,
        related_name="products",
        verbose_name=_("Product Icons"),
    )

    related_products = models.ManyToManyField(
        "self",
        blank=True,
        symmetrical=False,
        related_name="related_to",
        verbose_name=_("Related Products"),
    )
    upsell_products = models.ManyToManyField(
        "self",
        blank=True,
        symmetrical=False,
        related_name="upsold_by",
        verbose_name=_("Upsell Products"),
    )
    cross_sell_products = models.ManyToManyField(
        "self",
        blank=True,
        symmetrical=False,
        related_name="cross_sold_by",
        verbose_name=_("Cross Sell Products"),
    )

    primary_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Primary Image"),
    )
    hover_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Hover Image"),
    )
    video_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name=_("Primary Video URL"),
    )

    length = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Length"),
    )
    width = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Width"),
    )
    height = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Height"),
    )
    weight = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Weight"),
    )

    badge_text = models.CharField(
        max_length=60,
        blank=True,
        null=True,
        verbose_name=_("Primary Badge Text"),
    )
    secondary_badge_text = models.CharField(
        max_length=60,
        blank=True,
        null=True,
        verbose_name=_("Secondary Badge Text"),
    )
    ribbon_text = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name=_("Ribbon Text"),
    )
    ribbon_bg_color = models.CharField(
        max_length=7,
        default="#C5A880",
        blank=True,
        null=True,
        validators=[_hex_color_validator],
        verbose_name=_("Ribbon Background Color"),
    )
    ribbon_text_color = models.CharField(
        max_length=7,
        default="#FFFFFF",
        blank=True,
        null=True,
        validators=[_hex_color_validator],
        verbose_name=_("Ribbon Text Color"),
    )
    rating = models.PositiveIntegerField(
        default=5,
        choices=[(i, str(i)) for i in range(MIN_RATING, MAX_RATING + 1)],
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Rating (1-5)"),
    )
    reviews_count = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        verbose_name=_("Reviews Count"),
    )

    view_count = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("View Count"),
    )
    wishlist_count = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Wishlist Count"),
    )

    status = models.CharField(
        max_length=20,
        choices=ProductStatus.choices,
        default=ProductStatus.DRAFT,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Product Status"),
    )
    is_featured = models.BooleanField(
        default=False,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Is Featured"),
    )
    is_active = models.BooleanField(
        default=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Is Active"),
    )
    position = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        verbose_name=_("Display Position"),
    )
    published_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Published At"),
    )
    publish_from = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Publish From"),
    )
    publish_until = models.DateTimeField(
        null=True, blank=True, verbose_name=_("Publish Until"),
    )

    seo_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("SEO Title"),
    )
    seo_description = models.TextField(
        blank=True, null=True, verbose_name=_("SEO Description"),
    )
    seo_keywords = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("SEO Keywords"),
    )
    meta_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Meta Title"),
    )
    meta_description = models.TextField(
        blank=True, null=True, verbose_name=_("Meta Description"),
    )
    meta_keywords = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Meta Keywords"),
    )
    canonical_url = models.URLField(
        max_length=500, blank=True, null=True, verbose_name=_("Canonical URL"),
    )
    robots_directives = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Robots Directives"),
    )
    og_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Open Graph Title"),
    )
    og_description = models.TextField(
        blank=True, null=True, verbose_name=_("Open Graph Description"),
    )
    og_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True, null=True, verbose_name=_("Open Graph Image"),
    )
    twitter_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Twitter Title"),
    )
    twitter_description = models.TextField(
        blank=True, null=True, verbose_name=_("Twitter Description"),
    )
    twitter_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True, null=True, verbose_name=_("Twitter Image"),
    )
    structured_data = models.JSONField(
        default=dict, blank=True, null=True,
        verbose_name=_("Schema.org Structured Data"),
    )

    cart = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="catalog_product_line_items",
        verbose_name=_("Order Line Item (Audit Reference)"),
    )

    objects: ClassVar[ProductManager] = ProductManager()

    class Meta:
        verbose_name = _("Product Masterpiece")
        verbose_name_plural = _("Product Masterpieces")
        ordering = ["position", "-created_at"]
        db_table = "catalog_product"
        indexes = [
            models.Index(fields=["status", "is_active"]),
            models.Index(fields=["status", "is_active", "price"]),
            models.Index(fields=["status", "is_active", "rating"]),
            models.Index(fields=["status", "is_active", "is_featured"]),
            models.Index(fields=["sku"]),
            models.Index(fields=["barcode"]),
            models.Index(fields=["-wishlist_count"]),
            models.Index(fields=["-view_count"]),
            models.Index(fields=["slug"]),
            models.Index(fields=["category", "status"]),
            models.Index(fields=["artisan", "status"]),
            models.Index(fields=["position"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sku"],
                condition=Q(sku__isnull=False),
                name="catalog_product_unique_sku_when_set",
            ),
            models.UniqueConstraint(
                fields=["barcode"],
                condition=Q(barcode__isnull=False),
                name="catalog_product_unique_barcode_when_set",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_product_unique_slug_when_set",
            ),
            models.UniqueConstraint(
                fields=["internal_reference"],
                condition=Q(internal_reference__isnull=False),
                name="catalog_product_unique_internal_ref_when_set",
            ),
            models.CheckConstraint(
                check=(
                    Q(original_price__isnull=True)
                    | Q(price__isnull=True)
                    | Q(original_price__gt=F("price"))
                ),
                name="catalog_product_original_price_gt_price",
            ),
            models.CheckConstraint(
                check=(
                    Q(rating__isnull=True)
                    | Q(rating__gte=MIN_RATING, rating__lte=MAX_RATING)
                ),
                name="catalog_product_rating_in_range",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if (
            self.original_price is not None
            and self.price is not None
            and self.original_price <= self.price
        ):
            raise ValidationError(
                {"original_price": _(
                    "Original price must be greater than current price "
                    "to represent a discount."
                )}
            )
        if self.publish_from and self.publish_until:
            if self.publish_from >= self.publish_until:
                raise ValidationError(
                    {"publish_until": _("Publish-until must be after publish-from.")}
                )

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._optimize_primary_image()
        self._optimize_hover_image()
        self._optimize_og_image()
        self._optimize_twitter_image()
        super().save(*args, **kwargs)

    def _optimize_primary_image(self) -> None:
        if self.primary_image and not getattr(self.primary_image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.primary_image.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/primary",
                )
                self.primary_image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

    def _optimize_hover_image(self) -> None:
        if self.hover_image and not getattr(self.hover_image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.hover_image.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/hover",
                )
                self.hover_image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

    def _optimize_og_image(self) -> None:
        if self.og_image and not getattr(self.og_image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.og_image.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/og",
                )
                self.og_image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

    def _optimize_twitter_image(self) -> None:
        if self.twitter_image and not getattr(self.twitter_image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.twitter_image.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/twitter",
                )
                self.twitter_image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass

    def __str__(self) -> str:
        return self.title or _("Unnamed Product")

    @cached_property
    def display_title(self) -> str:
        return self.title or _("Unnamed Product")

    @cached_property
    def is_on_sale(self) -> bool:
        return bool(
            self.original_price is not None
            and self.price is not None
            and self.original_price > self.price
        )

    @cached_property
    def discount_amount(self) -> Decimal | None:
        if not self.is_on_sale:
            return None
        return (self.original_price - self.price).quantize(Decimal("0.01"))

    @cached_property
    def discount_percentage(self) -> Decimal | None:
        if not self.is_on_sale or not self.original_price:
            return None
        try:
            pct = (
                (self.original_price - self.price) / self.original_price
            ) * Decimal("100")
            return pct.quantize(Decimal("0.01"))
        except (ArithmeticError, InvalidOperation):
            return None

    @cached_property
    def has_discount(self) -> bool:
        return self.is_on_sale

    @cached_property
    def is_published(self) -> bool:
        return self.status == self.ProductStatus.PUBLISHED

    @cached_property
    def is_draft(self) -> bool:
        return self.status == self.ProductStatus.DRAFT

    @cached_property
    def is_archived(self) -> bool:
        return self.status == self.ProductStatus.ARCHIVED

    @cached_property
    def is_in_publishing_window(self) -> bool:
        if not self.is_published or not self.is_active:
            return False
        now = timezone.now()
        if self.publish_from and self.publish_from > now:
            return False
        if self.publish_until and self.publish_until <= now:
            return False
        return True

    @cached_property
    def is_visible(self) -> bool:
        return self.is_in_publishing_window

    @cached_property
    def default_image(self):
        if self.primary_image:
            return self.primary_image
        first_gallery = self.gallery_images.order_by("position", "id").first()
        if first_gallery and first_gallery.image:
            return first_gallery.image
        if self.hover_image:
            return self.hover_image
        return None

    @cached_property
    def effective_seo_title(self) -> str:
        return self.seo_title or self.meta_title or self.title or ""

    @cached_property
    def effective_seo_description(self) -> str:
        return (
            self.seo_description
            or self.meta_description
            or self.short_description
            or ""
        )

    @cached_property
    def favorite_count(self) -> int:
        return int(self.wishlist_count or 0)

    @cached_property
    def wishlist_total(self) -> int:
        return self.favorite_count

    def increment_view_count(self, commit: bool = True) -> None:
        self.view_count = F("view_count") + 1
        if commit:
            self.save(update_fields=["view_count", "updated_at"])
            self.refresh_from_db(fields=["view_count"])

    def increment_wishlist_count(self, commit: bool = True) -> None:
        self.wishlist_count = F("wishlist_count") + 1
        if commit:
            self.save(update_fields=["wishlist_count", "updated_at"])
            self.refresh_from_db(fields=["wishlist_count"])

    def decrement_wishlist_count(self, commit: bool = True) -> None:
        if self.wishlist_count and self.wishlist_count > 0:
            self.wishlist_count = F("wishlist_count") - 1
            if commit:
                self.save(update_fields=["wishlist_count", "updated_at"])
                self.refresh_from_db(fields=["wishlist_count"])

    def get_recommended_products(self, limit: int = 4) -> models.QuerySet["Product"]:
        related = self.related_products.filter(
            is_active=True, status=self.ProductStatus.PUBLISHED
        )
        if related.exists():
            return related[:limit]
        if self.category_id:
            return (
                Product.objects.filter(
                    category=self.category,
                    is_active=True,
                    status=self.ProductStatus.PUBLISHED,
                )
                .exclude(id=self.id)
                .order_by("-wishlist_count", "-view_count")[:limit]
            )
        return Product.objects.none()

    def get_upsell_products(self, limit: int = 4) -> models.QuerySet["Product"]:
        return self.upsell_products.filter(
            is_active=True, status=self.ProductStatus.PUBLISHED
        )[:limit]

    def get_cross_sell_products(self, limit: int = 4) -> models.QuerySet["Product"]:
        return self.cross_sell_products.filter(
            is_active=True, status=self.ProductStatus.PUBLISHED
        )[:limit]

    def get_default_variant(self) -> ProductVariant | None:
        return self.variants.filter(is_default=True, is_active=True).first()

    def get_active_variants(self) -> models.QuerySet["ProductVariant"]:
        return self.variants.filter(is_active=True).order_by("sort_order", "id")

    @classmethod
    def get_trending_products(cls, limit: int = 10) -> models.QuerySet["Product"]:
        return cls.objects.filter(
            status=cls.ProductStatus.PUBLISHED,
            is_active=True,
        ).order_by("-wishlist_count", "-view_count")[:limit]

    @classmethod
    def get_popular_products(cls, limit: int = 10) -> models.QuerySet["Product"]:
        return cls.objects.filter(
            status=cls.ProductStatus.PUBLISHED,
            is_active=True,
        ).order_by("-view_count", "-reviews_count")[:limit]

    @classmethod
    def get_new_arrivals(cls, limit: int = 10) -> models.QuerySet["Product"]:
        return cls.objects.filter(
            status=cls.ProductStatus.PUBLISHED,
            is_active=True,
        ).order_by("-published_at", "-created_at")[:limit]

class ProductVariant(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="variants",
        verbose_name=_("Product"),
    )
    name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Variant Name"),
    )
    sku = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("SKU"),
    )
    barcode = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        db_index=True,
        verbose_name=_("Barcode"),
    )

    price_override = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Price Override"),
    )
    compare_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Compare At Price"),
    )

    weight = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Weight Override"),
    )
    length = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Length Override"),
    )
    width = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Width Override"),
    )
    height = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Height Override"),
    )

    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Variant Image"),
    )

    attributes = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Attributes"),
    )
    color_code = models.CharField(
        max_length=7,
        blank=True,
        null=True,
        validators=[_hex_color_validator],
        verbose_name=_("Color Hex Code"),
    )

    is_active = models.BooleanField(
        default=True, blank=True, null=True, db_index=True,
        verbose_name=_("Is Active"),
    )
    is_default = models.BooleanField(
        default=False, blank=True, null=True, db_index=True,
        verbose_name=_("Is Default Variant"),
    )
    sort_order = models.PositiveIntegerField(
        default=0, blank=True, null=True, db_index=True,
        verbose_name=_("Sort Order"),
    )

    cart = models.ForeignKey(
        "orders.OrderItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="catalog_variant_line_items",
        verbose_name=_("Order Line Item (Audit Reference)"),
    )

    objects: ClassVar[ProductVariantManager] = ProductVariantManager()

    class Meta:
        verbose_name = _("Product Variant")
        verbose_name_plural = _("Product Variants")
        ordering = ["sort_order", "id"]
        db_table = "catalog_product_variant"
        indexes = [
            models.Index(fields=["product", "is_active"]),
            models.Index(fields=["product", "is_default"]),
            models.Index(fields=["product", "sort_order"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["sku"],
                condition=Q(sku__isnull=False),
                name="catalog_variant_unique_sku_when_set",
            ),
            models.UniqueConstraint(
                fields=["barcode"],
                condition=Q(barcode__isnull=False),
                name="catalog_variant_unique_barcode_when_set",
            ),
            models.CheckConstraint(
                check=(
                    Q(price_override__isnull=True)
                    | Q(price_override__gte=Decimal("0"))
                ),
                name="catalog_variant_price_override_gte_0",
            ),
            models.CheckConstraint(
                check=(
                    Q(compare_price__isnull=True)
                    | Q(compare_price__gte=Decimal("0"))
                ),
                name="catalog_variant_compare_price_gte_0",
            ),
            models.CheckConstraint(
                check=(
                    Q(price_override__isnull=True)
                    | Q(compare_price__isnull=True)
                    | Q(compare_price__gte=F("price_override"))
                ),
                name="catalog_variant_compare_gte_override",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if (
            self.price_override is not None
            and self.compare_price is not None
            and self.compare_price < self.price_override
        ):
            raise ValidationError(
                {"compare_price": _(
                    "Compare-at price must be greater than or equal to "
                    "the price override."
                )}
            )

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.image and not getattr(self.image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/variants",
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        product_title = (
            self.product.title if self.product and self.product.title
            else _("Unknown Product")
        )
        variant_label = self.name or self.sku or f"Variant #{self.pk}"
        return f"{product_title} - {variant_label}"

    @cached_property
    def effective_price(self) -> Decimal | None:
        if self.price_override is not None:
            return self.price_override
        if self.product and self.product.price is not None:
            return self.product.price
        return None

    @cached_property
    def is_on_sale(self) -> bool:
        if self.compare_price is None or self.effective_price is None:
            return False
        return self.compare_price > self.effective_price

class ProductSpecification(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="specifications",
        verbose_name=_("Product"),
    )
    label = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Specification Label"),
    )
    value = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Specification Value"),
    )
    group = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Group"),
    )
    display_order = models.PositiveIntegerField(
        default=0, blank=True, null=True, db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product Specification")
        verbose_name_plural = _("Product Specifications")
        ordering = ["display_order", "group", "label"]
        db_table = "catalog_product_specification"
        indexes = [
            models.Index(fields=["product", "display_order"]),
            models.Index(fields=["product", "group"]),
        ]

    def __str__(self) -> str:
        return f"{self.label or _('Unnamed')}: {self.value or ''}"

class ProductFAQ(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="faqs",
        verbose_name=_("Product"),
    )
    question = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Question"),
    )
    answer = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Answer"),
    )
    display_order = models.PositiveIntegerField(
        default=0, blank=True, null=True, db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True, db_index=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product FAQ")
        verbose_name_plural = _("Product FAQs")
        ordering = ["display_order", "id"]
        db_table = "catalog_product_faq"
        indexes = [
            models.Index(fields=["product", "is_active"]),
        ]

    def __str__(self) -> str:
        return self.question or _("Unanswered FAQ")

class ProductVideo(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="videos",
        verbose_name=_("Product"),
    )
    title = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name=_("Video Title"),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Description"),
    )
    video_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name=_("Video URL"),
    )
    thumbnail = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Video Thumbnail"),
    )
    duration_seconds = models.PositiveIntegerField(
        blank=True,
        null=True,
        verbose_name=_("Duration (seconds)"),
    )
    display_order = models.PositiveIntegerField(
        default=0, blank=True, null=True, db_index=True,
        verbose_name=_("Display Order"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product Video")
        verbose_name_plural = _("Product Videos")
        ordering = ["display_order", "id"]
        db_table = "catalog_product_video"
        indexes = [
            models.Index(fields=["product", "is_active"]),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.thumbnail and not getattr(self.thumbnail, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.thumbnail.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/videos",
                )
                self.thumbnail.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.title or self.video_url or _("Untitled Video")

class ProductImage(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="gallery_images",
        verbose_name=_("Product"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Gallery Image"),
    )
    title = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name=_("Image Title"),
    )
    alt_text = models.CharField(
        max_length=150,
        blank=True,
        null=True,
        verbose_name=_("Alt Text"),
    )
    caption = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Caption"),
    )
    is_primary = models.BooleanField(
        default=False,
        blank=True,
        null=True,
        verbose_name=_("Is Primary Display Image"),
    )
    position = models.PositiveIntegerField(
        default=0,
        blank=True,
        null=True,
        verbose_name=_("Display Position"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product Gallery Image")
        verbose_name_plural = _("Product Gallery Images")
        ordering = ["position", "id"]
        db_table = "catalog_product_image"
        indexes = [
            models.Index(fields=["product", "position"]),
            models.Index(fields=["product", "is_primary"]),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.image and not getattr(self.image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/gallery",
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        product_title = (
            self.product.title
            if (self.product and self.product.title)
            else _("Unknown Product")
        )
        return f"Gallery Image for {product_title}"

class ProductGalleryImage(CMSBaseModel):
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="additional_galleries",
        verbose_name=_("Product"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True,
        null=True,
        verbose_name=_("Gallery Image"),
    )
    alt_text = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Alt Text"),
    )
    caption = models.TextField(
        blank=True, null=True, verbose_name=_("Caption"),
    )
    media_type = models.CharField(
        max_length=20,
        default="image",
        blank=True,
        null=True,
        verbose_name=_("Media Type"),
    )
    sort_order = models.PositiveIntegerField(
        default=0, blank=True, null=True, db_index=True,
        verbose_name=_("Sort Order"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Additional Product Gallery")
        verbose_name_plural = _("Additional Product Galleries")
        ordering = ["sort_order", "id"]
        db_table = "catalog_product_gallery_image"
        indexes = [
            models.Index(fields=["product", "sort_order"]),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.image and not getattr(self.image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=_PRODUCT_IMAGE_TARGET_BYTES,
                    max_width=_PRODUCT_IMAGE_MAX_WIDTH,
                    min_width=_PRODUCT_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/products/additional_gallery",
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        product_title = (
            self.product.title if (self.product and self.product.title)
            else _("Unknown Product")
        )
        return f"Gallery Image {self.pk} for {product_title}"

class ProductTag(CMSBaseModel):
    name = models.CharField(
        max_length=50, unique=True,
        blank=True, null=True,
        verbose_name=_("Tag Name"),
    )
    slug = models.SlugField(
        max_length=50, unique=True,
        blank=True, null=True,
        verbose_name=_("Slug"),
    )
    description = models.TextField(
        blank=True, null=True, verbose_name=_("Description"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True, db_index=True,
        verbose_name=_("Is Active"),
    )
    products = models.ManyToManyField(
        Product,
        related_name="tags",
        blank=True,
        verbose_name=_("Tagged Products"),
    )

    class Meta:
        verbose_name = _("Product Tag")
        verbose_name_plural = _("Product Tags")
        ordering = ["name"]
        db_table = "catalog_product_tag"
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_product_tag_unique_slug_when_set",
            ),
        ]

    def __str__(self) -> str:
        return self.name or _("Unnamed Tag")

class ProductCollection(CMSBaseModel):
    name = models.CharField(
        max_length=120, blank=True, null=True, verbose_name=_("Collection Name"),
    )
    slug = models.SlugField(
        max_length=150, unique=True,
        blank=True, null=True,
        verbose_name=_("Slug"),
    )
    description = models.TextField(
        blank=True, null=True, verbose_name=_("Description"),
    )
    image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True, null=True, verbose_name=_("Collection Image"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True, db_index=True,
        verbose_name=_("Is Active"),
    )
    is_featured = models.BooleanField(
        default=False, blank=True, null=True,
        verbose_name=_("Is Featured"),
    )
    sort_order = models.PositiveIntegerField(
        default=0, blank=True, null=True, db_index=True,
        verbose_name=_("Sort Order"),
    )
    products = models.ManyToManyField(
        Product,
        related_name="in_collections",
        blank=True,
        verbose_name=_("Collection Products"),
    )

    class Meta:
        verbose_name = _("Product Collection")
        verbose_name_plural = _("Product Collections")
        ordering = ["sort_order", "name"]
        db_table = "catalog_product_collection"
        indexes = [
            models.Index(fields=["slug"]),
            models.Index(fields=["is_active", "sort_order"]),
            models.Index(fields=["is_featured"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(slug__isnull=False),
                name="catalog_product_collection_unique_slug_when_set",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.image and not getattr(self.image, "_committed", True):
            try:
                optimized = optimize_uploaded_image(
                    self.image.file,
                    target_max_bytes=_COLLECTION_IMAGE_TARGET_BYTES,
                    max_width=_COLLECTION_IMAGE_MAX_WIDTH,
                    min_width=_COLLECTION_IMAGE_MIN_WIDTH,
                    filename_prefix="catalog/collections",
                )
                self.image.save(optimized.filename, optimized.file, save=False)
            except Exception:
                pass
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name or _("Unnamed Collection")

class ProductSEO(CMSBaseModel):
    product = models.OneToOneField(
        Product,
        on_delete=models.CASCADE,
        related_name="seo_config",
        verbose_name=_("Product"),
    )
    meta_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Meta Title"),
    )
    meta_description = models.TextField(
        blank=True, null=True, verbose_name=_("Meta Description"),
    )
    keywords = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Keywords"),
    )
    canonical_url = models.URLField(
        max_length=500, blank=True, null=True, verbose_name=_("Canonical URL"),
    )
    robots = models.CharField(
        max_length=255, blank=True, null=True, verbose_name=_("Robots Directives"),
    )
    og_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Open Graph Title"),
    )
    og_description = models.TextField(
        blank=True, null=True, verbose_name=_("Open Graph Description"),
    )
    og_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True, null=True, verbose_name=_("Open Graph Image"),
    )
    twitter_title = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Twitter Title"),
    )
    twitter_description = models.TextField(
        blank=True, null=True, verbose_name=_("Twitter Description"),
    )
    twitter_image = models.ImageField(
        upload_to=_upload_to_catalog_media,
        blank=True, null=True, verbose_name=_("Twitter Image"),
    )

    class Meta:
        verbose_name = _("Product SEO Profile")
        verbose_name_plural = _("Product SEO Profiles")
        db_table = "catalog_product_seo"

    def __str__(self) -> str:
        product_title = (
            self.product.title if self.product and self.product.title
            else _("Unknown Product")
        )
        return f"SEO for {product_title}"

class ProductSchema(CMSBaseModel):
    product = models.OneToOneField(
        Product,
        on_delete=models.CASCADE,
        related_name="schema_config",
        verbose_name=_("Product"),
    )
    schema_type = models.CharField(
        max_length=100,
        default="Product",
        blank=True,
        null=True,
        verbose_name=_("Schema Type"),
    )
    schema_data = models.JSONField(
        default=dict, blank=True, null=True,
        verbose_name=_("Schema JSON Data"),
    )
    is_active = models.BooleanField(
        default=True, blank=True, null=True,
        verbose_name=_("Is Active"),
    )

    class Meta:
        verbose_name = _("Product Schema")
        verbose_name_plural = _("Product Schemas")
        db_table = "catalog_product_schema"

    def __str__(self) -> str:
        product_title = (
            self.product.title if self.product and self.product.title
            else _("Unknown Product")
        )
        return f"Schema for {product_title}"

class RecentlyViewedProduct(CMSBaseModel):
    user_id = models.IntegerField(
        null=True, blank=True, db_index=True, verbose_name=_("User ID"),
    )
    session_key = models.CharField(
        max_length=64, db_index=True, blank=True, null=True,
        verbose_name=_("Session Key"),
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="recently_viewed",
        verbose_name=_("Product"),
    )
    viewed_at = models.DateTimeField(
        auto_now=True, db_index=True, verbose_name=_("Viewed At"),
    )

    class Meta:
        verbose_name = _("Recently Viewed Product")
        verbose_name_plural = _("Recently Viewed Products")
        ordering = ["-viewed_at"]
        db_table = "catalog_recently_viewed_product"
        indexes = [
            models.Index(fields=["session_key", "-viewed_at"]),
            models.Index(fields=["user_id", "-viewed_at"]),
            models.Index(fields=["product", "-viewed_at"]),
        ]

    def __str__(self) -> str:
        return f"Viewed Product {self.product_id} at {self.viewed_at}"

__all__ = [
    "CategoryQuerySet",
    "CategoryManager",
    "ArtisanQuerySet",
    "ArtisanManager",
    "ProductQuerySet",
    "ProductManager",
    "ProductVariantQuerySet",
    "ProductVariantManager",
    "CatalogSettings",
    "Category",
    "Artisan",
    "Material",
    "Hue",
    "EthicalStandard",
    "Collection",
    "Tag",
    "ProductTag",
    "ProductCollection",
    "VariantType",
    "VariantOption",
    "ProductHighlight",
    "TrustBadge",
    "ProductLabel",
    "ProductIcon",
    "Product",
    "ProductVariant",
    "ProductImage",
    "ProductGalleryImage",
    "ProductSpecification",
    "ProductFAQ",
    "ProductVideo",
    "ProductSEO",
    "ProductSchema",
    "RecentlyViewedProduct",
    "_upload_to_catalog_media",
]