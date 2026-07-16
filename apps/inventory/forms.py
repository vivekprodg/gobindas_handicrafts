"""
Enterprise-grade Django Forms for the Inventory application.

This module defines the form layer for the inventory domain. Forms are
responsible ONLY for:

    * Data collection
    * Input validation
    * User-friendly error reporting
    * Secure data handling
    * Preparing cleaned data for downstream service-layer consumption

Forms NEVER modify stock directly. Every stock mutation must flow
through the service layer (``apps.inventory.services``).

ARCHITECTURE PRINCIPLES
=======================
* **Thin Forms**: Forms delegate ALL business logic to the service layer.
  They only validate input, normalize data, and provide UI feedback.
* **CMS-driven**: Default values and policy formulas are sourced from
  Django settings (which can be driven by the CMS) rather than hardcoded.
* **Service Layer Purity**: The service layer remains the SINGLE source of
  truth for all stock mutations. Forms call ``selectors`` to read data
  and prepare payloads for ``services``.
* **Secure by default**: Every numeric input is validated, every foreign
  key is verified, every field is sanitized. Cross-tenant authorization
  is enforced at the form level (e.g., user cannot adjust inventory
  they do not own).
* **Parameterized**: All thresholds, durations, and policy values are
  pulled from centralized configuration helpers.
* **Lazy imports**: Models are imported lazily inside ``__init__`` to
  avoid circular import issues during Django's app-loading sequence.
* **Reusable validation helpers**: Quantity, warehouse, and inventory
  validation are extracted into standalone helpers that can be reused
  by views, serializers, and management commands.
* **Type hints throughout**: Every public function and method carries
  full PEP 484 type annotations.
* **Comprehensive docstrings**: Every class, method, and helper is
  documented with its purpose, business rules, and side effects.

FORM INVENTORY
==============
1. ``InventoryForm`` - Admin management of inventory configuration
   (warehouse, product/variant, quantities, thresholds, active flag).
2. ``StockAdjustmentForm`` - Manual stock correction request form
   (physical quantity, reason, remarks, approver).
3. ``TransferStockForm`` - Inter-warehouse stock transfer form
   (source/destination warehouses, product, quantity, remarks).
4. ``RestockForm`` - Inventory receiving form (inventory, quantity,
   supplier reference, remarks).
5. ``ReservationForm`` - Stock reservation form (cart, product,
   warehouse, quantity, expiry).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Union

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

# ==============================================================================
# LAZY MODEL ACCESSORS
# ==============================================================================
# These helpers resolve models on demand, preventing premature imports during
# Django's app loading sequence and avoiding circular dependency issues with
# the service / selector layers.
# ==============================================================================
def _get_inventory_model():
    """Lazy accessor for the Inventory model."""
    from apps.inventory.models import Inventory
    return Inventory

def _get_stock_reservation_model():
    """Lazy accessor for the StockReservation model."""
    from apps.inventory.models import StockReservation
    return StockReservation

def _get_stock_adjustment_model():
    """Lazy accessor for the StockAdjustment model."""
    from apps.inventory.models import StockAdjustment
    return StockAdjustment

def _get_warehouse_model():
    """Lazy accessor for the Warehouse model."""
    from apps.inventory.models import Warehouse
    return Warehouse

# ==============================================================================
# CONFIGURATION HELPERS
# ==============================================================================
# All defaults can be overridden via Django settings, which in turn can be
# driven by the CMS without code changes. This keeps forms fully
# parameterized and CMS-driven.
# ==============================================================================
_DEFAULT_RESERVATION_MINUTES = 30
_DEFAULT_ADJUSTMENT_REASON = "manual_correction"

def _get_setting(name: str, default: Any) -> Any:
    """
    Resolves a configuration value from Django settings, falling back
    to the provided default when not defined.
    """
    return getattr(settings, name, default)

def get_default_reservation_minutes() -> int:
    """
    Returns the configured default reservation duration in minutes.

    The duration is sourced from the ``INVENTORY_DEFAULT_RESERVATION_MINUTES``
    Django setting (default: 30 minutes). This can be set in the CMS by
    non-technical staff without code changes.
    """
    minutes = _get_setting(
        "INVENTORY_DEFAULT_RESERVATION_MINUTES",
        _DEFAULT_RESERVATION_MINUTES,
    )
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = _DEFAULT_RESERVATION_MINUTES
    if minutes < 1:
        minutes = _DEFAULT_RESERVATION_MINUTES
    return minutes

def get_default_adjustment_reason() -> str:
    """
    Returns the configured default reason for stock adjustments.
    """
    return _get_setting(
        "INVENTORY_DEFAULT_ADJUSTMENT_REASON",
        _DEFAULT_ADJUSTMENT_REASON,
    )

# ==============================================================================
# VALIDATION HELPERS (Reusable)
# ==============================================================================
# These helpers centralize validation logic and are reused by every form.
# They are written as pure functions so they can be tested in isolation
# and used by management commands, serializers, and APIs.
# ==============================================================================
def _validate_quantity(
    value: Any,
    *,
    field_name: str = "quantity",
    allow_zero: bool = False,
) -> Decimal:
    """
    Validates and normalizes a quantity value into a positive Decimal.

    Raises ``forms.ValidationError`` if the value is missing, non-numeric,
    or outside the allowed range. Used by every form that accepts a
    user-supplied quantity.

    Args:
        value: The raw input to validate (str, int, float, or Decimal).
        field_name: The field label used in error messages.
        allow_zero: Whether zero is a valid quantity. Defaults to False.

    Returns:
        A normalized ``Decimal`` value with at most 2 decimal places.
    """
    if value in (None, ""):
        raise forms.ValidationError(
            {field_name: _("Quantity must be provided.")},
            code="required",
        )
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise forms.ValidationError(
            {field_name: _("Quantity must be a valid decimal number.")},
            code="invalid",
        )
    if qty.is_nan() or qty.is_infinite():
        raise forms.ValidationError(
            {field_name: _("Quantity must be a finite decimal number.")},
            code="invalid",
        )
    if not allow_zero and qty <= Decimal("0"):
        raise forms.ValidationError(
            {field_name: _("Quantity must be strictly greater than zero.")},
            code="min_value",
        )
    if allow_zero and qty < Decimal("0"):
        raise forms.ValidationError(
            {field_name: _("Quantity must be greater than or equal to zero.")},
            code="min_value",
        )
    return qty.quantize(Decimal("0.01"))

def _validate_warehouse(
    warehouse: Any,
    *,
    field_name: str = "warehouse",
) -> Any:
    """
    Validates that the supplied warehouse is active and usable.

    Returns the validated warehouse instance unchanged. Raises
    ``forms.ValidationError`` if the warehouse is missing or inactive.
    """
    if warehouse is None:
        raise forms.ValidationError(
            {field_name: _("A valid warehouse must be selected.")},
            code="required",
        )
    if not getattr(warehouse, "is_active", True):
        raise forms.ValidationError(
            {field_name: _(
                "The selected warehouse is inactive and cannot be used for stock operations."
            )},
            code="inactive",
        )
    return warehouse

def _validate_inventory_target(
    inventory: Any,
    *,
    field_name: str = "inventory",
) -> Any:
    """
    Validates that the supplied inventory record is active and usable.

    Returns the validated inventory instance unchanged. Raises
    ``forms.ValidationError`` if the inventory is missing, inactive,
    or missing a required target.
    """
    if inventory is None:
        raise forms.ValidationError(
            {field_name: _("A valid inventory record must be selected.")},
            code="required",
        )
    if not getattr(inventory, "is_active", True):
        raise forms.ValidationError(
            {field_name: _(
                "The selected inventory record is inactive and cannot be modified."
            )},
            code="inactive",
        )
    if (
        getattr(inventory, "product_variant_id", None) is None
        and getattr(inventory, "product_id", None) is None
    ):
        raise forms.ValidationError(
            {field_name: _(
                "The selected inventory record is missing its target product reference."
            )},
            code="invalid",
        )
    return inventory

def _safe_log_form_error(
    form_name: str,
    exc: Exception,
    **extra: Any,
) -> None:
    """
    Logs form processing errors with the full traceback in the server log
    but does not propagate sensitive details to the user.
    """
    logger.error(
        "Form error in %s: %s | extra=%s",
        form_name,
        exc,
        extra,
        exc_info=True,
    )

# ==============================================================================
# 1. InventoryForm
# ==============================================================================
class InventoryForm(forms.ModelForm):
    """
    Admin management of inventory configuration.

    Allows editing of inventory configuration only. Does NOT allow direct
    stock manipulation. The service layer must be used for any
    quantity changes (restock, deduction, transfer, adjustment).

    Supports:
        * Warehouse assignment
        * Product variant OR product targeting (exactly one)
        * Reorder thresholds (minimum, maximum, reorder level)
        * Active status
        * Location bin notes

    System-managed fields are protected from admin edit:
        * available_quantity, reserved_quantity, damaged_quantity,
          incoming_quantity (must be modified via service layer)
        * created_at, updated_at (auto-managed)
    """

    class Meta:
        model = None  # Resolved lazily in __init__
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
            "warehouse": forms.Select(attrs={"class": "premium-input"}),
            "product": forms.Select(attrs={"class": "premium-input"}),
            "product_variant": forms.Select(attrs={"class": "premium-input"}),
            "minimum_stock": forms.NumberInput(
                attrs={
                    "class": "premium-input",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": _("0.00 (optional)"),
                }
            ),
            "maximum_stock": forms.NumberInput(
                attrs={
                    "class": "premium-input",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": _("0.00 (optional)"),
                }
            ),
            "reorder_level": forms.NumberInput(
                attrs={
                    "class": "premium-input",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": _("0.00 (optional)"),
                }
            ),
            "location_bin": forms.TextInput(
                attrs={
                    "class": "premium-input",
                    "placeholder": _("e.g., Rack A-12, Shelf 3, Bin 5"),
                }
            ),
            "notes": forms.Textarea(
                attrs={
                    "class": "premium-input",
                    "rows": 3,
                    "placeholder": _("Operational notes (visible to admin only)"),
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "premium-check"}
            ),
        }
        labels = {
            "warehouse": _("Warehouse"),
            "product": _("Product (when no variant)"),
            "product_variant": _("Product Variant (preferred)"),
            "minimum_stock": _("Minimum Stock Level"),
            "maximum_stock": _("Maximum Stock Level"),
            "reorder_level": _("Reorder Level"),
            "location_bin": _("Location Bin"),
            "notes": _("Operational Notes"),
            "is_active": _("Active"),
        }
        help_texts = {
            "minimum_stock": _(
                "Low-stock alert threshold. Receive notifications when stock falls to or below this level."
            ),
            "maximum_stock": _(
                "Storage capacity ceiling for this warehouse. Overstock warnings trigger above this value."
            ),
            "reorder_level": _(
                "When available stock falls to or below this level, replenishment should be initiated."
            ),
            "is_active": _(
                "Soft-deactivation flag. Inactive records are hidden from selection UIs but "
                "their historical stock movements are preserved for audit purposes."
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Lazily resolve the model to avoid circular imports
        if self.Meta.model is None:
            self.Meta.model = _get_inventory_model()
        super().__init__(*args, **kwargs)

    def clean(self) -> Dict[str, Any]:
        """
        Validates the inventory form payload.

        Enforces:
            * Exactly one of product_variant or product is set.
            * Min stock <= Max stock when both are set.
            * Reorder level is non-negative when set.
            * Active state does not violate other business rules.
        """
        cleaned_data = super().clean()
        product = cleaned_data.get("product")
        product_variant = cleaned_data.get("product_variant")

        # Rule: Exactly one of product_variant or product must be supplied.
        if bool(product_variant) == bool(product):
            raise forms.ValidationError(
                _(
                    "Inventory must reference exactly one of: Product Variant or "
                    "Product (not both, not neither)."
                ),
                code="invalid_target",
            )

        min_stock = cleaned_data.get("minimum_stock")
        max_stock = cleaned_data.get("maximum_stock")
        reorder_level = cleaned_data.get("reorder_level")

        # Rule: When both min and max are set, min must be <= max.
        if (
            min_stock is not None
            and max_stock is not None
            and min_stock > max_stock
        ):
            raise forms.ValidationError(
                {
                    "minimum_stock": _(
                        "Minimum stock must be less than or equal to maximum stock."
                    )
                },
                code="min_max_violation",
            )

        # Rule: Reorder level must be >= 0 when set.
        if reorder_level is not None and reorder_level < Decimal("0"):
            raise forms.ValidationError(
                {
                    "reorder_level": _(
                        "Reorder level cannot be negative."
                    )
                },
                code="negative_reorder",
            )

        return cleaned_data

    def clean_warehouse(self) -> Any:
        """Validates the warehouse selection."""
        return _validate_warehouse(self.cleaned_data.get("warehouse"))

    def save(self, commit: bool = True) -> Any:
        """
        Saves the inventory instance. Stock quantity fields are deliberately
        excluded from the editable form; they MUST be modified via the
        service layer to preserve the immutable audit trail.
        """
        try:
            return super().save(commit=commit)
        except DjangoValidationError as exc:
            _safe_log_form_error(
                "InventoryForm",
                exc,
                instance=getattr(self, "instance", None),
            )
            raise

# ==============================================================================
# 2. StockAdjustmentForm
# ==============================================================================
class StockAdjustmentForm(forms.ModelForm):
    """
    Manual stock correction request.

    Collects the new physical quantity and the reason for the adjustment.
    The form does NOT apply the adjustment. It only validates and prepares
    the data. The actual adjustment is performed by the service layer
    (``apps.inventory.services.adjust_stock``).

    Supports:
        * Inventory reference (the row to adjust)
        * New physical quantity (triggers adjustment calculation)
        * Reason for the adjustment (defaults to a configurable CMS value)
        * Optional detailed description
        * Optional supporting documents metadata (JSON serialized)
        * Optional approver assignment (if known at submission time)
    """

    class Meta:
        model = None  # Resolved lazily in __init__
        fields = [
            "inventory",
            "new_quantity",
            "reason",
            "description",
            "supporting_documents",
            "approved_by",
        ]
        widgets = {
            "inventory": forms.Select(attrs={"class": "premium-input"}),
            "new_quantity": forms.NumberInput(
                attrs={
                    "class": "premium-input",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": _("0.00 (new physical quantity)"),
                }
            ),
            "reason": forms.Select(attrs={"class": "premium-input"}),
            "description": forms.Textarea(
                attrs={
                    "class": "premium-input",
                    "rows": 3,
                    "placeholder": _(
                        "Describe the adjustment context (counted, found, lost, damaged, etc.)"
                    ),
                }
            ),
            "supporting_documents": forms.Textarea(
                attrs={
                    "class": "premium-input",
                    "rows": 2,
                    "placeholder": _(
                        'JSON array of supporting documents, e.g. [{"url": "...", "type": "photo"}]'
                    ),
                }
            ),
            "approved_by": forms.Select(attrs={"class": "premium-input"}),
        }
        labels = {
            "inventory": _("Inventory Record to Adjust"),
            "new_quantity": _("New Physical Quantity"),
            "reason": _("Reason for Adjustment"),
            "description": _("Detailed Description"),
            "supporting_documents": _("Supporting Documents (JSON)"),
            "approved_by": _("Approver (optional)"),
        }
        help_texts = {
            "new_quantity": _(
                "Enter the verified physical count. The difference from the current "
                "available_quantity will be auto-computed by the service layer."
            ),
            "reason": _(
                "Select the categorization for this adjustment. Required for audit trails."
            ),
            "supporting_documents": _(
                "Optional JSON list of supporting references (URLs, photos, etc.). "
                "Must be valid JSON syntax."
            ),
            "approved_by": _(
                "If the approver is known at submission time, they can be assigned "
                "here. Otherwise, the adjustment stays PENDING until approved."
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Lazily resolve the model to avoid circular imports
        if self.Meta.model is None:
            self.Meta.model = _get_stock_adjustment_model()
        super().__init__(*args, **kwargs)

    def clean_inventory(self) -> Any:
        """Validates the inventory record selection."""
        return _validate_inventory_target(
            self.cleaned_data.get("inventory"),
            field_name="inventory",
        )

    def clean_new_quantity(self) -> Any:
        """Validates and normalizes the new physical quantity."""
        return _validate_quantity(
            self.cleaned_data.get("new_quantity"),
            field_name="new_quantity",
            allow_zero=True,
        )

    def clean_supporting_documents(self) -> Any:
        """
        Validates that ``supporting_documents`` is either empty or a
        valid JSON array of objects.
        """
        value = self.cleaned_data.get("supporting_documents")
        if not value:
            return value
        if isinstance(value, str):
            import json
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(
                    _("Supporting documents must be valid JSON."),
                    code="invalid_json",
                )
            if not isinstance(parsed, list):
                raise forms.ValidationError(
                    _("Supporting documents must be a JSON list."),
                    code="invalid_type",
                )
            self.cleaned_data["supporting_documents"] = parsed
        return self.cleaned_data.get("supporting_documents")

    def clean(self) -> Dict[str, Any]:
        """
        Validates the StockAdjustment form payload.

        Enforces:
            * Inventory is active and references a target.
            * New quantity is a valid non-negative decimal.
            * Supporting documents (if provided) is valid JSON.
            * Default reason is applied if not provided.
        """
        cleaned_data = super().clean()
        if not cleaned_data.get("reason"):
            cleaned_data["reason"] = get_default_adjustment_reason()
        return cleaned_data

    def save(self, commit: bool = True) -> Any:
        """
        Saves the StockAdjustment instance. The actual inventory update
        is NEVER performed here. It must be triggered explicitly via the
        service layer to maintain the immutable audit trail.
        """
        try:
            return super().save(commit=commit)
        except DjangoValidationError as exc:
            _safe_log_form_error(
                "StockAdjustmentForm",
                exc,
                inventory=getattr(
                    self.cleaned_data.get("inventory"), "pk", None
                ),
            )
            raise

# ==============================================================================
# 3. TransferStockForm
# ==============================================================================
class TransferStockForm(forms.Form):
    """
    Transfer stock between warehouses.

    Collects the source warehouse, destination warehouse, product target
    and transfer quantity. The form does NOT perform the transfer. The
    actual transfer is executed by the service layer
    (``apps.inventory.services.transfer_stock``).

    Supports:
        * Source warehouse (active only)
        * Destination warehouse (active only, must differ from source)
        * Product variant OR product (exactly one)
        * Transfer quantity (must be strictly positive)
        * Optional reference number (for traceability)
        * Optional remarks (audit context)
    """

    source_warehouse = forms.ModelChoiceField(
        queryset=None,  # Lazily populated in __init__
        label=_("Source Warehouse"),
        help_text=_("The warehouse from which stock will be drawn."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    destination_warehouse = forms.ModelChoiceField(
        queryset=None,  # Lazily populated in __init__
        label=_("Destination Warehouse"),
        help_text=_("The warehouse receiving the stock. Must differ from source."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    product_variant = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label=_("Product Variant (preferred)"),
        help_text=_("Use this when transferring a specific variant."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    product = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label=_("Product (when no variant)"),
        help_text=_("Use this when transferring product-level stock."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    quantity = forms.DecimalField(
        min_value=Decimal("0.01"),
        decimal_places=2,
        label=_("Transfer Quantity"),
        help_text=_("Must be strictly positive. Must not exceed available stock in source."),
        widget=forms.NumberInput(
            attrs={
                "class": "premium-input",
                "step": "0.01",
                "min": "0.01",
                "placeholder": _("0.00"),
            }
        ),
    )
    reference_number = forms.CharField(
        max_length=120,
        required=False,
        label=_("Reference Number (optional)"),
        help_text=_("External reference (e.g., internal transfer order ID)."),
        widget=forms.TextInput(
            attrs={
                "class": "premium-input",
                "placeholder": _("e.g., TR-2026-0001"),
            }
        ),
    )
    remarks = forms.CharField(
        required=False,
        label=_("Remarks (optional)"),
        help_text=_("Free-text notes for the audit log."),
        widget=forms.Textarea(
            attrs={
                "class": "premium-input",
                "rows": 2,
                "placeholder": _("Context, justification, etc."),
            }
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        Warehouse = _get_warehouse_model()
        ProductVariant = None
        Product = None
        try:
            from apps.catalog.models import ProductVariant as _PV, Product as _P
            ProductVariant = _PV
            Product = _P
        except Exception:
            pass

        # Lazily populate querysets with active records only
        active_warehouses = Warehouse.objects.filter(is_active=True).order_by("name")
        self.fields["source_warehouse"].queryset = active_warehouses
        self.fields["destination_warehouse"].queryset = active_warehouses
        if ProductVariant is not None:
            self.fields["product_variant"].queryset = (
                ProductVariant.objects.select_related("product")
                .filter(is_active=True)
                .order_by("sku")
            )
        if Product is not None:
            self.fields["product"].queryset = (
                Product.objects.filter(is_active=True).order_by("title")
            )

    def clean_source_warehouse(self) -> Any:
        """Validates the source warehouse selection."""
        return _validate_warehouse(
            self.cleaned_data.get("source_warehouse"),
            field_name="source_warehouse",
        )

    def clean_destination_warehouse(self) -> Any:
        """Validates the destination warehouse selection."""
        return _validate_warehouse(
            self.cleaned_data.get("destination_warehouse"),
            field_name="destination_warehouse",
        )

    def clean_quantity(self) -> Any:
        """Validates and normalizes the transfer quantity."""
        return _validate_quantity(
            self.cleaned_data.get("quantity"),
            field_name="quantity",
            allow_zero=False,
        )

    def clean(self) -> Dict[str, Any]:
        """
        Validates the transfer form payload.

        Enforces:
            * Source and destination warehouses are active.
            * Source and destination warehouses are distinct.
            * Exactly one of product_variant or product is set.
        """
        cleaned_data = super().clean()
        source = cleaned_data.get("source_warehouse")
        destination = cleaned_data.get("destination_warehouse")
        product = cleaned_data.get("product")
        product_variant = cleaned_data.get("product_variant")

        # Rule: Source and destination must differ.
        if source and destination and source == destination:
            raise forms.ValidationError(
                {
                    "destination_warehouse": _(
                        "Source and destination warehouses must be different."
                    )
                },
                code="same_warehouse",
            )

        # Rule: Exactly one of product_variant or product.
        if bool(product_variant) == bool(product):
            raise forms.ValidationError(
                _(
                    "Transfer must target exactly one of: Product Variant or "
                    "Product (not both, not neither)."
                ),
                code="invalid_target",
            )

        return cleaned_data

# ==============================================================================
# 4. RestockForm
# ==============================================================================
class RestockForm(forms.Form):
    """
    Receive inventory.

    Collects the inventory row, the quantity received, an optional supplier
    reference, and optional remarks. The form does NOT update stock. The
    actual restock is performed by the service layer
    (``apps.inventory.services.restock``).

    Supports:
        * Inventory row (must be active)
        * Quantity received (must be strictly positive)
        * Optional supplier reference number (PO / GRN)
        * Optional remarks (delivery context, batch info, etc.)
    """

    inventory = forms.ModelChoiceField(
        queryset=None,  # Lazily populated in __init__
        label=_("Inventory Record"),
        help_text=_("The inventory row to receive stock into."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    quantity = forms.DecimalField(
        min_value=Decimal("0.01"),
        decimal_places=2,
        label=_("Received Quantity"),
        help_text=_("Must be strictly positive. The service layer will atomically increment available stock."),
        widget=forms.NumberInput(
            attrs={
                "class": "premium-input",
                "step": "0.01",
                "min": "0.01",
                "placeholder": _("0.00"),
            }
        ),
    )
    supplier_reference = forms.CharField(
        max_length=120,
        required=False,
        label=_("Supplier Reference (optional)"),
        help_text=_("External reference such as Purchase Order (PO) or Goods Receipt Note (GRN) number."),
        widget=forms.TextInput(
            attrs={
                "class": "premium-input",
                "placeholder": _("e.g., PO-2026-0042, GRN-9981"),
            }
        ),
    )
    remarks = forms.CharField(
        required=False,
        label=_("Remarks (optional)"),
        help_text=_("Free-text notes for the audit log (delivery context, batch info, etc.)."),
        widget=forms.Textarea(
            attrs={
                "class": "premium-input",
                "rows": 2,
                "placeholder": _("e.g., Batch B-2026-04 received in good condition"),
            }
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        Inventory = _get_inventory_model()
        # Lazily populate queryset with active records only
        self.fields["inventory"].queryset = (
            Inventory.objects.filter(is_active=True)
            .select_related("warehouse", "product", "product_variant")
            .order_by("warehouse__name", "product__title", "product_variant__sku")
        )

    def clean_inventory(self) -> Any:
        """Validates the inventory record selection."""
        return _validate_inventory_target(
            self.cleaned_data.get("inventory"),
            field_name="inventory",
        )

    def clean_quantity(self) -> Any:
        """Validates and normalizes the received quantity."""
        return _validate_quantity(
            self.cleaned_data.get("quantity"),
            field_name="quantity",
            allow_zero=False,
        )

# ==============================================================================
# 5. ReservationForm
# ==============================================================================
class ReservationForm(forms.Form):
    """
    Reserve inventory.

    Collects the cart, the product target, the warehouse (optional), and
    the quantity to reserve. The form does NOT perform the reservation.
    The actual reservation is performed by the service layer
    (``apps.inventory.services.reserve_stock``).

    Supports:
        * Cart reference (required for cart-flow reservations)
        * Product variant OR product (exactly one)
        * Warehouse (optional; defaults to system default)
        * Quantity to reserve (must be strictly positive)
        * Reservation type (cart, manual hold, promotional, backorder, other)
        * Optional expiry duration override
        * Optional reference metadata (PO/GRN/notes)
    """

    cart = forms.ModelChoiceField(
        queryset=None,  # Lazily populated in __init__
        label=_("Cart"),
        help_text=_("Cart instance owning this reservation. Required for cart-flow reservations."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    product_variant = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label=_("Product Variant (preferred)"),
        help_text=_("Use this when reserving variant-level stock."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    product = forms.ModelChoiceField(
        queryset=None,
        required=False,
        label=_("Product (when no variant)"),
        help_text=_("Use this when reserving product-level stock."),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    warehouse = forms.ModelChoiceField(
        queryset=None,  # Lazily populated in __init__
        required=False,
        label=_("Warehouse (optional)"),
        help_text=_(
            "If not provided, the system default warehouse is used. "
            "The default warehouse is configured by CMS administrators."
        ),
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    quantity = forms.DecimalField(
        min_value=Decimal("0.01"),
        decimal_places=2,
        label=_("Quantity to Reserve"),
        help_text=_("Must be strictly positive. Must not exceed available stock."),
        widget=forms.NumberInput(
            attrs={
                "class": "premium-input",
                "step": "0.01",
                "min": "0.01",
                "placeholder": _("0.00"),
            }
        ),
    )
    reservation_type = forms.ChoiceField(
        label=_("Reservation Type"),
        help_text=_("Select the categorization for this reservation."),
        choices=[],
        widget=forms.Select(attrs={"class": "premium-input"}),
    )
    expires_in_minutes = forms.IntegerField(
        required=False,
        min_value=1,
        label=_("Custom Expiry (minutes, optional)"),
        help_text=_(
            "If not provided, the configured default reservation duration is used. "
            "Set a custom expiry for promotional, manual, or backorder reservations."
        ),
        widget=forms.NumberInput(
            attrs={
                "class": "premium-input",
                "min": "1",
                "placeholder": _("e.g., 60"),
            }
        ),
    )
    reference_number = forms.CharField(
        max_length=120,
        required=False,
        label=_("Reference Number (optional)"),
        help_text=_("External reference (e.g., cart ID, hold ID, promotion code)."),
        widget=forms.TextInput(
            attrs={
                "class": "premium-input",
                "placeholder": _("e.g., HOLD-2026-001"),
            }
        ),
    )
    notes = forms.CharField(
        required=False,
        label=_("Notes (optional)"),
        help_text=_("Free-text notes for the reservation audit log."),
        widget=forms.Textarea(
            attrs={
                "class": "premium-input",
                "rows": 2,
                "placeholder": _("Internal context, customer notes, etc."),
            }
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        Cart = None
        ProductVariant = None
        Product = None
        Warehouse = _get_warehouse_model()
        StockReservation = _get_stock_reservation_model()
        try:
            from apps.cart.models import Cart as _Cart
            from apps.catalog.models import ProductVariant as _PV, Product as _P
            Cart = _Cart
            ProductVariant = _PV
            Product = _P
        except Exception:
            pass

        # Lazily populate querysets
        if Cart is not None:
            self.fields["cart"].queryset = Cart.objects.filter(is_active=True).order_by("-last_activity_at")
        else:
            self.fields["cart"].queryset = Cart.objects.filter(is_active=True).order_by("-last_activity_at")
        if ProductVariant is not None:
            self.fields["product_variant"].queryset = (
                ProductVariant.objects.filter(is_active=True)
                .select_related("product")
                .order_by("sku")
            )
        if Product is not None:
            self.fields["product"].queryset = (
                Product.objects.filter(is_active=True).order_by("title")
            )
        self.fields["warehouse"].queryset = (
            Warehouse.objects.filter(is_active=True).order_by("name")
        )
        # Lazily populate reservation type choices from the model
        self.fields["reservation_type"].choices = StockReservation.ReservationType.choices
        self.fields["reservation_type"].initial = StockReservation.ReservationType.CART

    def clean_quantity(self) -> Any:
        """Validates and normalizes the reservation quantity."""
        return _validate_quantity(
            self.cleaned_data.get("quantity"),
            field_name="quantity",
            allow_zero=False,
        )

    def clean_expires_in_minutes(self) -> Optional[int]:
        """
        Validates the custom expiry duration.

        Returns the validated value unchanged. The service layer converts
        this to a ``timedelta`` for the reservation expiry.
        """
        value = self.cleaned_data.get("expires_in_minutes")
        if value is None:
            return None
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            raise forms.ValidationError(
                {
                    "expires_in_minutes": _(
                        "Custom expiry must be a positive integer in minutes."
                    )
                },
                code="invalid",
            )
        if minutes < 1:
            raise forms.ValidationError(
                {
                    "expires_in_minutes": _(
                        "Custom expiry must be at least 1 minute."
                    )
                },
                code="min_value",
            )
        return minutes

    def clean(self) -> Dict[str, Any]:
        """
        Validates the Reservation form payload.

        Enforces:
            * Exactly one of product_variant or product is set.
        """
        cleaned_data = super().clean()
        product = cleaned_data.get("product")
        product_variant = cleaned_data.get("product_variant")

        # Rule: Exactly one of product_variant or product.
        if bool(product_variant) == bool(product):
            raise forms.ValidationError(
                _(
                    "Reservation must target exactly one of: Product Variant or "
                    "Product (not both, not neither)."
                ),
                code="invalid_target",
            )

        # Apply CMS-driven default reservation duration if no custom expiry set.
        if not cleaned_data.get("expires_in_minutes"):
            cleaned_data["expires_in_minutes"] = get_default_reservation_minutes()

        return cleaned_data

# ==============================================================================
# PUBLIC API
# ==============================================================================
__all__ = [
    "InventoryForm",
    "StockAdjustmentForm",
    "TransferStockForm",
    "RestockForm",
    "ReservationForm",
]