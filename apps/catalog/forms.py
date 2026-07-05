from __future__ import annotations

import json
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.db.models import Min, Max

from .models import (
    Artisan,
    Category,
    Collection,
    EthicalStandard,
    Hue,
    Material,
    Product,
    ProductCollection,
    ProductGalleryImage,
    ProductImage,
    ProductSchema,
    ProductSEO,
    ProductTag,
    ProductVariant,
    Tag,
    VariantOption,
    VariantType,
)

class CategoryForm(forms.ModelForm):
    """
    Enterprise form for Category management.
    Includes validation to prevent recursive nesting and restricts depth to 2 levels.
    """
    class Meta:
        model = Category
        fields = "__all__"

    def clean(self):
        cleaned_data = super().clean()
        parent = cleaned_data.get("parent")
        
        if parent:
            if self.instance and self.instance.pk == parent.pk:
                self.add_error("parent", _("A category cannot be its own parent."))
            
            if parent.parent:
                self.add_error("parent", _("Nesting categories beyond 2 levels (Category -> Subcategory) is not supported."))
                
        return cleaned_data

class ProductForm(forms.ModelForm):
    """
    Comprehensive form for Product Masterpiece management.
    Handles data validation for prices, publishing dates, identification codes, and relationships.
    """
    class Meta:
        model = Product
        fields = "__all__"
        widgets = {
            "publish_from": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "vDateTimeField"}),
            "publish_until": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "vDateTimeField"}),
            "published_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "vDateTimeField"}),
            "short_description": forms.Textarea(attrs={"rows": 3}),
            "structured_data": forms.Textarea(attrs={"rows": 4}),
        }

    def clean_sku(self):
        sku = self.cleaned_data.get("sku")
        if sku:
            sku = sku.strip()
            qs = Product.objects.filter(sku__iexact=sku)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(_("A product with this SKU already exists."))
        return sku

    def clean_barcode(self):
        barcode = self.cleaned_data.get("barcode")
        if barcode:
            barcode = barcode.strip()
            qs = Product.objects.filter(barcode__iexact=barcode)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(_("A product with this barcode already exists."))
        return barcode

    def clean_related_products(self):
        related_products = self.cleaned_data.get("related_products")
        if related_products and self.instance and self.instance.pk:
            if self.instance in related_products:
                raise ValidationError(_("A product cannot be set as related to itself."))
        return related_products
        
    def clean_structured_data(self):
        structured_data = self.cleaned_data.get("structured_data")
        if structured_data:
            if isinstance(structured_data, str):
                try:
                    structured_data = json.loads(structured_data)
                except json.JSONDecodeError:
                    raise ValidationError(_("Invalid JSON format for structured data."))
        return structured_data

    def clean(self):
        cleaned_data = super().clean()
        
        # Pricing Validation
        price = cleaned_data.get("price")
        original_price = cleaned_data.get("original_price")
        if price and original_price and original_price <= price:
            self.add_error("original_price", _("Original price must be greater than current price to represent a valid discount."))

        # Publishing Window Validation
        publish_from = cleaned_data.get("publish_from")
        publish_until = cleaned_data.get("publish_until")
        if publish_from and publish_until and publish_from >= publish_until:
            self.add_error("publish_until", _("Publish until date must be strictly after the publish from date."))
            
        return cleaned_data

class ProductFilterForm(forms.Form):
    """
    Advanced filter form for the Product catalog.
    """
    search = forms.CharField(required=False, widget=forms.TextInput(attrs={'placeholder': 'Search products...'}))
    min_price = forms.DecimalField(required=False, min_value=0)
    max_price = forms.DecimalField(required=False, min_value=0)
    category = forms.ModelMultipleChoiceField(queryset=Category.objects.filter(is_active=True), required=False)
    artisan = forms.ModelMultipleChoiceField(queryset=Artisan.objects.filter(is_active=True), required=False)
    material = forms.ModelMultipleChoiceField(queryset=Material.objects.all(), required=False)
    hue = forms.ModelMultipleChoiceField(queryset=Hue.objects.all(), required=False)
    featured = forms.BooleanField(required=False)
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Dynamic price range logic could be initialized here if needed
        self.fields['artisan'].queryset = Artisan.objects.filter(is_active=True).order_by('name')

class ProductVariantForm(forms.ModelForm):
    """
    Enterprise form for handling Product Variants.
    Includes SKU/Barcode uniqueness and price override validations.
    """
    class Meta:
        model = ProductVariant
        fields = "__all__"
        widgets = {
            "attributes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_sku(self):
        sku = self.cleaned_data.get("sku")
        if sku:
            sku = sku.strip()
            qs = ProductVariant.objects.filter(sku__iexact=sku)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(_("A product variant with this SKU already exists."))
        return sku

    def clean_barcode(self):
        barcode = self.cleaned_data.get("barcode")
        if barcode:
            barcode = barcode.strip()
            qs = ProductVariant.objects.filter(barcode__iexact=barcode)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(_("A product variant with this barcode already exists."))
        return barcode
        
    def clean_attributes(self):
        attributes = self.cleaned_data.get("attributes")
        if attributes:
            if isinstance(attributes, str):
                try:
                    attributes = json.loads(attributes)
                except json.JSONDecodeError:
                    raise ValidationError(_("Invalid JSON format for variant attributes."))
        return attributes

    def clean(self):
        cleaned_data = super().clean()
        
        # Pricing Override Validation
        price_override = cleaned_data.get("price_override")
        compare_price = cleaned_data.get("compare_price")
        if price_override and compare_price and compare_price <= price_override:
            self.add_error("compare_price", _("Compare At Price must be greater than the Price Override to represent a valid discount."))
            
        return cleaned_data

class ProductImageForm(forms.ModelForm):
    """
    Form for validating primary and legacy gallery image metadata.
    """
    class Meta:
        model = ProductImage
        fields = "__all__"

    def clean_alt_text(self):
        alt_text = self.cleaned_data.get("alt_text")
        if alt_text:
            alt_text = alt_text.strip()
        return alt_text

class ProductGalleryImageForm(forms.ModelForm):
    """
    Form for validating explicitly structured gallery images.
    """
    class Meta:
        model = ProductGalleryImage
        fields = "__all__"

    def clean_alt_text(self):
        alt_text = self.cleaned_data.get("alt_text")
        if alt_text:
            alt_text = alt_text.strip()
        return alt_text

class ProductTagForm(forms.ModelForm):
    """
    Form for Product Tag management with uniqueness constraints.
    """
    class Meta:
        model = ProductTag
        fields = "__all__"

    def clean_slug(self):
        slug = self.cleaned_data.get("slug")
        if slug:
            slug = slug.strip().lower()
            qs = ProductTag.objects.filter(slug=slug)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(_("A tag with this slug already exists."))
        return slug

class ProductCollectionForm(forms.ModelForm):
    """
    Form for Product Collection curation management.
    """
    class Meta:
        model = ProductCollection
        fields = "__all__"

    def clean_slug(self):
        slug = self.cleaned_data.get("slug")
        if slug:
            slug = slug.strip().lower()
            qs = ProductCollection.objects.filter(slug=slug)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise ValidationError(_("A collection with this slug already exists."))
        return slug

class ProductSEOForm(forms.ModelForm):
    """
    Dedicated form for SEO field management.
    """
    class Meta:
        model = ProductSEO
        fields = "__all__"
        widgets = {
            "meta_description": forms.Textarea(attrs={"rows": 3}),
            "og_description": forms.Textarea(attrs={"rows": 3}),
            "twitter_description": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_canonical_url(self):
        canonical_url = self.cleaned_data.get("canonical_url")
        if canonical_url:
            canonical_url = canonical_url.strip()
        return canonical_url

class ProductSchemaForm(forms.ModelForm):
    """
    Dedicated form for Schema.org JSON structured data management.
    """
    class Meta:
        model = ProductSchema
        fields = "__all__"
        widgets = {
            "schema_data": forms.Textarea(attrs={"rows": 6, "class": "vLargeTextField"}),
        }

    def clean_schema_data(self):
        schema_data = self.cleaned_data.get("schema_data")
        if schema_data:
            if isinstance(schema_data, str):
                try:
                    schema_data = json.loads(schema_data)
                except json.JSONDecodeError:
                    raise ValidationError(_("Invalid JSON structure provided for Schema Data."))
        return schema_data

class PublishingWorkflowForm(forms.ModelForm):
    """
    Specialized workflow form strictly handling Product publishing timelines and status.
    Designed for use in custom CMS dashboards and bulk action views.
    """
    class Meta:
        model = Product
        fields = ["status", "is_active", "published_at", "publish_from", "publish_until"]
        widgets = {
            "published_at": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "vDateTimeField"}),
            "publish_from": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "vDateTimeField"}),
            "publish_until": forms.DateTimeInput(attrs={"type": "datetime-local", "class": "vDateTimeField"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        
        status = cleaned_data.get("status")
        publish_from = cleaned_data.get("publish_from")
        publish_until = cleaned_data.get("publish_until")
        published_at = cleaned_data.get("published_at")

        # Validate Scheduling constraints
        if publish_from and publish_until and publish_from >= publish_until:
            self.add_error("publish_until", _("The end of the publishing window must occur after the start."))

        # Auto-stamp published_at if transitioning to PUBLISHED without a timestamp
        if status == Product.ProductStatus.PUBLISHED and not published_at:
            cleaned_data["published_at"] = timezone.now()
            
        return cleaned_data