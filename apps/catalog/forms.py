"""
Enterprise-grade Forms for the Catalog application.
Provides comprehensive form classes for Catalog CMS management and multi-faceted product discovery filtering.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .models import (
    Artisan,
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
    ProductVariant,
    ProductVideo,
    Tag,
    TrustBadge,
    VariantOption,
    VariantType,
)

logger = logging.getLogger(__name__)

def _safe_get(d: Optional[Dict[str, Any]], key: str, default: Any = None) -> Any:
    if d is None:
        return default
    value = d.get(key, default)
    return value if value is not None else default

def _normalize_decimal(
    value: Any,
    *,
    allow_none: bool = True,
    min_value: Optional[Decimal] = None,
) -> Optional[Decimal]:
    if value is None or value == "":
        return None if allow_none else Decimal("0")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None if allow_none else Decimal("0")
    if decimal_value.is_nan() or decimal_value.is_infinite():
        return None if allow_none else Decimal("0")
    if min_value is not None and decimal_value < min_value:
        return min_value
    return decimal_value

def _normalize_integer(
    value: Any,
    *,
    allow_none: bool = True,
    min_value: Optional[int] = None,
) -> Optional[int]:
    if value is None or value == "":
        return None if allow_none else 0
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        try:
            int_value = int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return None if allow_none else 0
    if min_value is not None and int_value < min_value:
        return min_value
    return int_value

def _normalize_text(value: Any, *, max_length: Optional[int] = None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if max_length is not None and len(text) > max_length:
        text = text[:max_length]
    return text

def _parse_json_field(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = [
            "name",
            "slug",
            "parent",
            "description",
            "image",
            "show_on_homepage",
            "show_in_menu",
            "is_active",
            "sort_order",
            "seo_title",
            "seo_description",
        ]

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        parent = _safe_get(cleaned_data, "parent")

        if parent:
            if self.instance and self.instance.pk and self.instance.pk == parent.pk:
                self.add_error(
                    "parent",
                    _("A category cannot be its own parent."),
                )
            try:
                if parent.parent_id:
                    self.add_error(
                        "parent",
                        _(
                            "Nesting categories beyond 2 levels "
                            "(Category -> Subcategory) is not supported."
                        ),
                    )
            except Exception:
                pass

        return cleaned_data

class ArtisanForm(forms.ModelForm):
    class Meta:
        model = Artisan
        fields = [
            "name",
            "slug",
            "bio",
            "quote",
            "image",
            "region",
            "website",
            "email",
            "phone",
            "is_active",
            "position",
        ]

    def clean_slug(self) -> str:
        slug = _normalize_text(self.cleaned_data.get("slug"))
        if not slug:
            return slug
        qs = Artisan.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("An artisan with this slug already exists."),
                code="duplicate_slug",
            )
        return slug

class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = ["name", "description"]

    def clean_name(self) -> str:
        name = _normalize_text(self.cleaned_data.get("name"))
        if not name:
            return name
        qs = Material.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A material with this name already exists."),
                code="duplicate_name",
            )
        return name

class HueForm(forms.ModelForm):
    class Meta:
        model = Hue
        fields = ["name", "color_code", "swatch_image"]

    def clean_color_code(self) -> str:
        value = _normalize_text(self.cleaned_data.get("color_code"))
        if not value:
            return value
        return value.upper()

class EthicalStandardForm(forms.ModelForm):
    class Meta:
        model = EthicalStandard
        fields = ["name", "description", "icon", "is_active"]

    def clean_name(self) -> str:
        name = _normalize_text(self.cleaned_data.get("name"))
        if not name:
            return name
        qs = EthicalStandard.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("An ethical standard with this name already exists."),
                code="duplicate_name",
            )
        return name

class CollectionForm(forms.ModelForm):
    class Meta:
        model = Collection
        fields = ["name", "slug", "description", "image", "is_active"]

    def clean_slug(self) -> str:
        slug = _normalize_text(self.cleaned_data.get("slug"))
        if not slug:
            return slug
        qs = Collection.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A collection with this slug already exists."),
                code="duplicate_slug",
            )
        return slug

class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ["name", "slug"]

    def clean_slug(self) -> str:
        slug = _normalize_text(self.cleaned_data.get("slug")).lower()
        if not slug:
            return slug
        qs = Tag.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A tag with this slug already exists."),
                code="duplicate_slug",
            )
        return slug

class VariantTypeForm(forms.ModelForm):
    class Meta:
        model = VariantType
        fields = ["name", "display_order", "is_active"]

    def clean_name(self) -> str:
        name = _normalize_text(self.cleaned_data.get("name"))
        if not name:
            return name
        qs = VariantType.objects.filter(name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A variant type with this name already exists."),
                code="duplicate_name",
            )
        return name

class VariantOptionForm(forms.ModelForm):
    class Meta:
        model = VariantOption
        fields = [
            "variant_type",
            "value",
            "color_code",
            "swatch_image",
            "display_order",
            "is_active",
        ]

    def clean_color_code(self) -> str:
        value = _normalize_text(self.cleaned_data.get("color_code"))
        if not value:
            return value
        return value.upper()

class ProductHighlightForm(forms.ModelForm):
    class Meta:
        model = ProductHighlight
        fields = ["name", "icon_class", "display_order", "is_active"]

class TrustBadgeForm(forms.ModelForm):
    class Meta:
        model = TrustBadge
        fields = ["name", "image", "description", "display_order", "is_active"]

class ProductLabelForm(forms.ModelForm):
    class Meta:
        model = ProductLabel
        fields = [
            "name",
            "slug",
            "text_color",
            "bg_color",
            "display_order",
            "is_active",
        ]
        widgets = {
            "text_color": forms.TextInput(attrs={"placeholder": "#FFFFFF"}),
            "bg_color": forms.TextInput(attrs={"placeholder": "#2C2520"}),
        }

    def clean_slug(self) -> str:
        slug = _normalize_text(self.cleaned_data.get("slug"))
        if not slug:
            return slug
        qs = ProductLabel.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A label with this slug already exists."),
                code="duplicate_slug",
            )
        return slug

class ProductIconForm(forms.ModelForm):
    class Meta:
        model = ProductIcon
        fields = [
            "name",
            "icon_class",
            "image",
            "display_order",
            "is_active",
        ]

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "title",
            "slug",
            "sku",
            "barcode",
            "product_type",
            "model_number",
            "internal_reference",
            "short_description",
            "description",
            "story",
            "crafting_process",
            "care_instructions",
            "shipping_information",
            "delivery_promise",
            "return_policy",
            "warranty_information",
            "country_of_origin",
            "price",
            "original_price",
            "cost_price",
            "currency",
            "tax_class",
            "category",
            "artisan",
            "material",
            "hue",
            "ethical_standards",
            "highlights",
            "trust_badges",
            "labels",
            "icons",
            "primary_image",
            "hover_image",
            "video_url",
            "length",
            "width",
            "height",
            "weight",
            "badge_text",
            "secondary_badge_text",
            "ribbon_text",
            "ribbon_bg_color",
            "ribbon_text_color",
            "rating",
            "reviews_count",
            "status",
            "is_featured",
            "is_active",
            "position",
            "published_at",
            "publish_from",
            "publish_until",
            "seo_title",
            "seo_description",
            "seo_keywords",
            "meta_title",
            "meta_description",
            "meta_keywords",
            "canonical_url",
            "robots_directives",
            "og_title",
            "og_description",
            "og_image",
            "twitter_title",
            "twitter_description",
            "twitter_image",
            "structured_data",
        ]
        widgets = {
            "publish_from": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "vDateTimeField"}
            ),
            "publish_until": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "vDateTimeField"}
            ),
            "published_at": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "vDateTimeField"}
            ),
            "short_description": forms.Textarea(attrs={"rows": 3}),
            "structured_data": forms.Textarea(attrs={"rows": 4}),
        }

    def clean_slug(self) -> str:
        slug = _normalize_text(self.cleaned_data.get("slug"))
        if not slug:
            return slug
        qs = Product.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A product with this slug already exists."),
                code="duplicate_slug",
            )
        return slug

    def clean_sku(self) -> str:
        sku = _normalize_text(self.cleaned_data.get("sku"))
        if not sku:
            return sku
        qs = Product.objects.filter(sku__iexact=sku)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A product with this SKU already exists."),
                code="duplicate_sku",
            )
        return sku

    def clean_barcode(self) -> str:
        barcode = _normalize_text(self.cleaned_data.get("barcode"))
        if not barcode:
            return barcode
        qs = Product.objects.filter(barcode__iexact=barcode)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A product with this barcode already exists."),
                code="duplicate_barcode",
            )
        return barcode

    def clean_internal_reference(self) -> str:
        reference = _normalize_text(self.cleaned_data.get("internal_reference"))
        if not reference:
            return reference
        qs = Product.objects.filter(internal_reference__iexact=reference)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A product with this internal reference already exists."),
                code="duplicate_internal_reference",
            )
        return reference

    def clean_related_products(self) -> List[Product]:
        related = self.cleaned_data.get("related_products")
        if not related:
            return related
        try:
            if self.instance and self.instance.pk and self.instance in related:
                raise ValidationError(
                    _("A product cannot be set as related to itself."),
                )
        except ValidationError:
            raise
        except Exception:
            pass
        return related

    def clean_upsell_products(self) -> List[Product]:
        upsell = self.cleaned_data.get("upsell_products")
        if not upsell:
            return upsell
        try:
            if self.instance and self.instance.pk and self.instance in upsell:
                raise ValidationError(
                    _("A product cannot be set as an upsell of itself."),
                )
        except ValidationError:
            raise
        except Exception:
            pass
        return upsell

    def clean_cross_sell_products(self) -> List[Product]:
        cross = self.cleaned_data.get("cross_sell_products")
        if not cross:
            return cross
        try:
            if self.instance and self.instance.pk and self.instance in cross:
                raise ValidationError(
                    _("A product cannot be set as a cross-sell of itself."),
                )
        except ValidationError:
            raise
        except Exception:
            pass
        return cross

    def clean_structured_data(self) -> Any:
        value = self.cleaned_data.get("structured_data")
        if value in (None, ""):
            return value if value == "" else None
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValidationError(
                    _("Invalid JSON format for structured data."),
                    code="invalid_json",
                )
        return value

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()

        price = _normalize_decimal(_safe_get(cleaned_data, "price"))
        original_price = _normalize_decimal(
            _safe_get(cleaned_data, "original_price")
        )
        if (
            price is not None
            and original_price is not None
            and original_price <= price
        ):
            self.add_error(
                "original_price",
                _(
                    "Original price must be greater than current price "
                    "to represent a valid discount."
                ),
            )

        publish_from = _safe_get(cleaned_data, "publish_from")
        publish_until = _safe_get(cleaned_data, "publish_until")
        if (
            publish_from
            and publish_until
            and publish_from >= publish_until
        ):
            self.add_error(
                "publish_until",
                _(
                    "Publish-until date must be strictly after the "
                    "publish-from date."
                ),
            )

        return cleaned_data

class ProductVariantForm(forms.ModelForm):
    class Meta:
        model = ProductVariant
        fields = [
            "product",
            "name",
            "sku",
            "barcode",
            "price_override",
            "compare_price",
            "weight",
            "length",
            "width",
            "height",
            "image",
            "attributes",
            "color_code",
            "is_active",
            "is_default",
            "sort_order",
        ]
        widgets = {
            "attributes": forms.Textarea(attrs={"rows": 3}),
            "color_code": forms.TextInput(attrs={"placeholder": "#000000"}),
        }

    def clean_sku(self) -> str:
        sku = _normalize_text(self.cleaned_data.get("sku"))
        if not sku:
            return sku
        qs = ProductVariant.objects.filter(sku__iexact=sku)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A product variant with this SKU already exists."),
                code="duplicate_sku",
            )
        return sku

    def clean_barcode(self) -> str:
        barcode = _normalize_text(self.cleaned_data.get("barcode"))
        if not barcode:
            return barcode
        qs = ProductVariant.objects.filter(barcode__iexact=barcode)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A product variant with this barcode already exists."),
                code="duplicate_barcode",
            )
        return barcode

    def clean_attributes(self) -> Any:
        value = self.cleaned_data.get("attributes")
        if value in (None, ""):
            return value if value == "" else {}
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValidationError(
                    _("Invalid JSON format for variant attributes."),
                    code="invalid_json",
                )
        return value

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()

        price_override = _normalize_decimal(
            _safe_get(cleaned_data, "price_override")
        )
        compare_price = _normalize_decimal(
            _safe_get(cleaned_data, "compare_price")
        )
        if (
            price_override is not None
            and compare_price is not None
            and compare_price <= price_override
        ):
            self.add_error(
                "compare_price",
                _(
                    "Compare At Price must be greater than the Price "
                    "Override to represent a valid discount."
                ),
            )

        return cleaned_data

class ProductImageForm(forms.ModelForm):
    class Meta:
        model = ProductImage
        fields = [
            "product",
            "image",
            "title",
            "alt_text",
            "caption",
            "is_primary",
            "position",
            "is_active",
        ]

    def clean_alt_text(self) -> str:
        return _normalize_text(self.cleaned_data.get("alt_text"))

class ProductGalleryImageForm(forms.ModelForm):
    class Meta:
        model = ProductGalleryImage
        fields = [
            "product",
            "image",
            "alt_text",
            "caption",
            "media_type",
            "sort_order",
            "is_active",
        ]

    def clean_alt_text(self) -> str:
        return _normalize_text(self.cleaned_data.get("alt_text"))

    def clean_media_type(self) -> str:
        value = _normalize_text(
            _safe_get(self.cleaned_data, "media_type"),
            max_length=20,
        )
        if not value:
            return "image"
        return value.lower()

class ProductTagForm(forms.ModelForm):
    class Meta:
        model = ProductTag
        fields = ["name", "slug", "description", "is_active", "products"]

    def clean_slug(self) -> str:
        slug = _normalize_text(
            _safe_get(self.cleaned_data, "slug")
        ).lower()
        if not slug:
            return slug
        qs = ProductTag.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A tag with this slug already exists."),
                code="duplicate_slug",
            )
        return slug

class ProductCollectionForm(forms.ModelForm):
    class Meta:
        model = ProductCollection
        fields = [
            "name",
            "slug",
            "description",
            "image",
            "is_active",
            "is_featured",
            "sort_order",
            "products",
        ]

    def clean_slug(self) -> str:
        slug = _normalize_text(
            _safe_get(self.cleaned_data, "slug")
        ).lower()
        if not slug:
            return slug
        qs = ProductCollection.objects.filter(slug__iexact=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError(
                _("A collection with this slug already exists."),
                code="duplicate_slug",
            )
        return slug

class ProductSpecificationForm(forms.ModelForm):
    class Meta:
        model = ProductSpecification
        fields = [
            "product",
            "label",
            "value",
            "group",
            "display_order",
            "is_active",
        ]

class ProductFAQForm(forms.ModelForm):
    class Meta:
        model = ProductFAQ
        fields = [
            "product",
            "question",
            "answer",
            "display_order",
            "is_active",
        ]

class ProductVideoForm(forms.ModelForm):
    class Meta:
        model = ProductVideo
        fields = [
            "product",
            "title",
            "description",
            "video_url",
            "thumbnail",
            "duration_seconds",
            "display_order",
            "is_active",
        ]

class ProductSEOForm(forms.ModelForm):
    class Meta:
        model = ProductSEO
        fields = [
            "product",
            "meta_title",
            "meta_description",
            "keywords",
            "canonical_url",
            "robots",
            "og_title",
            "og_description",
            "og_image",
            "twitter_title",
            "twitter_description",
            "twitter_image",
        ]
        widgets = {
            "meta_description": forms.Textarea(attrs={"rows": 3}),
            "og_description": forms.Textarea(attrs={"rows": 3}),
            "twitter_description": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_canonical_url(self) -> str:
        return _normalize_text(self.cleaned_data.get("canonical_url"))

class ProductSchemaForm(forms.ModelForm):
    class Meta:
        model = ProductSchema
        fields = ["product", "schema_type", "schema_data", "is_active"]
        widgets = {
            "schema_data": forms.Textarea(
                attrs={"rows": 6, "class": "vLargeTextField"}
            ),
        }

    def clean_schema_data(self) -> Any:
        value = self.cleaned_data.get("schema_data")
        if value in (None, ""):
            return value if value == "" else {}
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValidationError(
                    _("Invalid JSON structure provided for Schema Data."),
                    code="invalid_json",
                )
        return value

class PublishingWorkflowForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "status",
            "is_active",
            "published_at",
            "publish_from",
            "publish_until",
        ]
        widgets = {
            "published_at": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "vDateTimeField"}
            ),
            "publish_from": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "vDateTimeField"}
            ),
            "publish_until": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "vDateTimeField"}
            ),
        }

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()

        publish_from = _safe_get(cleaned_data, "publish_from")
        publish_until = _safe_get(cleaned_data, "publish_until")
        if (
            publish_from
            and publish_until
            and publish_from >= publish_until
        ):
            self.add_error(
                "publish_until",
                _(
                    "The end of the publishing window must occur "
                    "after the start."
                ),
            )

        return cleaned_data

class ProductFilterForm(forms.Form):
    """
    Comprehensive multi-faceted discovery filter form for capture and validation
    of GET query parameters across storefront catalog, search, and collections.
    """
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": _("Search products..."), "class": "form-input"}
        ),
        label=_("Search Keyword"),
    )
    min_price = forms.DecimalField(
        required=False,
        min_value=Decimal("0.00"),
        decimal_places=2,
        label=_("Minimum Price"),
    )
    max_price = forms.DecimalField(
        required=False,
        min_value=Decimal("0.00"),
        decimal_places=2,
        label=_("Maximum Price"),
    )
    in_stock_only = forms.BooleanField(
        required=False,
        label=_("In-Stock Items Only"),
    )
    min_rating = forms.ChoiceField(
        required=False,
        choices=[("", _("All Ratings")), ("5", "5★"), ("4", "4★ & above"), ("3", "3★ & above"), ("2", "2★ & above"), ("1", "1★ & above")],
        label=_("Minimum Rating"),
    )
    category = forms.ModelMultipleChoiceField(
        queryset=Category.objects.filter(is_active=True),
        required=False,
        label=_("Categories"),
    )
    artisan = forms.ModelMultipleChoiceField(
        queryset=Artisan.objects.filter(is_active=True).order_by("name"),
        required=False,
        label=_("Master Craftsmen"),
    )
    material = forms.ModelMultipleChoiceField(
        queryset=Material.objects.all().order_by("name"),
        required=False,
        label=_("Materials"),
    )
    hue = forms.ModelMultipleChoiceField(
        queryset=Hue.objects.all().order_by("name"),
        required=False,
        label=_("Hues & Colors"),
    )
    ethical_standards = forms.ModelMultipleChoiceField(
        queryset=EthicalStandard.objects.filter(is_active=True).order_by("name"),
        required=False,
        label=_("Ethical Standards"),
    )
    tags = forms.ModelMultipleChoiceField(
        queryset=ProductTag.objects.filter(is_active=True).order_by("name"),
        required=False,
        label=_("Product Tags"),
    )
    collections = forms.ModelMultipleChoiceField(
        queryset=ProductCollection.objects.filter(is_active=True).order_by("name"),
        required=False,
        label=_("Craft Collections"),
    )
    featured = forms.BooleanField(required=False, label=_("Featured Masterpieces Only"))
    on_sale = forms.BooleanField(required=False, label=_("On Sale Items Only"))
    min_discount_pct = forms.IntegerField(
        required=False,
        min_value=0,
        max_value=100,
        label=_("Minimum Discount %"),
    )
    sort_by = forms.ChoiceField(
        required=False,
        choices=[
            ("", _("Featured Collection")),
            ("featured", _("Featured Collection")),
            ("newest", _("Newest Arrivals")),
            ("oldest", _("Oldest First")),
            ("price-low", _("Price: Low to High")),
            ("price-high", _("Price: High to Low")),
            ("rating", _("Highest Rated")),
            ("popularity", _("Most Popular")),
            ("name-asc", _("Name: A to Z")),
            ("name-desc", _("Name: Z to A")),
        ],
        label=_("Sort by"),
    )

    def clean_min_discount_pct(self) -> Optional[int]:
        val = self.cleaned_data.get("min_discount_pct")
        if val is not None and (val < 0 or val > 100):
            raise forms.ValidationError(_("Discount percentage must be between 0% and 100%."))
        return val

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        min_price = _normalize_decimal(_safe_get(cleaned_data, "min_price"))
        max_price = _normalize_decimal(_safe_get(cleaned_data, "max_price"))

        if (
            min_price is not None
            and max_price is not None
            and min_price > max_price
        ):
            self.add_error(
                "max_price",
                _("Maximum price must be greater than or equal to minimum price."),
            )
        return cleaned_data

__all__ = [
    "CategoryForm",
    "ArtisanForm",
    "MaterialForm",
    "HueForm",
    "EthicalStandardForm",
    "CollectionForm",
    "TagForm",
    "VariantTypeForm",
    "VariantOptionForm",
    "ProductHighlightForm",
    "TrustBadgeForm",
    "ProductLabelForm",
    "ProductIconForm",
    "ProductForm",
    "ProductVariantForm",
    "ProductImageForm",
    "ProductGalleryImageForm",
    "ProductTagForm",
    "ProductCollectionForm",
    "ProductSpecificationForm",
    "ProductFAQForm",
    "ProductVideoForm",
    "ProductSEOForm",
    "ProductSchemaForm",
    "PublishingWorkflowForm",
    "ProductFilterForm",
    "_safe_get",
    "_normalize_decimal",
    "_normalize_integer",
    "_normalize_text",
    "_parse_json_field",
]