from django import forms
from django.utils.translation import gettext_lazy as _

from .constants import PaymentGatewayCode
from .models import PaymentSettings, PaymentTransaction

class PaymentSettingsAdminForm(forms.ModelForm):
    class Meta:
        model = PaymentSettings
        fields = "__all__"

class CheckoutPaymentSelectForm(forms.Form):
    gateway = forms.ChoiceField(
        choices=PaymentGatewayCode.CHOICES,
        widget=forms.RadioSelect(attrs={"class": "payment-gateway-radio"}),
        label=_("Select Payment Method"),
    )

class BankTransferReceiptForm(forms.Form):
    receipt_image = forms.ImageField(
        required=True,
        label=_("Upload Bank Transfer Deposit Slip / Screenshot"),
        widget=forms.ClearableFileInput(attrs={"class": "form-input"}),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"placeholder": _("Sender bank name, transaction reference..."), "class": "form-textarea", "rows": 2}),
        label=_("Deposit Notes"),
    )