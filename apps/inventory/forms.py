"""
Enterprise-grade Django Forms for the Inventory application.

Responsible exclusively for data collection, input validation, and preparing
cleaned data for downstream consumption by the service layer.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_ADJUSTMENT_REASON = "manual_correction"

# ==============================================================================
# LAZY ACCESSORS & CONFIGURATION HELPERS
# ==============================================================================
def _get_inventory_model():
    from apps.inventory.models import Inventory
    return Inventory

def _get_stock_reservation_model():
    from apps.inventory.models import StockReservation
    return StockReservation

def _get_stock_adjustment_model():
    from apps.inventory.models import StockAdjustment
    return StockAdjustment

def _get_warehouse_model():
    from apps.inventory.models import Warehouse
    return Warehouse

def get_default_reservation_minutes() -> int:
    minutes = getattr(settings, "INVENTORY_DEFAULT_RESERVATION_MINUTES", _DEFAULT_RESERVATION_MINUTES)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = _DEFAULT_RESERVATION_MINUTES
    return max(1, minutes)

def get_default_adjustment_reason() -> str:
    return getattr(settings, "INVENTORY_DEFAULT_ADJUSTMENT_REASON", _DEFAULT_ADJUSTMENT_REASON)

# ==============================================================================
# REUSABLE VALIDATION HELPERS
# ==============================================================================
def _validate_quantity(value: Any, *, field_name: str = "quantity", allow_zero: bool = False) -> Decimal:
    if value in (None, ""):
        raise forms.ValidationError({field_name: _("Quantity must be provided.")}, code="required")
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise forms.ValidationError({field_name: _("Quantity must be a valid decimal number.")}, code="invalid")
    if qty.is_nan() or qty.is_infinite():
        raise forms.ValidationError({field_name: _("Quantity must be a finite decimal number.")}, code="invalid")
    if not allow_zero and qty <= Decimal("0"):
        raise forms.ValidationError({field_name: _("Quantity must be strictly greater than zero.")}, code="min_value")
    if allow_zero and qty < Decimal("0"):
        raise forms.ValidationError({field_name: _("Quantity must be greater than or equal to zero.")}, code="min_value")
    return qty.quantize(Decimal("0.01"))

def _validate_warehouse(warehouse: Any, *, field_name: str = "warehouse") -> Any:
    if warehouse is None:
        raise forms.ValidationError({field_name: _("A valid warehouse must be selected.")}, code="required")
    if not getattr(warehouse, "is_active", True):
        raise forms.ValidationError({field_name: _("Selected warehouse is inactive.")}, code="inactive")
    return warehouse

def _validate_inventory_target(inventory: Any, *, field_name: str = "inventory") -> Any:
    if inventory is None:
        raise forms.ValidationError({field_name: _("A valid inventory record must be selected.")}, code="required")
    if not getattr(inventory, "is_active", True):
        raise forms.ValidationError({field_name: _("Selected inventory record is inactive.")}, code="inactive")
    if getattr(inventory, "product_variant_id", None) is None and getattr(inventory, "product_id", None) is None:
        raise forms.ValidationError({field_name: _("Inventory record is missing product reference.")}, code="invalid")
    return inventory

# ==============================================================================
# FORMS
# ==============================================================================
class InventoryForm(forms.ModelForm):
    """Admin configuration form for inventory thresholds and metadata."""

    class Meta:
        model = None
        fields = [
            "warehouse",
            "product",
            "product_variant",
            "minimum_stock",
            "maximum_stock",
            "reorder_level",
            "location_bin",
            "notes",
            "is_active",
        ]
        widgets = {
            "warehouse": forms.Select(attrs={"class": "form-select"}),
            "product": forms.Select(attrs={"class": "form-select"}),
            "product_variant": forms.Select(attrs={"class": "form-select"}),
            "minimum_stock": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "maximum_stock": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "reorder_level": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "location_bin": forms.TextInput(attrs={"class": "form-control", "placeholder": "Rack A-12, Shelf 3"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if self.Meta.model is None:
            self.Meta.model = _get_inventory_model()
        super().__init__(*args, **kwargs)

    def clean_warehouse(self) -> Any:
        return _validate_warehouse(self.cleaned_data.get("warehouse"))

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        product = cleaned_data.get("product")
        product_variant = cleaned_data.get("product_variant")

        if bool(product_variant) == bool(product):
            raise forms.ValidationError(
                _("Inventory must reference exactly one of Product Variant or Product."),
                code="invalid_target",
            )

        min_stock = cleaned_data.get("minimum_stock")
        max_stock = cleaned_data.get("maximum_stock")
        reorder_level = cleaned_data.get("reorder_level")

        if min_stock is not None and max_stock is not None and min_stock > max_stock:
            raise forms.ValidationError(
                {"minimum_stock": _("Minimum stock must be less than or equal to maximum stock.")},
                code="min_max_violation",
            )
        if reorder_level is not None and reorder_level < Decimal("0"):
            raise forms.ValidationError({"reorder_level": _("Reorder level cannot be negative.")}, code="negative_reorder")

        return cleaned_data

class StockAdjustmentForm(forms.ModelForm):
    """Form for submitting manual stock adjustments."""

    class Meta:
        model = None
        fields = [
            "inventory",
            "new_quantity",
            "reason",
            "description",
            "supporting_documents",
            "approved_by",
        ]
        widgets = {
            "inventory": forms.Select(attrs={"class": "form-select"}),
            "new_quantity": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "reason": forms.Select(attrs={"class": "form-select"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "supporting_documents": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
            "approved_by": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if self.Meta.model is None:
            self.Meta.model = _get_stock_adjustment_model()
        super().__init__(*args, **kwargs)

    def clean_inventory(self) -> Any:
        return _validate_inventory_target(self.cleaned_data.get("inventory"))

    def clean_new_quantity(self) -> Decimal:
        return _validate_quantity(self.cleaned_data.get("new_quantity"), field_name="new_quantity", allow_zero=True)

    def clean_supporting_documents(self) -> Any:
        value = self.cleaned_data.get("supporting_documents")
        if not value:
            return []
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                raise forms.ValidationError(_("Supporting documents must be valid JSON."), code="invalid_json")
            if not isinstance(parsed, list):
                raise forms.ValidationError(_("Supporting documents must be a JSON list."), code="invalid_type")
            return parsed
        return value

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        if not cleaned_data.get("reason"):
            cleaned_data["reason"] = get_default_adjustment_reason()
        return cleaned_data

class TransferStockForm(forms.Form):
    """Form for inter-warehouse stock transfers."""

    source_warehouse = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs={"class": "form-select"}))
    destination_warehouse = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs={"class": "form-select"}))
    product_variant = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    product = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    quantity = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}))
    reference_number = forms.CharField(max_length=120, required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        Warehouse = _get_warehouse_model()
        active_warehouses = Warehouse.objects.filter(is_active=True).order_by("name")
        self.fields["source_warehouse"].queryset = active_warehouses
        self.fields["destination_warehouse"].queryset = active_warehouses

        try:
            from apps.catalog.models import Product, ProductVariant
            self.fields["product_variant"].queryset = ProductVariant.objects.filter(is_active=True).select_related("product").order_by("sku")
            self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by("title")
        except Exception:
            pass

    def clean_source_warehouse(self) -> Any:
        return _validate_warehouse(self.cleaned_data.get("source_warehouse"), field_name="source_warehouse")

    def clean_destination_warehouse(self) -> Any:
        return _validate_warehouse(self.cleaned_data.get("destination_warehouse"), field_name="destination_warehouse")

    def clean_quantity(self) -> Decimal:
        return _validate_quantity(self.cleaned_data.get("quantity"), field_name="quantity")

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        source = cleaned_data.get("source_warehouse")
        destination = cleaned_data.get("destination_warehouse")
        product = cleaned_data.get("product")
        product_variant = cleaned_data.get("product_variant")

        if source and destination and source == destination:
            raise forms.ValidationError({"destination_warehouse": _("Source and destination warehouses must be different.")}, code="same_warehouse")

        if bool(product_variant) == bool(product):
            raise forms.ValidationError(_("Transfer must target exactly one of Product Variant or Product."), code="invalid_target")

        return cleaned_data

class RestockForm(forms.Form):
    """Form for receiving inventory."""

    inventory = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs={"class": "form-select"}))
    quantity = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}))
    supplier_reference = forms.CharField(max_length=120, required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    remarks = forms.CharField(required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        Inventory = _get_inventory_model()
        self.fields["inventory"].queryset = (
            Inventory.objects.filter(is_active=True)
            .select_related("warehouse", "product", "product_variant")
            .order_by("warehouse__name", "product__title", "product_variant__sku")
        )

    def clean_inventory(self) -> Any:
        return _validate_inventory_target(self.cleaned_data.get("inventory"))

    def clean_quantity(self) -> Decimal:
        return _validate_quantity(self.cleaned_data.get("quantity"), field_name="quantity")

class ReservationForm(forms.Form):
    """Form for manual stock reservations."""

    cart = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    product_variant = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    product = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    warehouse = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs={"class": "form-select"}))
    quantity = forms.DecimalField(min_value=Decimal("0.01"), decimal_places=2, widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}))
    reservation_type = forms.ChoiceField(choices=[], widget=forms.Select(attrs={"class": "form-select"}))
    expires_in_minutes = forms.IntegerField(required=False, min_value=1, widget=forms.NumberInput(attrs={"class": "form-control"}))
    reference_number = forms.CharField(max_length=120, required=False, widget=forms.TextInput(attrs={"class": "form-control"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        Warehouse = _get_warehouse_model()
        StockReservation = _get_stock_reservation_model()

        self.fields["warehouse"].queryset = Warehouse.objects.filter(is_active=True).order_by("name")
        self.fields["reservation_type"].choices = StockReservation.ReservationType.choices
        self.fields["reservation_type"].initial = StockReservation.ReservationType.CART

        try:
            from apps.cart.models import Cart
            from apps.catalog.models import Product, ProductVariant
            self.fields["cart"].queryset = Cart.objects.filter(is_active=True).order_by("-last_activity_at")
            self.fields["product_variant"].queryset = ProductVariant.objects.filter(is_active=True).select_related("product").order_by("sku")
            self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by("title")
        except Exception:
            pass

    def clean_quantity(self) -> Decimal:
        return _validate_quantity(self.cleaned_data.get("quantity"), field_name="quantity")

    def clean_expires_in_minutes(self) -> Optional[int]:
        val = self.cleaned_data.get("expires_in_minutes")
        if val is None:
            return None
        if int(val) < 1:
            raise forms.ValidationError(_("Expiry must be at least 1 minute."), code="min_value")
        return int(val)

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        product = cleaned_data.get("product")
        product_variant = cleaned_data.get("product_variant")

        if bool(product_variant) == bool(product):
            raise forms.ValidationError(_("Reservation must target exactly one of Product Variant or Product."), code="invalid_target")

        if not cleaned_data.get("expires_in_minutes"):
            cleaned_data["expires_in_minutes"] = get_default_reservation_minutes()

        return cleaned_data

__all__ = [
    "InventoryForm",
    "StockAdjustmentForm",
    "TransferStockForm",
    "RestockForm",
    "ReservationForm",
    "get_default_reservation_minutes",
    "get_default_adjustment_reason",
]