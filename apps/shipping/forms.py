import json
from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import (
    ShipmentTrackingRecord,
    ShippingMethod,
    ShippingSettings,
    ShippingZone,
    WeightTierRate,
)

class ShippingSettingsAdminForm(forms.ModelForm):
    class Meta:
        model = ShippingSettings
        fields = "__all__"

class ShippingZoneAdminForm(forms.ModelForm):
    class Meta:
        model = ShippingZone
        fields = "__all__"

    def clean_countries(self) -> Any:
        val = self.cleaned_data.get("countries")
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                raise forms.ValidationError(_("Countries must be a valid JSON list of ISO codes."))
        return val or []

class ShippingMethodAdminForm(forms.ModelForm):
    class Meta:
        model = ShippingMethod
        fields = "__all__"

class WeightTierRateAdminForm(forms.ModelForm):
    class Meta:
        model = WeightTierRate
        fields = "__all__"

class TrackingLookupForm(forms.Form):
    tracking_number = forms.CharField(
        max_length=100,
        required=True,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("Enter tracking number (e.g., NP12345678)"),
                "class": "form-input tracking-input",
                "autocomplete": "off",
            }
        ),
        label=_("Tracking Number"),
    )

    def clean_tracking_number(self) -> str:
        num = self.cleaned_data.get("tracking_number", "").strip()
        if not num:
            raise forms.ValidationError(_("Tracking number cannot be blank."))
        return num