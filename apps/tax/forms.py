import json
from typing import Any, Dict

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import CustomerTaxExemption, TaxClass, TaxRule, TaxSettings, TaxZone

class TaxSettingsAdminForm(forms.ModelForm):
    class Meta:
        model = TaxSettings
        fields = "__all__"

class TaxClassAdminForm(forms.ModelForm):
    class Meta:
        model = TaxClass
        fields = "__all__"

    def clean_code(self) -> str:
        code = self.cleaned_data.get("code", "").strip()
        if not code:
            raise forms.ValidationError(_("Tax class code cannot be empty."))
        return code.upper()

class TaxZoneAdminForm(forms.ModelForm):
    class Meta:
        model = TaxZone
        fields = "__all__"

    def clean_countries(self) -> Any:
        value = self.cleaned_data.get("countries")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                raise forms.ValidationError(_("Countries must be a valid JSON list."))
        return value or []

class TaxRuleAdminForm(forms.ModelForm):
    class Meta:
        model = TaxRule
        fields = "__all__"

class CustomerTaxExemptionForm(forms.ModelForm):
    class Meta:
        model = CustomerTaxExemption
        fields = ["exemption_number", "reason"]
        widgets = {
            "exemption_number": forms.TextInput(attrs={"placeholder": _("Enter VAT ID or Tax Exemption Number"), "class": "form-input"}),
            "reason": forms.TextInput(attrs={"placeholder": _("Reseller, Export, Government Entity"), "class": "form-input"}),
        }