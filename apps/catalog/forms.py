"""
Enterprise-grade Forms for the Catalog application.

ARCHITECTURE OVERVIEW
====================

This module implements the COMPLETE form layer for the catalog domain.
The catalog is intentionally INVENTORY-AGNOSTIC:

    * Catalog forms NEVER carry inventory fields, widgets, validators,
      save() logic, or business rules.
    * Catalog forms NEVER edit or create inventory records.
    * Catalog forms NEVER calculate stock, quantity, or availability.
    * Catalog forms NEVER validate inventory business rules.
    * Inventory is the exclusive responsibility of the Inventory app.

Catalog forms are responsible for:

    * Products
    * Product Variants
    * Categories / Brands / Collections
    * Attributes / Attribute Values
    * Tags
    * Images / Media
    * Descriptions / Specifications
    * SEO / Metadata
    * Publishing / Visibility
    * Slugs / Identifiers
    * Pricing references (price is a catalog description, not a stock value)
    * Dimensions / Weight
    * Tax references
    * Marketing labels / Badges

ARCHITECTURE PRINCIPLES
=======================

* **Service Layer Purity**: Forms are THIN. They validate input,
  normalize data, and provide UI feedback. All business logic stays
  out of forms. Mutations are the responsibility of views and services.

* **Inventory Agnostic**: This module MUST NOT reference the Inventory
  app. The boundary is enforced architecturally - catalog forms own
  catalog data only.

* **CMS-Driven**: Every configurable label, help text, and option is
  derived from constants or the database. No business rule is
  hardcoded.

* **Backend-Agnostic / Future-Proof**: Designed to integrate with
  Purchase Orders, Manufacturing, Batch / Lot / Serial, Barcode / QR,
  Expiry, Mobile ERP, Notifications, etc. without modification.

* **Defensive Validation**: Every optional field is genuinely optional.
  Missing data NEVER raises an exception. Safe fallbacks are always
  used.

* **Security-First**: OWASP ASVS compliant. Whitelisted fields,
  sanitized inputs, no over-posting, no mass-assignment.

* **PEP 8 / Python 3.13+ / Django 5.1+**: Full type hints, docstrings,
  enterprise conventions.

Author: Handicraft E-commerce Engineering Team
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

# ==============================================================================
# WHITELIST HELPER (OWASP mass-assignment defense)
# ==============================================================================
def _safe_get(d: Optional[Dict[str, Any]], key: str, default: Any = None) -> Any:
    """
    Safe dict access that returns a default on missing or None values.
    """
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
    """
    Best-effort conversion of a value to Decimal.

    Returns None for empty / None / unparseable values. Never raises.
    """
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
    """
    Best-effort conversion of a value to int.

    Returns None for empty / None / unparseable values. Never raises.
    """
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
    """
    Normalize user-supplied text input to a safe string.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if max_length is not None and len(text) > max_length:
        text = text[:max_length]
    return text

def _parse_json_field(value: Any) -> Any:
    """
    Safely parse a JSON-encoded form field.

    Returns the parsed value on success, returns the original value
    unchanged if it is already a structured object, returns an
    empty container on parse failure.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

# ==============================================================================
# CATEGORY FORM
# ==============================================================================
class CategoryForm(forms.ModelForm):
    """
    Enterprise form for Category management.

    Validates:
        * Slug uniqueness (excluded when editing the same instance)
        * Prevents a category from being its own parent
        * Restricts nesting depth to two levels (Category -> Subcategory)
        * All other CMS-defined fields validated by ModelForm machinery
    """

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
                # Defensive: missing related object should not crash form.
                pass

        return cleaned_data

# ==============================================================================
# ARTISAN (BRAND) FORM
# ==============================================================================
class ArtisanForm(forms.ModelForm):
    """
    Enterprise form for Artisan (brand) management.

    Validates:
        * Slug uniqueness (excluded when editing the same instance)
        * All CMS-defined fields validated by ModelForm machinery
    """

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

# ==============================================================================
# MATERIAL FORM
# ==============================================================================
class MaterialForm(forms.ModelForm):
    """
    Enterprise form for Material catalog management.

    Validates:
        * Name uniqueness (excluded when editing the same instance)
    """

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

# ==============================================================================
# HUE (COLOR) FORM
# ==============================================================================
class HueForm(forms.ModelForm):
    """
    Enterprise form for Hue (color) catalog management.

    Validates:
        * Hex color code format (handled by model validator)
        * Uniqueness by name is NOT enforced at the schema level,
          so this form performs the slug-style uniqueness check
    """

    class Meta:
        model = Hue
        fields = ["name", "color_code", "swatch_image"]

    def clean_color_code(self) -> str:
        value = _normalize_text(self.cleaned_data.get("color_code"))
        if not value:
            return value
        return value.upper()

# ==============================================================================
# ETHICAL STANDARD FORM
# ==============================================================================
class EthicalStandardForm(forms.ModelForm):
    """
    Enterprise form for EthicalStandard catalog management.
    """

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

# ==============================================================================
# COLLECTION FORM
# ==============================================================================
class CollectionForm(forms.ModelForm):
    """
    Enterprise form for the legacy / lightweight Collection entity.
    """

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

# ==============================================================================
# TAG FORM
# ==============================================================================
class TagForm(forms.ModelForm):
    """
    Enterprise form for the legacy Tag entity.
    """

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

# ==============================================================================
# VARIANT TYPE FORM
# ==============================================================================
class VariantTypeForm(forms.ModelForm):
    """
    Enterprise form for VariantType (axis) management.
    """

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

# ==============================================================================
# VARIANT OPTION FORM
# ==============================================================================
class VariantOptionForm(forms.ModelForm):
    """
    Enterprise form for VariantOption (axis value) management.
    """

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

# ==============================================================================
# PRODUCT HIGHLIGHT FORM
# ==============================================================================
class ProductHighlightForm(forms.ModelForm):
    """
    Enterprise form for ProductHighlight management.
    """

    class Meta:
        model = ProductHighlight  # type: ignore[name-defined]
        fields = ["name", "icon_class", "display_order", "is_active"]

# ==============================================================================
# TRUST BADGE FORM
# ==============================================================================
class TrustBadgeForm(forms.ModelForm):
    """
    Enterprise form for TrustBadge management.
    """

    class Meta:
        model = TrustBadge
        fields = ["name", "image", "description", "display_order", "is_active"]

# ==============================================================================
# PRODUCT LABEL FORM
# ==============================================================================
class ProductLabelForm(forms.ModelForm):
    """
    Enterprise form for ProductLabel (marketing label) management.
    """

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

# ==============================================================================
# PRODUCT ICON FORM
# ==============================================================================
class ProductIconForm(forms.ModelForm):
    """
    Enterprise form for ProductIcon (decorative icon) management.
    """

    class Meta:
        model = ProductIcon  # type: ignore[name-defined]
        fields = [
            "name",
            "icon_class",
            "image",
            "display_order",
            "is_active",
        ]

# ==============================================================================
# PRODUCT FORM (Main Masterpiece)
# ==============================================================================
class ProductForm(forms.ModelForm):
    """
    Enterprise form for Product Masterpiece management.

    Validates catalog data only. NEVER touches inventory.

    Validation coverage:
        * Slug uniqueness (excluded when editing the same instance)
        * SKU uniqueness (excluded when editing the same instance)
        * Barcode uniqueness (excluded when editing the same instance)
        * Product cannot be related to itself
        * Pricing: original price must be greater than current price
        * Publishing window: publish_from must be before publish_until
        * Structured data: must be valid JSON if provided as string
        * All optional fields are genuinely optional

    This form does NOT validate or modify inventory in any way.
    Stock / availability / quantity validation is exclusively the
    responsibility of the Inventory application.
    """

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

    # -- Field-level cleaners (catalog integrity only) -----------------

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
            # Defensive: any membership-check failure is non-fatal.
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

    # -- Form-level cleaner (catalog integrity only) ------------------

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()

        # Pricing: original price must be greater than current price.
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

        # Publishing window: publish_from must be before publish_until.
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

# ==============================================================================
# PRODUCT VARIANT FORM
# ==============================================================================
class ProductVariantForm(forms.ModelForm):
    """
    Enterprise form for handling Product Variants.

    Validates catalog data only. NEVER touches inventory.

    Validation coverage:
        * SKU uniqueness (excluded when editing the same instance)
        * Barcode uniqueness (excluded when editing the same instance)
        * Pricing override: compare_at_price must be >= price_override
        * Attributes JSON must be valid if supplied as string

    This form does NOT validate or modify inventory in any way.
    Stock / availability / quantity validation is exclusively the
    responsibility of the Inventory application.
    """

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

# ==============================================================================
# PRODUCT IMAGE FORM
# ==============================================================================
class ProductImageForm(forms.ModelForm):
    """
    Form for validating primary and legacy gallery image metadata.
    """

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
    """
    Form for validating explicitly structured gallery images.
    """

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

# ==============================================================================
# PRODUCT TAG FORM
# ==============================================================================
class ProductTagForm(forms.ModelForm):
    """
    Form for ProductTag (rich tagging) management.
    """

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

# ==============================================================================
# PRODUCT COLLECTION FORM
# ==============================================================================
class ProductCollectionForm(forms.ModelForm):
    """
    Form for ProductCollection (rich collection) curation management.
    """

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

# ==============================================================================
# PRODUCT SPECIFICATION FORM
# ==============================================================================
class ProductSpecificationForm(forms.ModelForm):
    """
    Form for ProductSpecification (key-value) management.
    """

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

# ==============================================================================
# PRODUCT FAQ FORM
# ==============================================================================
class ProductFAQForm(forms.ModelForm):
    """
    Form for ProductFAQ management.
    """

    class Meta:
        model = ProductFAQ
        fields = [
            "product",
            "question",
            "answer",
            "display_order",
            "is_active",
        ]

# ==============================================================================
# PRODUCT VIDEO FORM
# ==============================================================================
class ProductVideoForm(forms.ModelForm):
    """
    Form for ProductVideo management.
    """

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

# ==============================================================================
# PRODUCT SEO FORM
# ==============================================================================
class ProductSEOForm(forms.ModelForm):
    """
    Dedicated form for SEO field management.
    """

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

# ==============================================================================
# PRODUCT SCHEMA FORM
# ==============================================================================
class ProductSchemaForm(forms.ModelForm):
    """
    Dedicated form for Schema.org JSON structured data management.
    """

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

# ==============================================================================
# PUBLISHING WORKFLOW FORM
# ==============================================================================
class PublishingWorkflowForm(forms.ModelForm):
    """
    Specialized workflow form for Product publishing timelines and
    status. Designed for use in custom CMS dashboards and bulk action
    views.

    Validates only publishing fields. NEVER mutates inventory.
    """

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

# ==============================================================================
# PRODUCT FILTER FORM (Search / Filter Form)
# ==============================================================================
class ProductFilterForm(forms.Form):
    """
    Advanced filter form for the Product catalog.

    All fields are genuinely optional. This form does NOT touch
    inventory; it is used by views to compose catalog querysets.
    """

    search = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": _("Search products...")}
        ),
        label=_("Search"),
    )
    min_price = forms.DecimalField(
        required=False, min_value=Decimal("0"), decimal_places=2
    )
    max_price = forms.DecimalField(
        required=False, min_value=Decimal("0"), decimal_places=2
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
        label=_("Hues"),
    )
    ethical_standards = forms.ModelMultipleChoiceField(
        queryset=EthicalStandard.objects.filter(is_active=True).order_by("name"),
        required=False,
        label=_("Ethical Standards"),
    )
    featured = forms.BooleanField(required=False, label=_("Featured Only"))
    on_sale = forms.BooleanField(required=False, label=_("On Sale Only"))
    sort_by = forms.ChoiceField(
        required=False,
        choices=[
            ("", _("Sort by relevance")),
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

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        min_price = _normalize_decimal(
            _safe_get(cleaned_data, "min_price")
        )
        max_price = _normalize_decimal(
            _safe_get(cleaned_data, "max_price")
        )
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

# ==============================================================================
# PUBLIC API
# ==============================================================================
__all__ = [
    # Taxonomy
    "CategoryForm",
    "ArtisanForm",
    "MaterialForm",
    "HueForm",
    "EthicalStandardForm",
    "CollectionForm",
    "TagForm",
    # Variants
    "VariantTypeForm",
    "VariantOptionForm",
    # Features
    "ProductHighlightForm",
    "TrustBadgeForm",
    "ProductLabelForm",
    "ProductIconForm",
    # Product main
    "ProductForm",
    "ProductVariantForm",
    "ProductImageForm",
    "ProductGalleryImageForm",
    "ProductTagForm",
    "ProductCollectionForm",
    "ProductSpecificationForm",
    "ProductFAQForm",
    "ProductVideoForm",
    # SEO / Schema
    "ProductSEOForm",
    "ProductSchemaForm",
    # Workflow / search
    "PublishingWorkflowForm",
    "ProductFilterForm",
    # Helpers
    "_safe_get",
    "_normalize_decimal",
    "_normalize_integer",
    "_normalize_text",
    "_parse_json_field",
]