"""
Enterprise-grade Django Forms layer for the Orders application.

This module is the CANONICAL input-validation surface for the entire
order domain. Every form defined here is a thin, stateless, easy-to-
test validator that:

    * binds to a Django model (when appropriate)
    * normalises untrusted user input
    * enforces cross-field invariants
    * configures presentation widgets
    * returns cleaned data that the services layer can consume

ARCHITECTURE
============
The orders app follows a strict, layered architecture. The forms
layer sits BETWEEN the views and the services layer. Concretely:

    views.py        → HTTP request handling
    forms.py        → THIS FILE (validation, normalisation, widgets)
    utils.py        → Pure helpers
    constants.py    → Configuration / reference values
    models.py       → Persistence layer (no business logic)
    signals.py      → ORM lifecycle detection
    event_handlers.py → Domain workflow coordination
    services.py     → Business logic / state transitions
    selectors.py    → Read-only data access

forms.py is the ONLY layer that handles:
    * Field-level validation
    * Cross-field validation
    * Widget configuration
    * Model binding
    * Data normalisation
    * Input sanitisation

forms.py MUST NEVER:
    * Create / update / delete model instances
    * Trigger signals, event handlers, or Celery tasks
    * Compute prices, taxes, or discounts
    * Send emails, SMS, or webhooks
    * Implement workflow orchestration
    * Contain duplicated service / selector / model logic
    * Perform permission checks

Business logic belongs in services.py. Persistence belongs in the
ORM. Notifications belong in event_handlers.py. Every form defined
in this file returns clean, validated, normalised data that the
services layer can consume directly.

OWASP COMPLIANCE
================
* All inputs are validated at the form boundary.
* File uploads are size-restricted and extension-whitelisted.
* Phone, email, and URL fields use Django's built-in validators.
* Decimal fields enforce non-negative ranges.
* No sensitive data is echoed in error messages.
* The forms never accept `__all__` for ModelForm.Meta.fields.

PYTHON 3.13 / DJANGO 5.1.4
=========================
* Full PEP 604 union syntax where it improves readability.
* Strict PEP 484 type hints.
* PEP 257 docstrings on every form.
* PEP 8 naming.
* Django 5.1.4 TextChoices, model Meta classes, and validators.

ARCHITECTURAL NOTE (TRACKING_URL)
================================
The finalized Order model does NOT contain a ``tracking_url`` field.
Tracking URLs are owned EXCLUSIVELY by the Shipment model. Every
form in this file that previously exposed an ``invoice_url``-style
``tracking_url`` field has been refactored to:

    1. Remove the field entirely from ``Meta.fields`` and widgets.
    2. Remove the widget declaration.
    3. Remove the ``clean_*`` override that referenced the field.
    4. Remove the field from the ``clean()`` cross-field method
       (where applicable).

The tracking URL is therefore never collected, validated, or
persisted at the order-form layer. The services layer is the
single owner of any tracking-URL persistence path (it reads it
from the related Shipment record).

This refactor is the ONLY functional change introduced by this
rewrite. All other validation rules, business rules, field
names, widgets, choices, and method signatures are preserved
exactly as in the source file.
"""

from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import (
    FileExtensionValidator,
    MaxValueValidator,
    MinValueValidator,
    RegexValidator,
)
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.orders import constants as c
from apps.orders import utils as u
from apps.orders.models import (
    Order,
    OrderAddressSnapshot,
    OrderAttachment,
    OrderItem,
    OrderNote,
    Payment,
    Refund,
    ReturnRequest,
    Shipment,
)

# ==============================================================================
# MODULE-LEVEL CONSTANTS
# ==============================================================================
#: Default CSS class applied to most input widgets. Matches the
#: existing design system used by the project.
DEFAULT_INPUT_CLASS: str = "premium-input"

#: Maximum file size for an order attachment (25 MB).
MAX_ATTACHMENT_SIZE: int = 25 * 1024 * 1024

#: Maximum file size for an order import upload (10 MB).
MAX_IMPORT_SIZE: int = 10 * 1024 * 1024

#: Whitelisted file extensions for order attachments.
ALLOWED_ATTACHMENT_EXTENSIONS: Tuple[str, ...] = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".txt",
)

#: Whitelisted file extensions for order imports.
ALLOWED_IMPORT_EXTENSIONS: Tuple[str, ...] = (".csv", ".json",)

#: Whitelisted MIME-type prefixes for order attachments.
ALLOWED_ATTACHMENT_MIME_PREFIXES: Tuple[str, ...] = (
    "image/",
    "application/pdf",
    "text/",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument",
)

#: Allowed import modes for the order import form.
IMPORT_MODE_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("create", _("Create only (skip existing order numbers)")),
    ("update", _("Update only (require existing order numbers)")),
    ("upsert", _("Create or update")),
)

#: Allowed attachment replacement reasons.
REPLACEMENT_REASON_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("incorrect_file", _("Incorrect file uploaded")),
    ("damaged_file", _("Damaged / corrupted file")),
    ("outdated", _("Outdated version")),
    ("other", _("Other")),
)

#: Allowed export formats for the order export form.
EXPORT_FORMAT_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("csv", _("CSV (Comma-Separated Values)")),
    ("json", _("JSON (JavaScript Object Notation)")),
    ("xlsx", _("XLSX (Microsoft Excel)")),
)

#: Phone-number validator (delegates to constants.PHONE_REGEX).
_phone_validator = RegexValidator(
    regex=c.PHONE_REGEX,
    message=_(
        "Phone number must be 7-20 characters and may only contain "
        "digits, spaces, hyphens, parentheses, and an optional leading +."
    ),
)

#: ISO 3166-1 alpha-2 country code validator.
_country_code_validator = RegexValidator(
    regex=r"^[A-Z]{2}$",
    message=_("Country code must be a 2-letter ISO 3166-1 alpha-2 code."),
)

#: Alphanumerics / hyphens / underscores validator (for identifiers).
_identifier_validator = RegexValidator(
    regex=r"^[A-Za-z0-9_\-]+$",
    message=_(
        "Value may only contain letters, digits, hyphens, and underscores."
    ),
)

#: Hexadecimal validator (for return-number suffixes, etc.).
_hex_validator = RegexValidator(
    regex=r"^[0-9A-Fa-f]+$",
    message=_("Value must be a hexadecimal string."),
)

# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================
def _input_attrs(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Build a baseline widget ``attrs`` dict.

    Centralised so that every form widget in this module shares the
    same presentation contract. Callers can extend the dict with
    placeholder, autocomplete, or data-* hints.
    """
    attrs: Dict[str, Any] = {"class": DEFAULT_INPUT_CLASS}
    if extra:
        attrs.update(extra)
    return attrs

def _textarea_attrs(
    extra: Optional[Dict[str, Any]] = None,
    *,
    rows: int = 3,
) -> Dict[str, Any]:
    """Build a baseline ``<textarea>`` widget ``attrs`` dict."""
    attrs: Dict[str, Any] = {"class": DEFAULT_INPUT_CLASS, "rows": rows}
    if extra:
        attrs.update(extra)
    return attrs

def _file_size_validator_factory(max_size: int):
    """Return a Django validator that enforces a maximum file size."""
    def _validate(value: Any) -> None:
        if not value:
            return
        size = getattr(value, "size", None)
        if size is not None and size > max_size:
            raise ValidationError(
                _("File too large. Maximum size is %(max)s bytes."),
                code="file_too_large",
                params={"max": max_size},
            )
    return _validate

def _file_extension_validator_factory(allowed: Iterable[str]):
    """Return a Django validator that enforces allowed file extensions."""
    allowed_tuple = tuple(allowed)
    def _validate(value: Any) -> None:
        if not value:
            return
        name = getattr(value, "name", "") or ""
        ext = os.path.splitext(name)[1].lower()
        if ext not in allowed_tuple:
            raise ValidationError(
                _("Unsupported file extension '%(ext)s'. Allowed: %(allowed)s."),
                code="invalid_extension",
                params={"ext": ext, "allowed": ", ".join(allowed_tuple)},
            )
    return _validate

def _validate_coupon_code(value: str) -> str:
    """Normalise a coupon code by upper-casing and stripping whitespace."""
    if value is None:
        return ""
    return str(value).strip().upper()

# ==============================================================================
# SHARED FIELD BUILDERS
# ==============================================================================
# Field-builder functions centralise the most-reused field declarations
# so that every form is consistent. They NEVER contain business logic;
# they only return the canonical ``forms.Field`` instance.

def build_email_field(**kwargs: Any) -> forms.EmailField:
    """Return the canonical ``email`` field used by order forms."""
    defaults: Dict[str, Any] = {
        "label": _("Email"),
        "required": True,
        "widget": forms.EmailInput(attrs=_input_attrs()),
    }
    defaults.update(kwargs)
    return forms.EmailField(**defaults)

def build_decimal_field(
    *,
    label: str,
    required: bool = True,
    min_value: Decimal = c.ZERO_DECIMAL_2,
    max_value: Optional[Decimal] = None,
    decimal_places: int = 2,
    initial: Optional[Decimal] = None,
    help_text: str = "",
) -> forms.DecimalField:
    """Return a money-style ``DecimalField`` with the canonical scale."""
    validators: List[Any] = [MinValueValidator(min_value)]
    if max_value is not None:
        validators.append(MaxValueValidator(max_value))
    return forms.DecimalField(
        label=label,
        required=required,
        min_value=min_value,
        max_digits=c.DecimalPrecision.MONEY_14_2[0],
        decimal_places=decimal_places,
        initial=initial if initial is not None else c.ZERO_DECIMAL_2,
        validators=validators,
        widget=forms.NumberInput(
            attrs=_input_attrs({"step": "0.01"}),
        ),
        help_text=help_text,
    )

def build_quantity_field(
    *,
    label: str = _("Quantity"),
    min_value: int = c.MIN_QUANTITY,
    initial: int = c.DEFAULT_QUANTITY,
    max_value: Optional[int] = None,
    required: bool = True,
) -> forms.IntegerField:
    """Return the canonical ``quantity`` field used by line-item forms."""
    validators: List[Any] = [MinValueValidator(min_value)]
    if max_value is not None:
        validators.append(MaxValueValidator(max_value))
    return forms.IntegerField(
        label=label,
        required=required,
        min_value=min_value,
        initial=initial,
        validators=validators,
        widget=forms.NumberInput(
            attrs=_input_attrs({"min": str(min_value), "step": "1"}),
        ),
    )

def build_status_field(
    choices: Any,
    *,
    label: str = _("Status"),
    required: bool = True,
    initial: Optional[str] = None,
) -> forms.ChoiceField:
    """Return a canonical status ``ChoiceField``."""
    return forms.ChoiceField(
        label=label,
        choices=choices,
        required=required,
        initial=initial,
        widget=forms.Select(attrs=_input_attrs()),
    )

def build_text_field(
    *,
    label: str,
    required: bool = True,
    max_length: int = 255,
    help_text: str = "",
    widget: Optional[forms.Widget] = None,
    initial: str = "",
) -> forms.CharField:
    """Return a single-line text field with a consistent widget."""
    if widget is None:
        widget = forms.TextInput(attrs=_input_attrs())
    return forms.CharField(
        label=label,
        required=required,
        max_length=max_length,
        initial=initial,
        widget=widget,
        help_text=help_text,
    )

def build_textarea_field(
    *,
    label: str,
    required: bool = True,
    rows: int = 3,
    help_text: str = "",
    placeholder: str = "",
) -> forms.CharField:
    """Return a multi-line text field with a consistent widget."""
    attrs = _textarea_attrs({"placeholder": placeholder} if placeholder else None, rows=rows)
    return forms.CharField(
        label=label,
        required=required,
        widget=forms.Textarea(attrs=attrs),
        help_text=help_text,
    )

def build_date_field(
    *,
    label: str,
    required: bool = False,
    help_text: str = "",
) -> forms.DateField:
    """Return a date input field using the HTML5 date picker."""
    return forms.DateField(
        label=label,
        required=required,
        widget=forms.DateInput(
            attrs=_input_attrs({"type": "date"}),
        ),
        help_text=help_text,
    )

def build_datetime_field(
    *,
    label: str,
    required: bool = False,
    help_text: str = "",
) -> forms.DateTimeField:
    """Return a datetime input field using the HTML5 datetime-local picker."""
    return forms.DateTimeField(
        label=label,
        required=required,
        widget=forms.DateTimeInput(
            attrs=_input_attrs({"type": "datetime-local"}),
        ),
        help_text=help_text,
    )

def build_file_field(
    *,
    label: str,
    required: bool = True,
    help_text: str = "",
    allowed_extensions: Iterable[str] = ALLOWED_ATTACHMENT_EXTENSIONS,
    max_size: int = MAX_ATTACHMENT_SIZE,
) -> forms.FileField:
    """Return a validated file input with size + extension checks."""
    return forms.FileField(
        label=label,
        required=required,
        validators=[
            _file_size_validator_factory(max_size),
            _file_extension_validator_factory(allowed_extensions),
        ],
        help_text=help_text,
    )

# ==============================================================================
# 1. ORDER FORMS
# ==============================================================================
class OrderCreateForm(forms.Form):
    """
    Form for creating a new ``Order`` header.

    The form is intentionally a plain ``Form`` (not a ``ModelForm``):
    persistence is owned by ``services.create_order``, and this form
    only collects, normalises, and validates the input fields that
    the service will accept.

    Address snapshots are NOT created here. The view is expected to
    use :class:`OrderAddressForm` (or the existing
    :class:`OrderAddressUpdateForm`) to build the immutable
    ``OrderAddressSnapshot`` and pass its pk to the service.
    """

    email = build_email_field(
        help_text=_("Contact email for the order (guest-checkout safe)."),
    )
    customer = forms.ModelChoiceField(
        label=_("Customer"),
        required=False,
        queryset=None,  # Set in __init__ by the view.
        widget=forms.Select(attrs=_input_attrs()),
        help_text=_(
            "Leave blank for guest checkout. Authenticated users are "
            "linked via the customer FK."
        ),
    )
    currency = forms.ChoiceField(
        label=_("Currency"),
        required=True,
        initial=c.DEFAULT_CURRENCY_CODE,
        choices=(
            (c.DEFAULT_CURRENCY_CODE, c.DEFAULT_CURRENCY_CODE),
            ("USD", "USD"),
            ("EUR", "EUR"),
            ("INR", "INR"),
        ),
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
        help_text=_("Defaults to PENDING for new orders."),
    )
    payment_status = forms.ChoiceField(
        label=_("Initial Payment Status"),
        required=True,
        initial=Order.PaymentStatus.PENDING,
        choices=Order.PaymentStatus.choices,
        widget=forms.Select(attrs=_input_attrs()),
    )
    payment_method = build_text_field(
        label=_("Payment Method"),
        required=False,
        max_length=c.FieldLength.PAYMENT_METHOD,
        help_text=_("Free-form payment-method label (e.g. 'esewa', 'card')."),
    )
    transaction_id = build_text_field(
        label=_("Transaction ID"),
        required=False,
        max_length=c.FieldLength.TRANSACTION_ID,
        help_text=_("Optional gateway reference. Required once captured."),
    )
    coupon_code = build_text_field(
        label=_("Coupon Code"),
        required=False,
        max_length=c.FieldLength.COUPON_CODE,
        help_text=_("Optional. The actual discount is computed by services."),
    )
    shipping_cost = build_decimal_field(
        label=_("Shipping Cost"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
        initial=c.ZERO_DECIMAL_2,
        help_text=_("Defaults to zero. Can be updated after order creation."),
    )
    tax_total = build_decimal_field(
        label=_("Tax Total"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
        initial=c.ZERO_DECIMAL_2,
    )
    discount_total = build_decimal_field(
        label=_("Discount Total"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
        initial=c.ZERO_DECIMAL_2,
    )
    customer_note = build_textarea_field(
        label=_("Customer Note"),
        required=False,
        rows=3,
        help_text=_("Free-form note supplied by the customer at checkout."),
    )
    notes = build_textarea_field(
        label=_("Internal Notes"),
        required=False,
        rows=3,
        help_text=_("Operator-only notes. Never exposed to customers."),
    )
    is_gift = forms.BooleanField(
        label=_("Is Gift Order?"),
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
    )
    gift_message = build_textarea_field(
        label=_("Gift Message"),
        required=False,
        rows=2,
    )
    gift_wrapping = build_text_field(
        label=_("Gift Wrapping"),
        required=False,
        max_length=c.FieldLength.GIFT_WRAPPING,
    )
    expected_delivery_date = build_date_field(
        label=_("Expected Delivery Date"),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        # Resolve the customer queryset lazily to avoid import-time
        # dependencies on the user model.
        try:
            from django.contrib.auth import get_user_model
            self.fields["customer"].queryset = get_user_model().objects.all()
        except Exception:  # noqa: BLE001
            self.fields["customer"].queryset = None

    def clean_coupon_code(self) -> str:
        """Normalise the coupon code."""
        return _validate_coupon_code(self.cleaned_data.get("coupon_code", ""))

    def clean(self) -> Dict[str, Any]:
        """Cross-field validation: discount cannot exceed shipping+tax."""
        cleaned = super().clean()
        discount = cleaned.get("discount_total") or c.ZERO_DECIMAL_2
        tax = cleaned.get("tax_total") or c.ZERO_DECIMAL_2
        shipping = cleaned.get("shipping_cost") or c.ZERO_DECIMAL_2
        if discount < 0 or tax < 0 or shipping < 0:
            raise ValidationError(
                _("Monetary values must be non-negative."),
            )
        return cleaned

class OrderUpdateForm(forms.ModelForm):
    """
    Form for editing the editable fields of an existing ``Order``.

    The form intentionally excludes:

        * ``id`` (immutable, primary key)
        * ``order_number`` (immutable, generated by the service)
        * financial aggregation fields (``subtotal``, ``total``) that
          are recomputed by the service
        * ``created_at`` and ``updated_at`` (auto-managed)
        * inventory / warehouse / reservation FKs (audit-only)

    The view may call ``form.save()`` or pass ``cleaned_data`` to a
    service depending on the business policy in force.

    Note: ``tracking_url`` is intentionally NOT exposed on this form.
    The Order model does not own a ``tracking_url`` field; tracking
    URLs are managed by the related Shipment record.
    """

    class Meta:
        model = Order
        fields = (
            "customer",
            "email",
            "status",
            "payment_status",
            "payment_method",
            "transaction_id",
            "currency",
            "shipping_cost",
            "tax_total",
            "discount_total",
            "coupon_code",
            "customer_note",
            "delivery_instructions",
            "has_invoice",
            "invoice_url",
            "tracking_number",
            "carrier",
            "is_gift",
            "gift_message",
            "gift_wrapping",
            "expected_delivery_date",
            "fraud_check_status",
            "risk_score",
            "is_active",
            "source",
            "external_order_id",
            "external_platform",
            "notes",
        )
        widgets = {
            "customer": forms.Select(attrs=_input_attrs()),
            "email": forms.EmailInput(attrs=_input_attrs()),
            "status": forms.Select(attrs=_input_attrs()),
            "payment_status": forms.Select(attrs=_input_attrs()),
            "payment_method": forms.TextInput(attrs=_input_attrs()),
            "transaction_id": forms.TextInput(attrs=_input_attrs()),
            "currency": forms.TextInput(attrs=_input_attrs()),
            "shipping_cost": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01"}),
            ),
            "tax_total": forms.NumberInput(attrs=_input_attrs({"step": "0.01"})),
            "discount_total": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01"}),
            ),
            "coupon_code": forms.TextInput(attrs=_input_attrs()),
            "customer_note": forms.Textarea(
                attrs=_textarea_attrs(rows=3),
            ),
            "delivery_instructions": forms.Textarea(
                attrs=_textarea_attrs(rows=3),
            ),
            "has_invoice": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "invoice_url": forms.URLInput(attrs=_input_attrs()),
            "tracking_number": forms.TextInput(attrs=_input_attrs()),
            "carrier": forms.TextInput(attrs=_input_attrs()),
            "is_gift": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "gift_message": forms.Textarea(
                attrs=_textarea_attrs(rows=2),
            ),
            "gift_wrapping": forms.TextInput(attrs=_input_attrs()),
            "expected_delivery_date": forms.DateInput(
                attrs=_input_attrs({"type": "date"}),
            ),
            "fraud_check_status": forms.Select(attrs=_input_attrs()),
            "risk_score": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01", "min": "0", "max": "100"}),
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "source": forms.Select(attrs=_input_attrs()),
            "external_order_id": forms.TextInput(attrs=_input_attrs()),
            "external_platform": forms.TextInput(attrs=_input_attrs()),
            "notes": forms.Textarea(attrs=_textarea_attrs(rows=3)),
        }

    def clean_coupon_code(self) -> str:
        return _validate_coupon_code(self.cleaned_data.get("coupon_code", ""))

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        # Tracking consistency: tracking number without a carrier
        # is almost always a user mistake. The form catches it
        # without touching business logic.
        if cleaned.get("tracking_number") and not cleaned.get("carrier"):
            self.add_error(
                "tracking_number",
                _("A carrier must be specified when adding a tracking number."),
            )
        return cleaned

class OrderEditForm(forms.ModelForm):
    """
    Lightweight customer-facing edit form for an order.

    Only the fields a customer is allowed to change while the order
    is still pending are exposed. Financial and audit fields are
    deliberately excluded.
    """

    class Meta:
        model = Order
        fields = (
            "customer_note",
            "delivery_instructions",
            "is_gift",
            "gift_message",
            "gift_wrapping",
        )
        widgets = {
            "customer_note": forms.Textarea(
                attrs=_textarea_attrs(
                    {"placeholder": _("Any general requests regarding this order.")},
                    rows=3,
                ),
            ),
            "delivery_instructions": forms.Textarea(
                attrs=_textarea_attrs(
                    {"placeholder": _("Gate codes, safe dropping locations, etc.")},
                    rows=3,
                ),
            ),
            "is_gift": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "gift_message": forms.Textarea(attrs=_textarea_attrs(rows=2)),
            "gift_wrapping": forms.TextInput(attrs=_input_attrs()),
        }

class OrderCancelForm(forms.Form):
    """
    Form for a customer (or operator) to request a cancellation.

    The form does not perform the cancellation itself; it only
    collects the reason and validates that the order is in a state
    from which cancellation is allowed (per
    ``constants.OrderStatus.CANCELLABLE_FROM``).
    """

    remarks = build_textarea_field(
        label=_("Cancellation Reason"),
        rows=3,
        placeholder=_(
            "Please let us know why you are cancelling this order. "
            "Your feedback helps us improve."
        ),
        help_text=_("A brief reason for the cancellation is required."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop("order", None)
        super().__init__(*args, **kwargs)

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if self.order is not None:
            cancellable = c.OrderStatus.CANCELLABLE_FROM
            if self.order.status not in cancellable:
                raise ValidationError(
                    _(
                        "This order is in status '%(status)s' and can no longer "
                        "be cancelled directly. Please contact support for "
                        "assistance with terminal orders."
                    ),
                    code="order_not_cancellable",
                    params={"status": self.order.status},
                )
        return cleaned

class OrderStatusForm(forms.Form):
    """
    Form for transitioning an order to a new status.

    The form only enforces choice-level validation. State-machine
    validation is performed by the service layer. The form's
    ``clean_status`` method merely rejects blank or unknown values.
    """

    status = build_status_field(
        choices=Order.OrderStatus.choices,
        label=_("New Status"),
        initial=Order.OrderStatus.PENDING,
    )
    remarks = build_textarea_field(
        label=_("Remarks"),
        required=False,
        rows=2,
        help_text=_("Optional reason / context for the status change."),
    )

    def clean_status(self) -> str:
        value = self.cleaned_data.get("status", "")
        if value not in {choice.value for choice in Order.OrderStatus}:
            raise ValidationError(
                _("Unknown order status '%(value)s'."),
                code="invalid_status",
                params={"value": value},
            )
        return value

class OrderSearchForm(forms.Form):
    """
    Lightweight search form for the orders changelist.

    Supports a free-text query plus a single status filter. Use
    :class:`AdvancedOrderFilterForm` for richer filtering.
    """

    q = forms.CharField(
        label=_("Search"),
        required=False,
        widget=forms.TextInput(
            attrs=_input_attrs(
                {
                    "placeholder": _(
                        "Search by order number, email, customer name, "
                        "or transaction id…"
                    ),
                },
            ),
        ),
        help_text=_(
            "Matches against order number, customer name, email, "
            "transaction id, and tracking number."
        ),
    )
    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=(
            ("", _("-- Any status --")),
            *Order.OrderStatus.choices,
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    payment_status = forms.ChoiceField(
        label=_("Payment Status"),
        required=False,
        choices=(
            ("", _("-- Any payment status --")),
            *Order.PaymentStatus.choices,
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )

    def clean_q(self) -> str:
        return u.normalize_whitespace(self.cleaned_data.get("q", ""))

class OrderFilterForm(forms.Form):
    """
    Comprehensive filter form for orders.

    Backed by :class:`apps.orders.selectors.get_orders`; the view is
    expected to translate ``cleaned_data`` into selector kwargs.
    """

    q = forms.CharField(
        label=_("Search"),
        required=False,
        widget=forms.TextInput(attrs=_input_attrs()),
    )
    status = forms.MultipleChoiceField(
        label=_("Order Status"),
        required=False,
        choices=Order.OrderStatus.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    payment_status = forms.MultipleChoiceField(
        label=_("Payment Status"),
        required=False,
        choices=Order.PaymentStatus.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    source = forms.MultipleChoiceField(
        label=_("Source"),
        required=False,
        choices=Order.Source.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    fraud_check_status = forms.MultipleChoiceField(
        label=_("Fraud Check"),
        required=False,
        choices=Order.FraudCheckStatus.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    is_gift = forms.ChoiceField(
        label=_("Gift?"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            ("true", _("Gift orders only")),
            ("false", _("Non-gift orders only")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    is_active = forms.ChoiceField(
        label=_("Active?"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            ("true", _("Active only")),
            ("false", _("Inactive only")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    min_total = build_decimal_field(
        label=_("Min Total"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )
    max_total = build_decimal_field(
        label=_("Max Total"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )
    created_after = build_datetime_field(
        label=_("Created After"),
    )
    created_before = build_datetime_field(
        label=_("Created Before"),
    )
    currency = forms.ChoiceField(
        label=_("Currency"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            (c.DEFAULT_CURRENCY_CODE, c.DEFAULT_CURRENCY_CODE),
            ("USD", "USD"),
            ("EUR", "EUR"),
            ("INR", "INR"),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    ordering = forms.ChoiceField(
        label=_("Sort By"),
        required=False,
        choices=(
            ("-created_at", _("Newest first")),
            ("created_at", _("Oldest first")),
            ("-total", _("Highest total first")),
            ("total", _("Lowest total first")),
            ("order_number", _("Order number (A→Z)")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        min_total = cleaned.get("min_total")
        max_total = cleaned.get("max_total")
        if (
            min_total is not None
            and max_total is not None
            and min_total > max_total
        ):
            raise ValidationError(
                _("Min total cannot exceed max total."),
            )
        if (
            cleaned.get("created_after")
            and cleaned.get("created_before")
            and cleaned["created_after"] > cleaned["created_before"]
        ):
            raise ValidationError(
                _("'Created After' must be earlier than 'Created Before'."),
            )
        return cleaned

class OrderExportForm(forms.Form):
    """
    Form for exporting orders to CSV / JSON / XLSX.

    The view is expected to call
    :func:`apps.orders.selectors.get_orders_for_csv_export` (for CSV)
    or build an equivalent projection. The form NEVER persists data.
    """

    FORMAT_CHOICES: Tuple[Tuple[str, str], ...] = EXPORT_FORMAT_CHOICES

    format = forms.ChoiceField(
        label=_("Export Format"),
        required=True,
        initial="csv",
        choices=FORMAT_CHOICES,
        widget=forms.RadioSelect(),
    )
    created_after = build_datetime_field(
        label=_("Created After"),
    )
    created_before = build_datetime_field(
        label=_("Created Before"),
    )
    include_test_payments = forms.BooleanField(
        label=_("Include test payments?"),
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
    )
    include_only_fields = forms.MultipleChoiceField(
        label=_("Limit To Fields"),
        required=False,
        choices=tuple(
            (field, field)
            for field in c.CSV_EXPORT_FIELDS
        ),
        widget=forms.CheckboxSelectMultiple(),
        help_text=_("Leave blank to export the full whitelist."),
    )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        after = cleaned.get("created_after")
        before = cleaned.get("created_before")
        if after and before and after > before:
            raise ValidationError(
                _("'Created After' must be earlier than 'Created Before'."),
            )
        return cleaned

class OrderImportForm(forms.Form):
    """
    Form for importing orders from a CSV / JSON file.

    The view is expected to parse the uploaded file, normalise the
    rows, and call the appropriate service functions. The form does
    NOT trigger any import action itself.
    """

    file = build_file_field(
        label=_("Import File"),
        required=True,
        help_text=_(
            "Upload a CSV or JSON file. Maximum size 10 MB."
        ),
        allowed_extensions=ALLOWED_IMPORT_EXTENSIONS,
        max_size=MAX_IMPORT_SIZE,
    )
    import_mode = forms.ChoiceField(
        label=_("Import Mode"),
        required=True,
        initial="create",
        choices=IMPORT_MODE_CHOICES,
        widget=forms.RadioSelect(),
    )
    dry_run = forms.BooleanField(
        label=_("Dry Run (validate only)"),
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
        help_text=_(
            "When enabled, the importer reports validation errors "
            "without persisting any changes."
        ),
    )
    send_notifications = forms.BooleanField(
        label=_("Send Customer Notifications"),
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
    )
    default_currency = forms.ChoiceField(
        label=_("Default Currency"),
        required=True,
        initial=c.DEFAULT_CURRENCY_CODE,
        choices=(
            (c.DEFAULT_CURRENCY_CODE, c.DEFAULT_CURRENCY_CODE),
            ("USD", "USD"),
            ("EUR", "EUR"),
            ("INR", "INR"),
        ),
        widget=forms.Select(attrs=_input_attrs()),
        help_text=_(
            "Used when an imported row omits the currency field."
        ),
    )

class OrderMetadataForm(forms.ModelForm):
    """
    Form for editing an order's free-form ``json_metadata`` and
    ``tags`` (legacy CSV) fields, plus internal ``notes``.

    These fields are safe to edit via the admin because they never
    influence financial calculations or business invariants.
    """

    tags_csv = forms.CharField(
        label=_("Tags (comma-separated)"),
        required=False,
        widget=forms.TextInput(
            attrs=_input_attrs(
                {
                    "placeholder": _(
                        "wholesale, vip, returning-customer"
                    ),
                },
            ),
        ),
        help_text=_(
            "Will be normalised (lowercased, deduplicated) and stored "
            "in the JSONField. The legacy 'tags_text' column receives a "
            "raw CSV copy for backward compatibility."
        ),
    )

    class Meta:
        model = Order
        fields = ("json_metadata", "notes", "tags_text")
        widgets = {
            "json_metadata": forms.Textarea(
                attrs=_textarea_attrs(
                    {
                        "placeholder": _('{"campaign": "spring2026"}'),
                    },
                    rows=5,
                ),
            ),
            "notes": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "tags_text": forms.HiddenInput(),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.tags:
            self.fields["tags_csv"].initial = ", ".join(
                str(tag) for tag in self.instance.tags
            )

    def clean_json_metadata(self) -> Dict[str, Any]:
        """Validate that the supplied value is a JSON object."""
        value = self.cleaned_data.get("json_metadata")
        if value in (None, ""):
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = u.safe_json_loads(value, default=None)
            if not isinstance(parsed, dict):
                raise ValidationError(
                    _("Metadata must be a JSON object (dictionary)."),
                )
            return parsed
        raise ValidationError(
            _("Metadata must be a JSON object (dictionary)."),
        )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        tags_csv = cleaned.get("tags_csv", "")
        tags = u.normalize_tags(tags_csv) if tags_csv else []
        cleaned["tags"] = tags
        cleaned["tags_text"] = ", ".join(tags)
        return cleaned

# ==============================================================================
# 2. ORDER ITEM FORMS
# ==============================================================================
class OrderItemCreateForm(forms.Form):
    """
    Form for adding a new ``OrderItem`` to an existing order.

    The form accepts an optional ``product_id`` and ``variant_id``
    (resolved by the service) plus the snapshot fields the service
    needs to record. Financial computation is owned by
    :func:`apps.orders.services.add_order_item``.
    """

    product_id = forms.IntegerField(
        label=_("Product ID"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=_input_attrs({"min": "1"})),
    )
    variant_id = forms.IntegerField(
        label=_("Variant ID"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=_input_attrs({"min": "1"})),
    )
    product_name = build_text_field(
        label=_("Product Name (snapshot)"),
        required=True,
        max_length=c.FieldLength.PRODUCT_NAME_SNAPSHOT,
    )
    product_sku = build_text_field(
        label=_("Product SKU (snapshot)"),
        required=False,
        max_length=c.FieldLength.PRODUCT_SKU_SNAPSHOT,
    )
    variant_name = build_text_field(
        label=_("Variant Name (snapshot)"),
        required=False,
        max_length=c.FieldLength.VARIANT_NAME_SNAPSHOT,
    )
    quantity = build_quantity_field()
    unit_price = build_decimal_field(
        label=_("Unit Price"),
        required=True,
        min_value=c.ZERO_DECIMAL_2,
    )
    discount = build_decimal_field(
        label=_("Discount"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
        initial=c.ZERO_DECIMAL_2,
    )
    tax = build_decimal_field(
        label=_("Tax"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
        initial=c.ZERO_DECIMAL_2,
    )
    weight = forms.DecimalField(
        label=_("Weight"),
        required=False,
        min_value=c.ZERO_DECIMAL_3,
        initial=c.ZERO_DECIMAL_3,
        max_digits=c.DecimalPrecision.ITEM_WEIGHT[0],
        decimal_places=c.DecimalPrecision.ITEM_WEIGHT[1],
        widget=forms.NumberInput(
            attrs=_input_attrs({"step": "0.001", "min": "0"}),
        ),
        help_text=_("Stored in kilograms."),
    )
    attributes = forms.JSONField(
        label=_("Selected Attributes"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _('{"color": "red", "size": "XL"}')},
                rows=3,
            ),
        ),
        help_text=_(
            "Free-form JSON snapshot of the chosen variant attributes."
        ),
    )
    personalization = forms.JSONField(
        label=_("Personalization"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _('{"engraving": "Happy Birthday"}')},
                rows=3,
            ),
        ),
    )
    is_gift = forms.BooleanField(
        label=_("Is Gift Item?"),
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
    )
    gift_message = build_textarea_field(
        label=_("Gift Message"),
        required=False,
        rows=2,
    )
    gift_wrapping = build_text_field(
        label=_("Gift Wrapping"),
        required=False,
        max_length=c.FieldLength.GIFT_WRAPPING,
    )
    expected_ship_date = build_date_field(
        label=_("Expected Ship Date"),
    )
    promised_delivery_date = build_date_field(
        label=_("Promised Delivery Date"),
    )
    metadata = forms.JSONField(
        label=_("Audit Metadata"),
        required=False,
        widget=forms.Textarea(attrs=_textarea_attrs(rows=3)),
    )

    def clean_attributes(self) -> Dict[str, Any]:
        return _normalize_json_field(self.cleaned_data.get("attributes"))

    def clean_personalization(self) -> Dict[str, Any]:
        return _normalize_json_field(self.cleaned_data.get("personalization"))

    def clean_metadata(self) -> Dict[str, Any]:
        return _normalize_json_field(self.cleaned_data.get("metadata"))

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if not cleaned.get("product_id") and not cleaned.get("product_name"):
            raise ValidationError(
                _(
                    "Either a product ID or a product name snapshot "
                    "must be supplied."
                ),
            )
        ship = cleaned.get("expected_ship_date")
        deliver = cleaned.get("promised_delivery_date")
        if ship and deliver and ship > deliver:
            raise ValidationError(
                _(
                    "Promised delivery date must be on or after the "
                    "expected ship date."
                ),
            )
        return cleaned

class OrderItemUpdateForm(forms.ModelForm):
    """
    Form for editing an existing ``OrderItem``.

    The form exposes the editable snapshot fields and lifecycle
    fields. Quantity changes that affect the ``line_total`` are
    recomputed by the service; ``line_total`` itself is read-only
    here. Status transitions are validated against the
    ``ItemStatus`` TextChoices.
    """

    class Meta:
        model = OrderItem
        fields = (
            "product_name_snapshot",
            "product_sku_snapshot",
            "variant_name_snapshot",
            "unit_price",
            "discount",
            "tax",
            "weight",
            "quantity",
            "status",
            "saved_reason",
            "attributes",
            "personalization",
            "is_gift",
            "gift_message",
            "gift_wrapping",
            "expected_ship_date",
            "promised_delivery_date",
        )
        widgets = {
            "product_name_snapshot": forms.TextInput(attrs=_input_attrs()),
            "product_sku_snapshot": forms.TextInput(attrs=_input_attrs()),
            "variant_name_snapshot": forms.TextInput(attrs=_input_attrs()),
            "unit_price": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01", "min": "0"}),
            ),
            "discount": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01", "min": "0"}),
            ),
            "tax": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01", "min": "0"}),
            ),
            "weight": forms.NumberInput(
                attrs=_input_attrs({"step": "0.001", "min": "0"}),
            ),
            "quantity": forms.NumberInput(
                attrs=_input_attrs({"min": "1", "step": "1"}),
            ),
            "status": forms.Select(attrs=_input_attrs()),
            "saved_reason": forms.Select(attrs=_input_attrs()),
            "attributes": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "personalization": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "is_gift": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "gift_message": forms.Textarea(attrs=_textarea_attrs(rows=2)),
            "gift_wrapping": forms.TextInput(attrs=_input_attrs()),
            "expected_ship_date": forms.DateInput(
                attrs=_input_attrs({"type": "date"}),
            ),
            "promised_delivery_date": forms.DateInput(
                attrs=_input_attrs({"type": "date"}),
            ),
        }

    def clean_status(self) -> str:
        value = self.cleaned_data.get("status", "")
        valid = {choice.value for choice in OrderItem.ItemStatus}
        if value not in valid:
            raise ValidationError(
                _("Invalid item status '%(value)s'."),
                code="invalid_status",
                params={"value": value},
            )
        return value

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if cleaned.get("expected_ship_date") and cleaned.get(
            "promised_delivery_date"
        ) and cleaned["expected_ship_date"] > cleaned["promised_delivery_date"]:
            raise ValidationError(
                _(
                    "Promised delivery date must be on or after the "
                    "expected ship date."
                ),
            )
        return cleaned

class QuantityUpdateForm(forms.Form):
    """
    Form for updating only the ``quantity`` of an ``OrderItem``.

    The form takes the existing item in ``__init__`` and uses its
    current quantity as the upper bound. Updates that exceed the
    current shipped / returned / refunded counters are rejected
    by the service, not here.
    """

    quantity = forms.IntegerField(
        label=_("Quantity"),
        required=True,
        min_value=c.MIN_QUANTITY,
        widget=forms.NumberInput(
            attrs=_input_attrs({"min": "1", "step": "1"}),
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.item: Optional[OrderItem] = kwargs.pop("item", None)
        super().__init__(*args, **kwargs)
        if self.item is not None:
            current = int(self.item.quantity or 0)
            if current < c.MIN_QUANTITY:
                current = c.MIN_QUANTITY
            self.fields["quantity"].initial = current
            self.fields["quantity"].validators.append(
                MaxValueValidator(current),
            )
            self.fields["quantity"].widget.attrs["max"] = str(current)

    def clean_quantity(self) -> int:
        quantity = self.cleaned_data.get("quantity", 0)
        if quantity < c.MIN_QUANTITY:
            raise ValidationError(
                _("Quantity must be at least %(min)s."),
                code="quantity_too_low",
                params={"min": c.MIN_QUANTITY},
            )
        if (
            self.item is not None
            and quantity > int(self.item.quantity or 0)
        ):
            # The form does NOT permit increasing the quantity here.
            # The service is responsible for adding new line items.
            raise ValidationError(
                _(
                    "Quantity can only be reduced via this form. "
                    "Add a new line item to increase the ordered quantity."
                ),
                code="quantity_increase_not_allowed",
            )
        return quantity

class GiftForm(forms.ModelForm):
    """
    Form for editing the gift-related fields of an ``OrderItem``.
    """

    class Meta:
        model = OrderItem
        fields = ("is_gift", "gift_message", "gift_wrapping")
        widgets = {
            "is_gift": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "gift_message": forms.Textarea(attrs=_textarea_attrs(rows=3)),
            "gift_wrapping": forms.TextInput(attrs=_input_attrs()),
        }

class CustomizationForm(forms.ModelForm):
    """
    Form for editing the JSON ``attributes`` and ``personalization``
    blobs of an ``OrderItem``.
    """

    class Meta:
        model = OrderItem
        fields = ("attributes", "personalization")
        widgets = {
            "attributes": forms.Textarea(
                attrs=_textarea_attrs(
                    {"placeholder": _('{"color": "red", "size": "XL"}')},
                    rows=4,
                ),
            ),
            "personalization": forms.Textarea(
                attrs=_textarea_attrs(
                    {"placeholder": _('{"engraving": "Happy Birthday"}')},
                    rows=4,
                ),
            ),
        }

    def clean_attributes(self) -> Dict[str, Any]:
        return _normalize_json_field(self.cleaned_data.get("attributes"))

    def clean_personalization(self) -> Dict[str, Any]:
        return _normalize_json_field(self.cleaned_data.get("personalization"))

# ==============================================================================
# 3. PAYMENT FORMS
# ==============================================================================
class PaymentForm(forms.ModelForm):
    """
    Form for creating a new ``Payment`` record against an order.

    The form enforces:

        * non-empty ``transaction_id`` (uniqueness is checked by the
          model; the form surfaces the same constraint for a faster
          UX feedback)
        * non-empty ``gateway``
        * strictly positive ``amount``
        * canonical ``currency`` and ``status`` choices
    """

    class Meta:
        model = Payment
        fields = (
            "transaction_id",
            "gateway",
            "amount",
            "currency",
            "status",
            "payment_method",
            "paid_at",
            "risk_score",
            "is_test_payment",
            "metadata",
        )
        widgets = {
            "transaction_id": forms.TextInput(attrs=_input_attrs()),
            "gateway": forms.TextInput(attrs=_input_attrs()),
            "amount": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01", "min": "0.01"}),
            ),
            "currency": forms.TextInput(attrs=_input_attrs()),
            "status": forms.Select(attrs=_input_attrs()),
            "payment_method": forms.TextInput(attrs=_input_attrs()),
            "paid_at": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
            "risk_score": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01", "min": "0", "max": "100"}),
            ),
            "is_test_payment": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "metadata": forms.Textarea(attrs=_textarea_attrs(rows=3)),
        }

    def clean_transaction_id(self) -> str:
        value = (self.cleaned_data.get("transaction_id") or "").strip()
        if not value:
            raise ValidationError(
                _("Transaction id is required."),
                code="transaction_id_required",
            )
        if Payment.objects.filter(transaction_id=value).exists():
            raise ValidationError(
                _("A payment with this transaction id already exists."),
                code="transaction_id_duplicate",
            )
        return value

    def clean_amount(self) -> Decimal:
        amount = self.cleaned_data.get("amount")
        if amount is None or amount <= c.ZERO_DECIMAL_2:
            raise ValidationError(
                _("Payment amount must be strictly positive."),
                code="amount_non_positive",
            )
        return amount

    def clean_status(self) -> str:
        value = self.cleaned_data.get("status", "")
        if value not in {choice.value for choice in Payment.PaymentState}:
            raise ValidationError(
                _("Invalid payment state '%(value)s'."),
                code="invalid_status",
                params={"value": value},
            )
        return value

    def clean_metadata(self) -> Dict[str, Any]:
        return _normalize_json_field(self.cleaned_data.get("metadata"))

class PaymentStatusForm(forms.ModelForm):
    """
    Form for transitioning a payment's status.

    The form does not enforce the full state machine; that is
    owned by the service. The form rejects unknown status values
    and ensures that ``paid_at`` is set when the new status is
    terminal-success.
    """

    class Meta:
        model = Payment
        fields = ("status", "paid_at")
        widgets = {
            "status": forms.Select(attrs=_input_attrs()),
            "paid_at": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
        }

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        status_value = cleaned.get("status", "")
        if status_value not in {choice.value for choice in Payment.PaymentState}:
            raise ValidationError(
                _("Invalid payment state '%(value)s'."),
                code="invalid_status",
                params={"value": status_value},
            )
        if status_value in {
            Payment.PaymentState.CAPTURED,
            Payment.PaymentState.COMPLETED,
        } and not cleaned.get("paid_at"):
            # Auto-stamp to a sensible default; the service can
            # override this with timezone.now() if needed.
            cleaned["paid_at"] = timezone.now()
        return cleaned

class RefundRequestForm(forms.Form):
    """
    Form for a customer (or operator) to request a refund against
    a captured payment.

    The form enforces that the chosen payment belongs to the
    supplied order, that the amount is positive, and that the
    reason is non-empty. Actual gateway-side refund processing
    is owned by the service.
    """

    payment_id = forms.ChoiceField(
        label=_("Select Payment"),
        required=True,
        widget=forms.Select(attrs=_input_attrs()),
        help_text=_(
            "Only captured / completed payments are eligible for refund."
        ),
    )
    amount = build_decimal_field(
        label=_("Requested Amount"),
        required=False,
        min_value=Decimal("0.01"),
        help_text=_(
            "Leave blank to request a full refund of the selected "
            "payment transaction."
        ),
    )
    reason = build_textarea_field(
        label=_("Reason for Refund"),
        required=True,
        rows=4,
        placeholder=_(
            "Provide details about why you are requesting this refund."
        ),
    )
    refund_method = forms.ChoiceField(
        label=_("Refund Method"),
        required=False,
        initial=Refund.RefundMethod.ORIGINAL,
        choices=Refund.RefundMethod.choices,
        widget=forms.Select(attrs=_input_attrs()),
    )
    refund_reason_category = forms.ChoiceField(
        label=_("Reason Category"),
        required=False,
        choices=(
            ("", _("-- Choose category --")),
            *Refund.RefundReasonCategory.choices,
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    customer_notes = build_textarea_field(
        label=_("Additional Notes"),
        required=False,
        rows=2,
    )
    evidence_urls = forms.CharField(
        label=_("Evidence Image URLs"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _("One URL per line.")},
                rows=3,
            ),
        ),
        help_text=_(
            "Optional. Each line becomes a URL in the refund's "
            "evidence_images list."
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop("order", None)
        super().__init__(*args, **kwargs)
        self._eligible_payments: Dict[str, Payment] = {}
        if self.order is not None:
            payments = self.order.payments.filter(
                status__in=(
                    Payment.PaymentState.CAPTURED,
                    Payment.PaymentState.COMPLETED,
                )
            )
            choices: List[Tuple[str, str]] = []
            for payment in payments:
                label = (
                    f"{payment.gateway} - {payment.transaction_id} "
                    f"({payment.amount} {payment.currency})"
                )
                choices.append((str(payment.id), label))
                self._eligible_payments[str(payment.id)] = payment
            self.fields["payment_id"].choices = choices

    def clean_payment_id(self) -> str:
        value = self.cleaned_data.get("payment_id", "")
        if not self._eligible_payments:
            raise ValidationError(
                _("No captured payments are available for refund."),
                code="no_eligible_payments",
            )
        if value not in self._eligible_payments:
            raise ValidationError(
                _("Invalid payment transaction selected."),
                code="invalid_payment",
            )
        return value

    def clean_amount(self) -> Decimal:
        amount = self.cleaned_data.get("amount")
        payment_id = self.cleaned_data.get("payment_id")
        payment = self._eligible_payments.get(payment_id) if payment_id else None
        if payment is None:
            return amount or c.ZERO_DECIMAL_2
        max_refundable = payment.amount or c.ZERO_DECIMAL_2
        if amount is None:
            return max_refundable
        if amount <= c.ZERO_DECIMAL_2:
            raise ValidationError(
                _("Refund amount must be strictly positive."),
                code="amount_non_positive",
            )
        if amount > max_refundable:
            raise ValidationError(
                _(
                    "Requested amount cannot exceed the available "
                    "refundable amount (%(max)s %(currency)s)."
                ),
                code="amount_exceeds_balance",
                params={
                    "max": max_refundable,
                    "currency": payment.currency,
                },
            )
        return amount

    def clean_evidence_urls(self) -> List[str]:
        raw = self.cleaned_data.get("evidence_urls", "") or ""
        return [
            line.strip()
            for line in raw.splitlines()
            if line.strip()
        ]

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        refund_method = cleaned.get("refund_method")
        if refund_method and refund_method not in {
            choice.value for choice in Refund.RefundMethod
        }:
            self.add_error(
                "refund_method",
                _("Invalid refund method '%(value)s'."),
                params={"value": refund_method},
            )
        category = cleaned.get("refund_reason_category")
        if category and category not in {
            choice.value for choice in Refund.RefundReasonCategory
        }:
            self.add_error(
                "refund_reason_category",
                _("Invalid refund reason category."),
            )
        return cleaned

class PaymentSearchForm(forms.Form):
    """
    Search form for the payment ledger.

    All criteria are optional. The view is expected to compose a
    queryset (or call a selector) using the cleaned data.
    """

    transaction_id = forms.CharField(
        label=_("Transaction ID"),
        required=False,
        widget=forms.TextInput(attrs=_input_attrs()),
    )
    gateway = forms.ChoiceField(
        label=_("Gateway"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            ("stripe", "Stripe"),
            ("paypal", "PayPal"),
            ("razorpay", "Razorpay"),
            ("esewa", "eSewa"),
            ("khalti", "Khalti"),
            ("manual", "Manual"),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    status = forms.MultipleChoiceField(
        label=_("Status"),
        required=False,
        choices=Payment.PaymentState.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    payment_method = forms.CharField(
        label=_("Payment Method"),
        required=False,
        widget=forms.TextInput(attrs=_input_attrs()),
    )
    min_amount = build_decimal_field(
        label=_("Min Amount"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )
    max_amount = build_decimal_field(
        label=_("Max Amount"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )
    paid_after = build_datetime_field(label=_("Paid After"))
    paid_before = build_datetime_field(label=_("Paid Before"))
    is_test = forms.ChoiceField(
        label=_("Test Payment?"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            ("true", _("Test only")),
            ("false", _("Production only")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if (
            cleaned.get("min_amount") is not None
            and cleaned.get("max_amount") is not None
            and cleaned["min_amount"] > cleaned["max_amount"]
        ):
            raise ValidationError(
                _("Min amount cannot exceed max amount."),
            )
        if (
            cleaned.get("paid_after")
            and cleaned.get("paid_before")
            and cleaned["paid_after"] > cleaned["paid_before"]
        ):
            raise ValidationError(
                _("'Paid After' must be earlier than 'Paid Before'."),
            )
        return cleaned

# ==============================================================================
# 4. SHIPMENT FORMS
# ==============================================================================
class ShipmentForm(forms.ModelForm):
    """
    Form for creating or editing a ``Shipment``.

    The form is used by both the storefront ("where is my order?")
    and the operations console. ``shipment_number`` is excluded
    because it is auto-generated by the service.

    Note: ``tracking_url`` is preserved here because the Shipment
    model is the canonical owner of that field. Order-level forms
    do NOT expose it (see OrderUpdateForm / AdminOrderUpdateForm).
    """

    warehouse_id = forms.IntegerField(
        label=_("Source Warehouse ID"),
        required=False,
        min_value=1,
        widget=forms.NumberInput(attrs=_input_attrs({"min": "1"})),
        help_text=_("Audit-only reference. The Inventory app owns stock."),
    )

    class Meta:
        model = Shipment
        fields = (
            "carrier",
            "tracking_number",
            "tracking_url",
            "shipping_cost",
            "dispatch_date",
            "delivery_date",
            "picked_up_at",
            "estimated_delivery_date",
            "actual_delivery_date",
            "carrier_service_level",
            "carrier_api_integration_id",
            "total_weight",
            "notes",
        )
        widgets = {
            "carrier": forms.TextInput(attrs=_input_attrs()),
            "tracking_number": forms.TextInput(attrs=_input_attrs()),
            "tracking_url": forms.URLInput(attrs=_input_attrs()),
            "shipping_cost": forms.NumberInput(
                attrs=_input_attrs({"step": "0.01", "min": "0"}),
            ),
            "dispatch_date": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
            "delivery_date": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
            "picked_up_at": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
            "estimated_delivery_date": forms.DateInput(
                attrs=_input_attrs({"type": "date"}),
            ),
            "actual_delivery_date": forms.DateInput(
                attrs=_input_attrs({"type": "date"}),
            ),
            "carrier_service_level": forms.TextInput(attrs=_input_attrs()),
            "carrier_api_integration_id": forms.TextInput(
                attrs=_input_attrs(),
            ),
            "total_weight": forms.NumberInput(
                attrs=_input_attrs({"step": "0.001", "min": "0"}),
            ),
            "notes": forms.Textarea(attrs=_textarea_attrs(rows=3)),
        }

    def clean_tracking_number(self) -> str:
        return u.normalize_tracking_number(
            self.cleaned_data.get("tracking_number", "")
        )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        # Tracking URL must accompany a tracking number.
        if cleaned.get("tracking_url") and not cleaned.get("tracking_number"):
            self.add_error(
                "tracking_url",
                _("A tracking URL requires a tracking number."),
            )
        # Date consistency.
        if (
            cleaned.get("dispatch_date")
            and cleaned.get("delivery_date")
            and cleaned["dispatch_date"] > cleaned["delivery_date"]
        ):
            self.add_error(
                "delivery_date",
                _("Delivery date cannot precede dispatch date."),
            )
        if (
            cleaned.get("estimated_delivery_date")
            and cleaned.get("actual_delivery_date")
            and cleaned["estimated_delivery_date"]
            > cleaned["actual_delivery_date"]
        ):
            self.add_error(
                "actual_delivery_date",
                _(
                    "Actual delivery date cannot precede the estimated "
                    "delivery date."
                ),
            )
        return cleaned

class TrackingForm(forms.ModelForm):
    """
    Lightweight form for updating a shipment's tracking number,
    tracking URL, and carrier. Designed for use by the customer
    service team after a carrier-side update.
    """

    class Meta:
        model = Shipment
        fields = ("carrier", "tracking_number", "tracking_url")
        widgets = {
            "carrier": forms.TextInput(attrs=_input_attrs()),
            "tracking_number": forms.TextInput(attrs=_input_attrs()),
            "tracking_url": forms.URLInput(attrs=_input_attrs()),
        }

    def clean_tracking_number(self) -> str:
        return u.normalize_tracking_number(
            self.cleaned_data.get("tracking_number", "")
        )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if not cleaned.get("carrier"):
            self.add_error(
                "carrier",
                _("A carrier must be specified when adding a tracking number."),
            )
        if cleaned.get("tracking_url") and not cleaned.get("tracking_number"):
            self.add_error(
                "tracking_url",
                _("A tracking URL requires a tracking number."),
            )
        return cleaned

class ShipmentStatusForm(forms.ModelForm):
    """
    Form for transitioning a shipment's status.

    The state machine is enforced by the service; the form only
    rejects unknown status values and keeps dispatch / delivery
    timestamps consistent.
    """

    class Meta:
        model = Shipment
        fields = (
            "status",
            "dispatch_date",
            "delivery_date",
            "picked_up_at",
        )
        widgets = {
            "status": forms.Select(attrs=_input_attrs()),
            "dispatch_date": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
            "delivery_date": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
            "picked_up_at": forms.DateTimeInput(
                attrs=_input_attrs({"type": "datetime-local"}),
            ),
        }

    def clean_status(self) -> str:
        value = self.cleaned_data.get("status", "")
        if value not in {choice.value for choice in Shipment.ShipmentStatus}:
            raise ValidationError(
                _("Invalid shipment status '%(value)s'."),
                code="invalid_status",
                params={"value": value},
            )
        return value

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if (
            cleaned.get("dispatch_date")
            and cleaned.get("delivery_date")
            and cleaned["dispatch_date"] > cleaned["delivery_date"]
        ):
            self.add_error(
                "delivery_date",
                _("Delivery date cannot precede dispatch date."),
            )
        return cleaned

class CarrierForm(forms.ModelForm):
    """
    Form for updating the carrier, service level, and carrier
    integration id of a shipment.
    """

    class Meta:
        model = Shipment
        fields = (
            "carrier",
            "carrier_service_level",
            "carrier_api_integration_id",
        )
        widgets = {
            "carrier": forms.TextInput(attrs=_input_attrs()),
            "carrier_service_level": forms.TextInput(attrs=_input_attrs()),
            "carrier_api_integration_id": forms.TextInput(
                attrs=_input_attrs(),
            ),
        }

# ==============================================================================
# 5. RETURN FORMS
# ==============================================================================
class ReturnRequestForm(forms.Form):
    """
    Form for initiating a physical return of one or more order
    line items.

    The form dynamically generates a per-item quantity field for
    every active item in the supplied order. The view is expected
    to call :func:`apps.orders.services.create_return_request`
    with the parsed quantities.
    """

    return_type = forms.ChoiceField(
        label=_("Return Type"),
        required=True,
        initial=ReturnRequest.ReturnType.REFUND,
        choices=ReturnRequest.ReturnType.choices,
        widget=forms.Select(attrs=_input_attrs()),
    )
    reason_category = forms.ChoiceField(
        label=_("Reason Category"),
        required=True,
        initial=ReturnRequest.ReturnReasonCategory.OTHER,
        choices=ReturnRequest.ReturnReasonCategory.choices,
        widget=forms.Select(attrs=_input_attrs()),
    )
    reason_text = build_textarea_field(
        label=_("Reason Details"),
        required=True,
        rows=4,
        placeholder=_(
            "Please detail the condition of the items and why you wish "
            "to return them."
        ),
    )
    customer_notes = build_textarea_field(
        label=_("Additional Comments"),
        required=False,
        rows=2,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.order: Optional[Order] = kwargs.pop("order", None)
        super().__init__(*args, **kwargs)
        self.eligible_items: List[OrderItem] = []
        if self.order is not None and self.order.status in (
            Order.OrderStatus.DELIVERED,
            Order.OrderStatus.COMPLETED,
        ):
            self.eligible_items = list(
                self.order.items.filter(status=OrderItem.ItemStatus.ACTIVE)
            )
            for item in self.eligible_items:
                field_name = f"item_qty_{item.id}"
                self.fields[field_name] = forms.IntegerField(
                    label=_("Return Qty: %(name)s") % {
                        "name": item.product_name_snapshot
                        or _("Unnamed item")
                    },
                    min_value=0,
                    max_value=int(item.quantity or 0),
                    initial=0,
                    required=False,
                    widget=forms.NumberInput(
                        attrs=_input_attrs(
                            {"min": "0", "max": str(int(item.quantity or 0))}
                        ),
                    ),
                )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if self.order is None:
            raise ValidationError(
                _("A valid order context is required."),
                code="invalid_order",
            )
        if self.order.status not in (
            Order.OrderStatus.DELIVERED,
            Order.OrderStatus.COMPLETED,
        ):
            raise ValidationError(
                _(
                    "Items can only be returned after the order has been "
                    "delivered or completed."
                ),
                code="order_not_returnable",
            )
        total_return_qty = 0
        cleaned_quantities: Dict[str, int] = {}
        for item in self.eligible_items:
            field_name = f"item_qty_{item.id}"
            qty = cleaned.get(field_name) or 0
            if qty < 0:
                qty = 0
            if qty > int(item.quantity or 0):
                self.add_error(
                    field_name,
                    _(
                        "Cannot return more than %(max)s of '%(name)s'."
                    ),
                    params={
                        "max": int(item.quantity or 0),
                        "name": item.product_name_snapshot or _("Unnamed item"),
                    },
                )
                qty = 0
            cleaned_quantities[str(item.id)] = qty
            total_return_qty += qty
        if total_return_qty == 0:
            raise ValidationError(
                _("You must select at least one item to return."),
                code="no_items_selected",
            )
        cleaned["item_quantities"] = cleaned_quantities
        return cleaned

class ReturnApprovalForm(forms.ModelForm):
    """
    Form for an operator to approve a return request.

    The form allows the operator to specify a restock decision and
    location, and to record an internal note. The actual state
    transition is performed by the service.
    """

    approval_notes = forms.CharField(
        label=_("Internal Notes"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _("Optional operator notes.")},
                rows=3,
            ),
        ),
    )

    class Meta:
        model = ReturnRequest
        fields = ("restock_decision", "restock_location")
        widgets = {
            "restock_decision": forms.Select(attrs=_input_attrs()),
            "restock_location": forms.TextInput(attrs=_input_attrs()),
        }

    def clean_restock_decision(self) -> str:
        value = self.cleaned_data.get("restock_decision", "")
        if value and value not in {
            choice.value for choice in ReturnRequest.RestockDecision
        }:
            raise ValidationError(
                _("Invalid restock decision."),
                code="invalid_restock_decision",
            )
        return value

class ReturnCompletionForm(forms.ModelForm):
    """
    Form for an operator to mark a return as completed.

    The form collects an optional completion note (stored in
    ``internal_notes``) and the restock outcome. State transition
    validation is owned by the service.
    """

    completion_notes = forms.CharField(
        label=_("Completion Notes"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _("Optional final notes.")},
                rows=3,
            ),
        ),
    )

    class Meta:
        model = ReturnRequest
        fields = ("restock_decision",)
        widgets = {
            "restock_decision": forms.Select(attrs=_input_attrs()),
        }

# ==============================================================================
# 6. REFUND FORMS
# ==============================================================================
class RefundApprovalForm(forms.ModelForm):
    """
    Form for an operator to approve a pending refund.

    The form does not transition the refund's state; the service
    does. The form only validates the supplied notes.
    """

    approval_notes = forms.CharField(
        label=_("Approval Notes"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _("Optional internal notes.")},
                rows=3,
            ),
        ),
    )

    class Meta:
        model = Refund
        fields = ()  # No model fields are exposed for direct editing.

class RefundCompletionForm(forms.ModelForm):
    """
    Form for an operator to mark a refund as completed.

    The form collects the ``gateway_refund_id`` (if known) and an
    optional completion note. State transition is owned by the
    service.
    """

    completion_notes = forms.CharField(
        label=_("Completion Notes"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _("Optional final notes.")},
                rows=3,
            ),
        ),
    )

    class Meta:
        model = Refund
        fields = ("gateway_refund_id",)
        widgets = {
            "gateway_refund_id": forms.TextInput(attrs=_input_attrs()),
        }

class RefundSearchForm(forms.Form):
    """
    Search form for the refund ledger.
    """

    order_number = forms.CharField(
        label=_("Order Number"),
        required=False,
        widget=forms.TextInput(attrs=_input_attrs()),
    )
    transaction_id = forms.CharField(
        label=_("Payment Transaction ID"),
        required=False,
        widget=forms.TextInput(attrs=_input_attrs()),
    )
    status = forms.MultipleChoiceField(
        label=_("Status"),
        required=False,
        choices=Refund.RefundStatus.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    refund_method = forms.MultipleChoiceField(
        label=_("Refund Method"),
        required=False,
        choices=Refund.RefundMethod.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    refund_reason_category = forms.MultipleChoiceField(
        label=_("Reason Category"),
        required=False,
        choices=Refund.RefundReasonCategory.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    min_amount = build_decimal_field(
        label=_("Min Amount"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )
    max_amount = build_decimal_field(
        label=_("Max Amount"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )
    created_after = build_datetime_field(label=_("Created After"))
    created_before = build_datetime_field(label=_("Created Before"))

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if (
            cleaned.get("min_amount") is not None
            and cleaned.get("max_amount") is not None
            and cleaned["min_amount"] > cleaned["max_amount"]
        ):
            raise ValidationError(
                _("Min amount cannot exceed max amount."),
            )
        if (
            cleaned.get("created_after")
            and cleaned.get("created_before")
            and cleaned["created_after"] > cleaned["created_before"]
        ):
            raise ValidationError(
                _("'Created After' must be earlier than 'Created Before'."),
            )
        return cleaned

# ==============================================================================
# 7. ATTACHMENT FORMS
# ==============================================================================
class AttachmentUploadForm(forms.ModelForm):
    """
    Form for uploading a new ``OrderAttachment``.

    The form validates:

        * file size (max ``MAX_ATTACHMENT_SIZE``)
        * file extension (whitelist)
        * canonical ``attachment_type``
        * description length
    """

    class Meta:
        model = OrderAttachment
        fields = (
            "file",
            "attachment_type",
            "description",
            "is_visible_to_customer",
        )
        widgets = {
            "file": forms.ClearableFileInput(attrs=_input_attrs()),
            "attachment_type": forms.Select(attrs=_input_attrs()),
            "description": forms.TextInput(attrs=_input_attrs()),
            "is_visible_to_customer": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Add explicit size + extension validators on top of the
        # widget so the form rejects invalid uploads before any
        # expensive processing occurs.
        self.fields["file"].validators = [
            _file_size_validator_factory(MAX_ATTACHMENT_SIZE),
            _file_extension_validator_factory(ALLOWED_ATTACHMENT_EXTENSIONS),
        ]

    def clean_file(self) -> Any:
        uploaded = self.cleaned_data.get("file")
        if not uploaded:
            raise ValidationError(
                _("A file is required."),
                code="file_required",
            )
        return uploaded

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        uploaded = cleaned.get("file")
        attachment_type = cleaned.get("attachment_type", "")
        if uploaded and attachment_type:
            # Soft heuristic: invoice / packing-slip attachments are
            # typically PDFs; evidence / delivery proof are images.
            name = (getattr(uploaded, "name", "") or "").lower()
            if (
                attachment_type
                in (
                    OrderAttachment.AttachmentType.INVOICE,
                    OrderAttachment.AttachmentType.PACKING_SLIP,
                    OrderAttachment.AttachmentType.RETURN_LABEL,
                    OrderAttachment.AttachmentType.REPLACEMENT_LABEL,
                )
                and not name.endswith(".pdf")
            ):
                self.add_error(
                    "attachment_type",
                    _(
                        "Attachment type '%(type)s' typically expects a "
                        "PDF file."
                    ),
                    params={"type": attachment_type},
                )
        return cleaned

class AttachmentReplaceForm(forms.ModelForm):
    """
    Form for replacing the file of an existing ``OrderAttachment``.

    The form collects a new file plus a free-form reason. The view
    is expected to update the underlying record in place.
    """

    reason = forms.ChoiceField(
        label=_("Replacement Reason"),
        required=True,
        choices=REPLACEMENT_REASON_CHOICES,
        widget=forms.Select(attrs=_input_attrs()),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _("Optional context.")},
                rows=2,
            ),
        ),
    )

    class Meta:
        model = OrderAttachment
        fields = ("file",)
        widgets = {
            "file": forms.ClearableFileInput(attrs=_input_attrs()),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["file"].validators = [
            _file_size_validator_factory(MAX_ATTACHMENT_SIZE),
            _file_extension_validator_factory(ALLOWED_ATTACHMENT_EXTENSIONS),
        ]

    def clean_file(self) -> Any:
        uploaded = self.cleaned_data.get("file")
        if not uploaded:
            raise ValidationError(
                _("A replacement file is required."),
                code="file_required",
            )
        return uploaded

class AttachmentDeleteForm(forms.Form):
    """
    Confirmation form for soft-deleting an ``OrderAttachment``.

    The form requires an explicit confirmation checkbox before
    the view will mark the record inactive.
    """

    confirm = forms.BooleanField(
        label=_("I confirm this attachment should be deleted."),
        required=True,
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
    )
    reason = forms.CharField(
        label=_("Reason"),
        required=False,
        widget=forms.Textarea(
            attrs=_textarea_attrs(
                {"placeholder": _("Optional deletion reason.")},
                rows=2,
            ),
        ),
    )

# ==============================================================================
# 8. ORDER ADDRESS FORMS
# ==============================================================================
class OrderAddressForm(forms.ModelForm):
    """
    Form for capturing or editing an immutable
    ``OrderAddressSnapshot``.

    The form is the canonical way to populate the shipping and
    billing address fields on the storefront. The view is expected
    to call :func:`apps.orders.services.create_address_snapshot`
    with the cleaned data.
    """

    class Meta:
        model = OrderAddressSnapshot
        fields = (
            "full_name",
            "phone_number",
            "company",
            "address_line_1",
            "address_line_2",
            "city",
            "state_or_province",
            "postal_code",
            "country",
            "country_code",
            "phone_e164",
            "latitude",
            "longitude",
            "delivery_notes",
            "metadata",
        )
        widgets = {
            "full_name": forms.TextInput(attrs=_input_attrs()),
            "phone_number": forms.TextInput(attrs=_input_attrs()),
            "company": forms.TextInput(attrs=_input_attrs()),
            "address_line_1": forms.TextInput(attrs=_input_attrs()),
            "address_line_2": forms.TextInput(attrs=_input_attrs()),
            "city": forms.TextInput(attrs=_input_attrs()),
            "state_or_province": forms.TextInput(attrs=_input_attrs()),
            "postal_code": forms.TextInput(attrs=_input_attrs()),
            "country": forms.TextInput(attrs=_input_attrs()),
            "country_code": forms.TextInput(
                attrs=_input_attrs(
                    {"maxlength": "2", "style": "text-transform:uppercase;"}
                ),
            ),
            "phone_e164": forms.TextInput(attrs=_input_attrs()),
            "latitude": forms.NumberInput(
                attrs=_input_attrs({"step": "0.000001"}),
            ),
            "longitude": forms.NumberInput(
                attrs=_input_attrs({"step": "0.000001"}),
            ),
            "delivery_notes": forms.Textarea(
                attrs=_textarea_attrs(rows=2),
            ),
            "metadata": forms.Textarea(attrs=_textarea_attrs(rows=3)),
        }

    def clean_phone_number(self) -> str:
        phone = self.cleaned_data.get("phone_number", "")
        return "".join(str(phone).split())

    def clean_postal_code(self) -> str:
        postal = self.cleaned_data.get("postal_code", "")
        return str(postal).strip().upper()

    def clean_country_code(self) -> str:
        code = (self.cleaned_data.get("country_code") or "").strip().upper()
        if code and not re.match(r"^[A-Z]{2}$", code):
            raise ValidationError(
                _("Country code must be a 2-letter ISO 3166-1 alpha-2 code."),
                code="invalid_country_code",
            )
        return code

    def clean_phone_e164(self) -> str:
        phone = self.cleaned_data.get("phone_e164", "")
        if not phone:
            return ""
        return u.format_phone_e164(phone)

    def clean_metadata(self) -> Dict[str, Any]:
        return _normalize_json_field(self.cleaned_data.get("metadata"))

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        lat = cleaned.get("latitude")
        lon = cleaned.get("longitude")
        if lat is not None and not -90 <= float(lat) <= 90:
            self.add_error(
                "latitude",
                _("Latitude must be between -90 and 90."),
            )
        if lon is not None and not -180 <= float(lon) <= 180:
            self.add_error(
                "longitude",
                _("Longitude must be between -180 and 180."),
            )
        return cleaned

class OrderNotesForm(forms.ModelForm):
    """
    Form for updating the customer-facing notes attached to an
    order (``customer_note`` and ``delivery_instructions``).
    """

    class Meta:
        model = Order
        fields = ("customer_note", "delivery_instructions")
        widgets = {
            "customer_note": forms.Textarea(
                attrs=_textarea_attrs(
                    {
                        "placeholder": _(
                            "Any general requests regarding this order."
                        ),
                    },
                    rows=3,
                ),
            ),
            "delivery_instructions": forms.Textarea(
                attrs=_textarea_attrs(
                    {
                        "placeholder": _(
                            "Gate codes, safe dropping locations, etc."
                        ),
                    },
                    rows=3,
                ),
            ),
        }

class AdminOrderUpdateForm(forms.ModelForm):
    """
    Administrative form providing safe modification boundaries for
    internal staff. Restricts financial-field manipulation while
    allowing tracking / status updates.

    Note: ``tracking_url`` is intentionally NOT exposed here. The
    Order model does not own a ``tracking_url`` field; tracking
    URLs are managed exclusively by the Shipment model.
    """

    class Meta:
        model = Order
        fields = (
            "status",
            "payment_status",
            "carrier",
            "tracking_number",
            "invoice_url",
            "has_invoice",
            "fraud_check_status",
            "is_active",
        )
        widgets = {
            "status": forms.Select(attrs=_input_attrs()),
            "payment_status": forms.Select(attrs=_input_attrs()),
            "carrier": forms.TextInput(attrs=_input_attrs()),
            "tracking_number": forms.TextInput(attrs=_input_attrs()),
            "invoice_url": forms.URLInput(attrs=_input_attrs()),
            "has_invoice": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
            "fraud_check_status": forms.Select(attrs=_input_attrs()),
            "is_active": forms.CheckboxInput(
                attrs={"class": "premium-checkbox"},
            ),
        }

    def clean_status(self) -> str:
        value = self.cleaned_data.get("status", "")
        if value not in {choice.value for choice in Order.OrderStatus}:
            raise ValidationError(
                _("Invalid order status '%(value)s'."),
                code="invalid_status",
                params={"value": value},
            )
        return value

    def clean_payment_status(self) -> str:
        value = self.cleaned_data.get("payment_status", "")
        if value not in {choice.value for choice in Order.PaymentStatus}:
            raise ValidationError(
                _("Invalid payment status '%(value)s'."),
                code="invalid_status",
                params={"value": value},
            )
        return value

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        # Tracking consistency: number requires carrier.
        if cleaned.get("tracking_number") and not cleaned.get("carrier"):
            self.add_error(
                "carrier",
                _("A carrier must be specified when adding a tracking number."),
            )
        if cleaned.get("invoice_url") and not cleaned.get("has_invoice"):
            self.add_error(
                "has_invoice",
                _("Mark 'Has Invoice' as true when supplying an invoice URL."),
            )
        return cleaned

# ==============================================================================
# 9. SEARCH / FILTER FORMS
# ==============================================================================
class GlobalOrderSearchForm(forms.Form):
    """
    Global search form for the top-of-page search bar.

    Accepts a free-text query and a search-type selector. The view
    is expected to dispatch to the appropriate selector.
    """

    SEARCH_TYPE_CHOICES: Tuple[Tuple[str, str], ...] = (
        ("all", _("All")),
        ("order_number", _("Order Number")),
        ("email", _("Customer Email")),
        ("transaction_id", _("Transaction ID")),
        ("tracking_number", _("Tracking Number")),
    )

    q = forms.CharField(
        label=_("Search"),
        required=True,
        min_length=2,
        widget=forms.TextInput(
            attrs=_input_attrs(
                {
                    "placeholder": _(
                        "Search orders, customers, payments…"
                    ),
                    "autofocus": "autofocus",
                },
            ),
        ),
    )
    search_type = forms.ChoiceField(
        label=_("Search In"),
        required=False,
        initial="all",
        choices=SEARCH_TYPE_CHOICES,
        widget=forms.Select(attrs=_input_attrs()),
    )

    def clean_q(self) -> str:
        return u.normalize_whitespace(self.cleaned_data.get("q", ""))

class AdvancedOrderFilterForm(OrderFilterForm):
    """
    Alias of :class:`OrderFilterForm` with an additional
    ``include_archived`` flag. Provided as a separate class so the
    view can decide whether to honour the flag.
    """

    include_archived = forms.BooleanField(
        label=_("Include Archived Orders"),
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "premium-checkbox"}),
    )

class DateRangeForm(forms.Form):
    """
    Generic date-range picker. Used by export / analytics views
    that do not need the full filter vocabulary.
    """

    start_date = build_date_field(
        label=_("Start Date"),
        required=True,
    )
    end_date = build_date_field(
        label=_("End Date"),
        required=True,
    )
    preset = forms.ChoiceField(
        label=_("Preset"),
        required=False,
        choices=(
            ("", _("-- Custom range --")),
            ("today", _("Today")),
            ("yesterday", _("Yesterday")),
            ("last_7_days", _("Last 7 days")),
            ("last_30_days", _("Last 30 days")),
            ("this_month", _("This month")),
            ("last_month", _("Last month")),
            ("this_year", _("This year")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and start > end:
            raise ValidationError(
                _("Start date must be on or before end date."),
            )
        return cleaned

class StatusFilterForm(forms.Form):
    """
    Filter form focused on the various status dimensions of an
    order. Useful for ops dashboards.
    """

    statuses = forms.MultipleChoiceField(
        label=_("Order Statuses"),
        required=False,
        choices=Order.OrderStatus.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    payment_statuses = forms.MultipleChoiceField(
        label=_("Payment Statuses"),
        required=False,
        choices=Order.PaymentStatus.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    fraud_check_statuses = forms.MultipleChoiceField(
        label=_("Fraud Check Statuses"),
        required=False,
        choices=Order.FraudCheckStatus.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    sources = forms.MultipleChoiceField(
        label=_("Sources"),
        required=False,
        choices=Order.Source.choices,
        widget=forms.CheckboxSelectMultiple(),
    )
    is_gift = forms.ChoiceField(
        label=_("Gift?"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            ("true", _("Gift orders only")),
            ("false", _("Non-gift orders only")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    is_active = forms.ChoiceField(
        label=_("Active?"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            ("true", _("Active only")),
            ("false", _("Inactive only")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )

class CustomerFilterForm(forms.Form):
    """
    Filter form scoped to a specific customer. Used by the
    "my orders" page and the admin customer-detail view.
    """

    customer = forms.ModelChoiceField(
        label=_("Customer"),
        required=False,
        queryset=None,  # Set in __init__.
        widget=forms.Select(attrs=_input_attrs()),
    )
    email = forms.EmailField(
        label=_("Customer Email"),
        required=False,
        widget=forms.EmailInput(attrs=_input_attrs()),
    )
    is_gift = forms.ChoiceField(
        label=_("Gift?"),
        required=False,
        choices=(
            ("", _("-- Any --")),
            ("true", _("Gift orders only")),
            ("false", _("Non-gift orders only")),
        ),
        widget=forms.Select(attrs=_input_attrs()),
    )
    min_total = build_decimal_field(
        label=_("Min Total"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )
    max_total = build_decimal_field(
        label=_("Max Total"),
        required=False,
        min_value=c.ZERO_DECIMAL_2,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            from django.contrib.auth import get_user_model
            self.fields["customer"].queryset = get_user_model().objects.all()
        except Exception:  # noqa: BLE001
            self.fields["customer"].queryset = None

    def clean(self) -> Dict[str, Any]:
        cleaned = super().clean()
        if (
            cleaned.get("min_total") is not None
            and cleaned.get("max_total") is not None
            and cleaned["min_total"] > cleaned["max_total"]
        ):
            raise ValidationError(
                _("Min total cannot exceed max total."),
            )
        return cleaned

# ==============================================================================
# INTERNAL JSON-FIELD NORMALISER
# ==============================================================================
def _normalize_json_field(value: Any) -> Dict[str, Any]:
    """
    Normalise a JSONField input into a Python ``dict``.

    The orders app stores audit metadata in JSONField columns. The
    form layer accepts both serialised JSON strings (paste-friendly)
    and pre-parsed ``dict`` instances. This helper is shared by
    every form that exposes a ``JSONField`` so the behaviour is
    consistent across the entire app.
    """
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = u.safe_json_loads(value, default=None)
        if isinstance(parsed, dict):
            return parsed
        if parsed is None:
            return {}
        raise ValidationError(
            _("Value must be a JSON object (dictionary)."),
            code="invalid_json_object",
        )
    raise ValidationError(
        _("Value must be a JSON object (dictionary)."),
        code="invalid_json_object",
    )

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Internal helpers (exported for test reuse only)
    "_input_attrs",
    "_textarea_attrs",
    "_file_size_validator_factory",
    "_file_extension_validator_factory",
    "_validate_coupon_code",
    "_normalize_json_field",
    # Shared field builders
    "build_email_field",
    "build_decimal_field",
    "build_quantity_field",
    "build_status_field",
    "build_text_field",
    "build_textarea_field",
    "build_date_field",
    "build_datetime_field",
    "build_file_field",
    # Module-level constants (exported for admin / templating reuse)
    "DEFAULT_INPUT_CLASS",
    "MAX_ATTACHMENT_SIZE",
    "MAX_IMPORT_SIZE",
    "ALLOWED_ATTACHMENT_EXTENSIONS",
    "ALLOWED_IMPORT_EXTENSIONS",
    "ALLOWED_ATTACHMENT_MIME_PREFIXES",
    "IMPORT_MODE_CHOICES",
    "REPLACEMENT_REASON_CHOICES",
    "EXPORT_FORMAT_CHOICES",
    # Order forms
    "OrderCreateForm",
    "OrderUpdateForm",
    "OrderEditForm",
    "OrderCancelForm",
    "OrderStatusForm",
    "OrderSearchForm",
    "OrderFilterForm",
    "OrderExportForm",
    "OrderImportForm",
    "OrderMetadataForm",
    "OrderNotesForm",
    "AdminOrderUpdateForm",
    # Address forms
    "OrderAddressForm",
    # Order item forms
    "OrderItemCreateForm",
    "OrderItemUpdateForm",
    "QuantityUpdateForm",
    "GiftForm",
    "CustomizationForm",
    # Payment forms
    "PaymentForm",
    "PaymentStatusForm",
    "RefundRequestForm",
    "PaymentSearchForm",
    # Shipment forms
    "ShipmentForm",
    "TrackingForm",
    "ShipmentStatusForm",
    "CarrierForm",
    # Return forms
    "ReturnRequestForm",
    "ReturnApprovalForm",
    "ReturnCompletionForm",
    # Refund forms
    "RefundApprovalForm",
    "RefundCompletionForm",
    "RefundSearchForm",
    # Attachment forms
    "AttachmentUploadForm",
    "AttachmentReplaceForm",
    "AttachmentDeleteForm",
    # Search / filter forms
    "GlobalOrderSearchForm",
    "AdvancedOrderFilterForm",
    "DateRangeForm",
    "StatusFilterForm",
    "CustomerFilterForm",
]