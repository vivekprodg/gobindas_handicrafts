from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.customers.forms import COUNTRY_CHOICES
from apps.orders import constants as c
from apps.orders import utils as u
from apps.orders.models import (
    Order,
    OrderAddressSnapshot,
    OrderAttachment,
    OrderItem,
    Payment,
    Refund,
    ReturnRequest,
    Shipment,
)

DEFAULT_INPUT_CLASS: str = "premium-input"
MAX_ATTACHMENT_SIZE: int = 25 * 1024 * 1024
MAX_IMPORT_SIZE: int = 10 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS: Tuple[str, ...] = (
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt"
)
ALLOWED_IMPORT_EXTENSIONS: Tuple[str, ...] = (".csv", ".json")

IMPORT_MODE_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("create", _("Create only")),
    ("update", _("Update only")),
    ("upsert", _("Create or update")),
)
REPLACEMENT_REASON_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("incorrect_file", _("Incorrect file")),
    ("damaged_file", _("Corrupted file")),
    ("outdated", _("Outdated version")),
    ("other", _("Other")),
)
EXPORT_FORMAT_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("csv", _("CSV")),
    ("json", _("JSON")),
)

def _input_attrs(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    attrs = {"class": DEFAULT_INPUT_CLASS}
    if extra:
        attrs.update(extra)
    return attrs

def _textarea_attrs(extra: Optional[Dict[str, Any]] = None, rows: int = 3) -> Dict[str, Any]:
    attrs = {"class": DEFAULT_INPUT_CLASS, "rows": rows}
    if extra:
        attrs.update(extra)
    return attrs

def build_email_field(**kwargs: Any) -> forms.EmailField:
    defaults = {"label": _("Email"), "required": True, "widget": forms.EmailInput(attrs=_input_attrs())}
    defaults.update(kwargs)
    return forms.EmailField(**defaults)

def build_decimal_field(
    *, label: str, required: bool = True, min_value: Decimal = c.ZERO_DECIMAL_2, initial: Optional[Decimal] = None
) -> forms.DecimalField:
    return forms.DecimalField(
        label=label,
        required=required,
        min_value=min_value,
        max_digits=14,
        decimal_places=2,
        initial=initial if initial is not None else c.ZERO_DECIMAL_2,
        widget=forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
    )

def build_quantity_field(*, label: str = _("Quantity"), min_value: int = 1, initial: int = 1) -> forms.IntegerField:
    return forms.IntegerField(
        label=label,
        required=True,
        min_value=min_value,
        initial=initial,
        widget=forms.NumberInput(attrs=_input_attrs({"min": str(min_value), "step": "1"})),
    )

def build_textarea_field(*, label: str, required: bool = True, rows: int = 3, placeholder: str = "") -> forms.CharField:
    attrs = _textarea_attrs({"placeholder": placeholder} if placeholder else None, rows=rows)
    return forms.CharField(label=label, required=required, widget=forms.Textarea(attrs=attrs))

def build_date_field(*, label: str, required: bool = False) -> forms.DateField:
    return forms.DateField(label=label, required=required, widget=forms.DateInput(attrs=_input_attrs({"type": "date"})))

def build_datetime_field(*, label: str, required: bool = False) -> forms.DateTimeField:
    return forms.DateTimeField(
        label=label, required=required, widget=forms.DateTimeInput(attrs=_input_attrs({"type": "datetime-local"}))
    )

class CheckoutAddressForm(forms.Form):
    """
    Form for selecting or capturing delivery and billing addresses during checkout.
    Supports B2B Wholesale metadata pre-fill (Company Name, Tax/PAN Number) for business accounts.
    """
    saved_shipping_address = forms.ChoiceField(
        required=False,
        label=_("Saved Shipping Address"),
        widget=forms.Select(attrs=_input_attrs()),
    )
    use_shipping_for_billing = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Billing address is the same as shipping address"),
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
    )

    full_name = forms.CharField(label=_("Full Name / Representative"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    company_name = forms.CharField(label=_("Company / Organization Name (Optional)"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    phone_number = forms.CharField(label=_("Telephone / Phone Number"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    email = forms.EmailField(label=_("Email Address"), required=False, widget=forms.EmailInput(attrs=_input_attrs()))
    address_line_1 = forms.CharField(label=_("Address Line 1"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    address_line_2 = forms.CharField(label=_("Address Line 2"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    city = forms.CharField(label=_("City"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    state_or_province = forms.CharField(label=_("State / Province"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    postal_code = forms.CharField(label=_("Postal Code"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    country = forms.ChoiceField(
        choices=COUNTRY_CHOICES,
        initial="Nepal",
        required=False,
        label=_("Country"),
        widget=forms.Select(attrs=_input_attrs({"data-searchable": "true"})),
    )
    customer_note = build_textarea_field(label=_("Special Delivery Instructions / Bulk Order Notes"), required=False, rows=2)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self.user = user

        if user and getattr(user, "is_authenticated", False):
            profile = getattr(user, "customer_profile", None)
            if profile and profile.is_business_account:
                if profile.company_name and not self.initial.get("company_name"):
                    self.initial["company_name"] = profile.company_name

            try:
                from apps.customers.models import CustomerAddress
                addresses = CustomerAddress.objects.filter(customer__user=user, is_active=True)
                choices = [("", _("-- Select a saved address --"))]
                for addr in addresses:
                    label = f"{addr.full_name} - {addr.address_line_1}, {addr.city}"
                    if addr.is_default:
                        label += _(" (Default)")
                    choices.append((str(addr.pk), label))
                choices.append(("new", _("+ Add a new address")))
                self.fields["saved_shipping_address"].choices = choices
            except Exception:
                self.fields["saved_shipping_address"].choices = [("", _("No saved addresses"))]
        else:
            self.fields["saved_shipping_address"].widget = forms.HiddenInput()

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        saved_id = cleaned_data.get("saved_shipping_address")
        is_authenticated = self.user and getattr(self.user, "is_authenticated", False)

        if not is_authenticated or saved_id == "new" or not saved_id:
            required_fields = ["full_name", "phone_number", "address_line_1", "city", "state_or_province", "country"]
            if not is_authenticated:
                required_fields.append("email")

            for field_name in required_fields:
                val = cleaned_data.get(field_name)
                if not val or not str(val).strip():
                    self.add_error(field_name, _("This field is required for delivery destination."))

        return cleaned_data

class OrderCreateForm(forms.Form):
    email = build_email_field()
    customer = forms.ModelChoiceField(
        label=_("Customer"), required=False, queryset=None, widget=forms.Select(attrs=_input_attrs())
    )
    currency = forms.ChoiceField(
        label=_("Currency"),
        required=True,
        initial=c.DEFAULT_CURRENCY_CODE,
        choices=[(c.DEFAULT_CURRENCY_CODE, c.DEFAULT_CURRENCY_CODE), ("USD", "USD"), ("EUR", "EUR"), ("INR", "INR")],
        widget=forms.Select(attrs=_input_attrs()),
    )
    source = forms.ChoiceField(
        label=_("Order Source"),
        required=True,
        initial=Order.Source.WEB,
        choices=Order.Source.choices,
        widget=forms.Select(attrs=_input_attrs()),
    )
    status = forms.ChoiceField(
        label=_("Initial Status"),
        required=True,
        initial=Order.OrderStatus.PENDING,
        choices=Order.OrderStatus.choices,
        widget=forms.Select(attrs=_input_attrs()),
    )
    payment_status = forms.ChoiceField(
        label=_("Initial Payment Status"),
        required=True,
        initial=Order.PaymentStatus.PENDING,
        choices=Order.PaymentStatus.choices,
        widget=forms.Select(attrs=_input_attrs()),
    )
    payment_method = forms.CharField(label=_("Payment Method"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    transaction_id = forms.CharField(label=_("Transaction ID"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    coupon_code = forms.CharField(label=_("Coupon Code"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    shipping_cost = build_decimal_field(label=_("Shipping Cost"), required=False)
    tax_total = build_decimal_field(label=_("Tax Total"), required=False)
    discount_total = build_decimal_field(label=_("Discount Total"), required=False)
    customer_note = build_textarea_field(label=_("Customer Note"), required=False, rows=3)
    notes = build_textarea_field(label=_("Internal Notes"), required=False, rows=3)
    is_gift = forms.BooleanField(
        label=_("Is Gift Order?"), required=False, initial=False, widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"})
    )
    gift_message = build_textarea_field(label=_("Gift Message"), required=False, rows=2)
    gift_wrapping = forms.CharField(label=_("Gift Wrapping"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    expected_delivery_date = build_date_field(label=_("Expected Delivery Date"))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        try:
            from django.contrib.auth import get_user_model
            self.fields["customer"].queryset = get_user_model().objects.all()
        except Exception:
            self.fields["customer"].queryset = None

class OrderUpdateForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = (
            "customer", "email", "status", "payment_status", "payment_method",
            "transaction_id", "currency", "shipping_cost", "tax_total",
            "discount_total", "coupon_code", "customer_note", "delivery_instructions",
            "has_invoice", "invoice_url", "tracking_number", "carrier", "is_gift",
            "gift_message", "gift_wrapping", "expected_delivery_date",
            "fraud_check_status", "risk_score", "is_active", "source",
            "external_order_id", "external_platform", "notes",
        )
        widgets = {
            "customer": forms.Select(attrs=_input_attrs()),
            "email": forms.EmailInput(attrs=_input_attrs()),
            "status": forms.Select(attrs=_input_attrs()),
            "payment_status": forms.Select(attrs=_input_attrs()),
            "payment_method": forms.TextInput(attrs=_input_attrs()),
            "transaction_id": forms.TextInput(attrs=_input_attrs()),
            "currency": forms.TextInput(attrs=_input_attrs()),
            "shipping_cost": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "tax_total": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "discount_total": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "coupon_code": forms.TextInput(attrs=_input_attrs()),
            "customer_note": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "delivery_instructions": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "has_invoice": forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
            "invoice_url": forms.URLInput(attrs=_input_attrs()),
            "tracking_number": forms.TextInput(attrs=_input_attrs()),
            "carrier": forms.TextInput(attrs=_input_attrs()),
            "is_gift": forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
            "gift_message": forms.Textarea(attrs=_textarea_attrs(rows=2)),
            "gift_wrapping": forms.TextInput(attrs=_input_attrs()),
            "expected_delivery_date": forms.DateInput(attrs=_input_attrs({"type": "date"})),
            "fraud_check_status": forms.Select(attrs=_input_attrs()),
            "risk_score": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "is_active": forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
            "source": forms.Select(attrs=_input_attrs()),
            "external_order_id": forms.TextInput(attrs=_input_attrs()),
            "external_platform": forms.TextInput(attrs=_input_attrs()),
            "notes": forms.Textarea(attrs=_textarea_attrs(rows=3)),
        }

class OrderEditForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ("customer_note", "delivery_instructions", "is_gift", "gift_message", "gift_wrapping")
        widgets = {
            "customer_note": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "delivery_instructions": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "is_gift": forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
            "gift_message": forms.Textarea(attrs=_textarea_attrs(rows=2)),
            "gift_wrapping": forms.TextInput(attrs=_input_attrs()),
        }

class OrderCancelForm(forms.Form):
    remarks = build_textarea_field(label=_("Cancellation Reason"), rows=3, placeholder=_("Reason for cancelling..."))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop("order", None)
        super().__init__(*args, **kwargs)

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if self.order and self.order.status not in c.OrderStatus.CANCELLABLE_FROM:
            raise ValidationError(_("Order cannot be cancelled in its current state."))
        return cleaned

class OrderStatusForm(forms.Form):
    status = forms.ChoiceField(label=_("New Status"), choices=Order.OrderStatus.choices, widget=forms.Select(attrs=_input_attrs()))
    remarks = build_textarea_field(label=_("Remarks"), required=False, rows=2)

class OrderSearchForm(forms.Form):
    q = forms.CharField(
        label=_("Search"), required=False, widget=forms.TextInput(attrs=_input_attrs({"placeholder": _("Order #, Email...")}))
    )
    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[("", _("-- Any --"))] + list(Order.OrderStatus.choices),
        widget=forms.Select(attrs=_input_attrs()),
    )
    payment_status = forms.ChoiceField(
        label=_("Payment Status"),
        required=False,
        choices=[("", _("-- Any --"))] + list(Order.PaymentStatus.choices),
        widget=forms.Select(attrs=_input_attrs()),
    )

class OrderFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    status = forms.MultipleChoiceField(
        label=_("Order Status"), required=False, choices=Order.OrderStatus.choices, widget=forms.CheckboxSelectMultiple()
    )
    payment_status = forms.MultipleChoiceField(
        label=_("Payment Status"), required=False, choices=Order.PaymentStatus.choices, widget=forms.CheckboxSelectMultiple()
    )
    min_total = build_decimal_field(label=_("Min Total"), required=False)
    max_total = build_decimal_field(label=_("Max Total"), required=False)

class OrderExportForm(forms.Form):
    format = forms.ChoiceField(
        label=_("Format"), required=True, initial="csv", choices=EXPORT_FORMAT_CHOICES, widget=forms.RadioSelect()
    )
    created_after = build_datetime_field(label=_("Created After"))
    created_before = build_datetime_field(label=_("Created Before"))

class OrderImportForm(forms.Form):
    file = forms.FileField(label=_("Import File"), required=True)
    import_mode = forms.ChoiceField(
        label=_("Import Mode"), required=True, initial="create", choices=IMPORT_MODE_CHOICES, widget=forms.RadioSelect()
    )
    dry_run = forms.BooleanField(
        label=_("Dry Run"), required=False, initial=True, widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"})
    )

class OrderMetadataForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ("json_metadata", "notes", "tags_text")
        widgets = {
            "json_metadata": forms.Textarea(attrs=_textarea_attrs(rows=4)),
            "notes": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "tags_text": forms.HiddenInput(),
        }

class OrderItemCreateForm(forms.Form):
    product_id = forms.IntegerField(label=_("Product ID"), required=False, widget=forms.NumberInput(attrs=_input_attrs()))
    variant_id = forms.IntegerField(label=_("Variant ID"), required=False, widget=forms.NumberInput(attrs=_input_attrs()))
    product_name = forms.CharField(label=_("Product Name"), required=True, widget=forms.TextInput(attrs=_input_attrs()))
    product_sku = forms.CharField(label=_("SKU"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    variant_name = forms.CharField(label=_("Variant"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    quantity = build_quantity_field()
    unit_price = build_decimal_field(label=_("Unit Price"), required=True)
    discount = build_decimal_field(label=_("Discount"), required=False)
    tax = build_decimal_field(label=_("Tax"), required=False)

class OrderItemUpdateForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = (
            "product_name_snapshot", "product_sku_snapshot", "variant_name_snapshot", "unit_price", "discount", "tax", "quantity", "status"
        )
        widgets = {
            "product_name_snapshot": forms.TextInput(attrs=_input_attrs()),
            "product_sku_snapshot": forms.TextInput(attrs=_input_attrs()),
            "variant_name_snapshot": forms.TextInput(attrs=_input_attrs()),
            "unit_price": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "discount": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "tax": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "quantity": forms.NumberInput(attrs=_input_attrs({"min": "1"})),
            "status": forms.Select(attrs=_input_attrs()),
        }

class QuantityUpdateForm(forms.Form):
    quantity = forms.IntegerField(label=_("Quantity"), required=True, min_value=1, widget=forms.NumberInput(attrs=_input_attrs({"min": "1"})))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.item: Optional[OrderItem] = kwargs.pop("item", None)
        super().__init__(*args, **kwargs)

class GiftForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = ("is_gift", "gift_message", "gift_wrapping")

class CustomizationForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = ("attributes", "personalization")

class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ("transaction_id", "gateway", "amount", "currency", "status", "payment_method")
        widgets = {
            "transaction_id": forms.TextInput(attrs=_input_attrs()),
            "gateway": forms.TextInput(attrs=_input_attrs()),
            "amount": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "currency": forms.TextInput(attrs=_input_attrs()),
            "status": forms.Select(attrs=_input_attrs()),
            "payment_method": forms.TextInput(attrs=_input_attrs()),
        }

class PaymentStatusForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ("status", "paid_at")

class RefundRequestForm(forms.Form):
    payment_id = forms.ChoiceField(label=_("Select Payment"), required=True, widget=forms.Select(attrs=_input_attrs()))
    amount = build_decimal_field(label=_("Requested Amount"), required=False)
    reason = build_textarea_field(label=_("Reason for Refund"), required=True, rows=4)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop("order", None)
        super().__init__(*args, **kwargs)
        if self.order:
            self.fields["payment_id"].choices = [
                (str(p.id), f"{p.gateway} - {p.transaction_id} ({p.amount} {p.currency})")
                for p in self.order.payments.all()
            ]

class PaymentSearchForm(forms.Form):
    transaction_id = forms.CharField(label=_("Transaction ID"), required=False, widget=forms.TextInput(attrs=_input_attrs()))
    status = forms.MultipleChoiceField(
        label=_("Status"), required=False, choices=Payment.PaymentState.choices, widget=forms.CheckboxSelectMultiple()
    )

class ShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ("carrier", "tracking_number", "tracking_url", "shipping_cost", "dispatch_date", "notes")
        widgets = {
            "carrier": forms.TextInput(attrs=_input_attrs()),
            "tracking_number": forms.TextInput(attrs=_input_attrs()),
            "tracking_url": forms.URLInput(attrs=_input_attrs()),
            "shipping_cost": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "dispatch_date": forms.DateTimeInput(attrs=_input_attrs({"type": "datetime-local"})),
            "notes": forms.Textarea(attrs=_textarea_attrs(rows=3)),
        }

class TrackingForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ("carrier", "tracking_number", "tracking_url")

class ShipmentStatusForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ("status", "dispatch_date", "delivery_date")

class CarrierForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ("carrier", "carrier_service_level")

class ReturnRequestForm(forms.Form):
    return_type = forms.ChoiceField(
        label=_("Return Type"), choices=ReturnRequest.ReturnType.choices, widget=forms.Select(attrs=_input_attrs())
    )
    reason_category = forms.ChoiceField(
        label=_("Reason Category"), choices=ReturnRequest.ReturnReasonCategory.choices, widget=forms.Select(attrs=_input_attrs())
    )
    reason_text = build_textarea_field(label=_("Details"), required=True, rows=4)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop("order", None)
        super().__init__(*args, **kwargs)

class ReturnApprovalForm(forms.ModelForm):
    class Meta:
        model = ReturnRequest
        fields = ("restock_decision", "restock_location")

class ReturnCompletionForm(forms.ModelForm):
    class Meta:
        model = ReturnRequest
        fields = ("restock_decision",)

class RefundApprovalForm(forms.ModelForm):
    class Meta:
        model = Refund
        fields = ()

class RefundCompletionForm(forms.ModelForm):
    class Meta:
        model = Refund
        fields = ("gateway_refund_id",)

class RefundSearchForm(forms.Form):
    order_number = forms.CharField(label=_("Order Number"), required=False, widget=forms.TextInput(attrs=_input_attrs()))

class AttachmentUploadForm(forms.ModelForm):
    class Meta:
        model = OrderAttachment
        fields = ("file", "attachment_type", "description", "is_visible_to_customer")
        widgets = {
            "file": forms.ClearableFileInput(attrs=_input_attrs()),
            "attachment_type": forms.Select(attrs=_input_attrs()),
            "description": forms.TextInput(attrs=_input_attrs()),
            "is_visible_to_customer": forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
        }

class AttachmentReplaceForm(forms.ModelForm):
    class Meta:
        model = OrderAttachment
        fields = ("file",)

class AttachmentDeleteForm(forms.Form):
    confirm = forms.BooleanField(
        label=_("Confirm deletion"), required=True, widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"})
    )

class OrderAddressForm(forms.ModelForm):
    country = forms.ChoiceField(
        choices=COUNTRY_CHOICES,
        initial="Nepal",
        required=True,
        label=_("Country"),
        widget=forms.Select(attrs=_input_attrs({"data-searchable": "true"})),
    )

    class Meta:
        model = OrderAddressSnapshot
        fields = (
            "full_name", "phone_number", "company", "address_line_1", "address_line_2",
            "city", "state_or_province", "postal_code", "country", "country_code"
        )
        widgets = {k: forms.TextInput(attrs=_input_attrs()) for k in fields if k != "country"}

class OrderNotesForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ("customer_note", "delivery_instructions")

class AdminOrderUpdateForm(forms.ModelForm):
    class Meta:
        model = Order
        fields = ("status", "payment_status", "carrier", "tracking_number", "invoice_url", "has_invoice", "is_active")

class GlobalOrderSearchForm(forms.Form):
    q = forms.CharField(
        label=_("Search"), required=True, min_length=2, widget=forms.TextInput(attrs=_input_attrs({"placeholder": _("Search...")}))
    )

class AdvancedOrderFilterForm(OrderFilterForm):
    include_archived = forms.BooleanField(
        label=_("Include Archived"), required=False, widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"})
    )

class DateRangeForm(forms.Form):
    start_date = build_date_field(label=_("Start Date"), required=True)
    end_date = build_date_field(label=_("End Date"), required=True)

class StatusFilterForm(forms.Form):
    statuses = forms.MultipleChoiceField(
        label=_("Order Statuses"), required=False, choices=Order.OrderStatus.choices, widget=forms.CheckboxSelectMultiple()
    )

class CustomerFilterForm(forms.Form):
    email = forms.EmailField(label=_("Email"), required=False, widget=forms.EmailInput(attrs=_input_attrs()))

__all__ = [
    "CheckoutAddressForm", "OrderCreateForm", "OrderUpdateForm", "OrderEditForm", "OrderCancelForm",
    "OrderStatusForm", "OrderSearchForm", "OrderFilterForm", "OrderExportForm",
    "OrderImportForm", "OrderMetadataForm", "OrderItemCreateForm", "OrderItemUpdateForm",
    "QuantityUpdateForm", "GiftForm", "CustomizationForm", "PaymentForm",
    "PaymentStatusForm", "RefundRequestForm", "PaymentSearchForm", "ShipmentForm",
    "TrackingForm", "ShipmentStatusForm", "CarrierForm", "ReturnRequestForm",
    "ReturnApprovalForm", "ReturnCompletionForm", "RefundApprovalForm",
    "RefundCompletionForm", "RefundSearchForm", "AttachmentUploadForm",
    "AttachmentReplaceForm", "AttachmentDeleteForm", "OrderAddressForm",
    "OrderNotesForm", "AdminOrderUpdateForm", "GlobalOrderSearchForm",
    "AdvancedOrderFilterForm", "DateRangeForm", "StatusFilterForm", "CustomerFilterForm",
]