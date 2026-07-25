"""
Django forms for coupon application and admin configuration management.
"""
from __future__ import annotations

from typing import Any, Dict
from django import forms
from django.utils.translation import gettext_lazy as _

from . import constants as c
from .models import Coupon, CouponCMSSetting

class ApplyCouponForm(forms.Form):
    """
    Front-end form for submitting coupon codes in Cart and Checkout.
    """
    coupon_code = forms.CharField(
        max_length=50,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "cart-coupon-input",
                "placeholder": "Enter coupon code (e.g. HANDICRAFT10)",
                "autocomplete": "off",
                "data-cart-coupon-input": "true",
            }
        ),
        label=_("Coupon Code")
    )

    def clean_coupon_code(self) -> str:
        code = self.cleaned_data.get("coupon_code", "")
        clean_code = str(code).strip().upper()
        if not clean_code:
            raise forms.ValidationError(_("Please enter a valid coupon code."))
        return clean_code

class CouponAdminForm(forms.ModelForm):
    """
    Admin form with rich validation rules for backend managers.
    """

    class Meta:
        model = Coupon
        fields = "__all__"

    def clean(self) -> Dict[str, Any]:
        cleaned_data = super().clean()
        discount_type = cleaned_data.get("discount_type")
        discount_value = cleaned_data.get("discount_value")
        valid_from = cleaned_data.get("valid_from")
        valid_to = cleaned_data.get("valid_to")

        if discount_type == c.DiscountType.PERCENTAGE and discount_value:
            if discount_value > c.MAX_DISCOUNT_PERCENTAGE:
                self.add_error("discount_value", _("Percentage discount value cannot exceed 100%."))

        if valid_from and valid_to and valid_from >= valid_to:
            self.add_error("valid_to", _("Expiration end date must be strictly after the start date."))

        return cleaned_data