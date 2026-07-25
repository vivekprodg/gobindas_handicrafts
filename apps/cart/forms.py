from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from django import forms
from django.utils.translation import gettext_lazy as _

class AddToCartForm(forms.Form):
    """Form for adding a product to the cart."""
    product_id = forms.IntegerField(widget=forms.HiddenInput())
    variant_id = forms.IntegerField(widget=forms.HiddenInput(), required=False)
    quantity = forms.IntegerField(
        min_value=1,
        max_value=999,
        initial=1,
        label=_("Quantity"),
    )
    personalization_text = forms.CharField(
        required=False,
        max_length=200,
        label=_("Personalization (optional)"),
        widget=forms.TextInput(attrs={
            "placeholder": _("Enter engraving text or special instructions"),
            "class": "form-input",
        }),
    )

    def clean_quantity(self) -> int:
        qty = self.cleaned_data.get("quantity") or 1
        if qty < 1:
            raise forms.ValidationError(_("Quantity must be at least 1."))
        if qty > 999:
            raise forms.ValidationError(_("Quantity cannot exceed 999 per order."))
        return qty

    def clean_personalization_text(self) -> Optional[str]:
        text = self.cleaned_data.get("personalization_text")
        if text:
            text = text.strip()
            return text if text else None
        return None

class UpdateCartItemForm(forms.Form):
    """Form for updating the quantity of a cart item."""
    quantity = forms.IntegerField(
        min_value=1,
        max_value=999,
        label=_("Quantity"),
    )

    def clean_quantity(self) -> int:
        qty = self.cleaned_data.get("quantity")
        if qty is None:
            raise forms.ValidationError(_("Quantity is required."))
        if qty < 1:
            raise forms.ValidationError(_("Quantity must be at least 1."))
        if qty > 999:
            raise forms.ValidationError(_("Quantity cannot exceed 999 per order."))
        return qty

class ApplyCouponForm(forms.Form):
    """Form for applying a coupon code to the cart."""
    coupon_code = forms.CharField(
        max_length=64,
        label=_("Coupon Code"),
        widget=forms.TextInput(attrs={
            "placeholder": _("Enter coupon code"),
            "autocomplete": "off",
            "class": "form-input",
        }),
    )
    discount_amount = forms.DecimalField(
        required=False,
        decimal_places=2,
        max_digits=12,
        label=_("Discount Amount"),
        help_text=_(
            "If left blank, the system will apply the discount associated with the coupon code."
        ),
    )

    def clean_coupon_code(self) -> str:
        code = self.cleaned_data.get("coupon_code", "").strip()
        if not code:
            raise forms.ValidationError(_("Coupon code is required."))
        return code.upper()

    def clean_discount_amount(self) -> Optional[Decimal]:
        discount = self.cleaned_data.get("discount_amount")
        if discount is not None:
            if discount < 0:
                raise forms.ValidationError(_("Discount amount cannot be negative."))
            return discount.quantize(Decimal("0.01"))
        return None

    def clean(self) -> dict[str, Any]:
        return super().clean()

class CartItemActionForm(forms.Form):
    """Generic form for cart item actions (CSRF protection)."""
    pass

class CartClearForm(forms.Form):
    """Form for clearing the entire cart (CSRF protection)."""
    pass

__all__ = [
    "AddToCartForm",
    "UpdateCartItemForm",
    "ApplyCouponForm",
    "CartItemActionForm",
    "CartClearForm",
]