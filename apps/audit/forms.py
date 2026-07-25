from typing import Any, Dict

from django import forms
from django.utils.translation import gettext_lazy as _

from .constants import AuditAction, AuditSeverity
from .models import AuditLog

class AuditFilterForm(forms.Form):
    action = forms.ChoiceField(
        choices=[("", _("All Actions"))] + list(AuditAction.CHOICES),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Action"),
    )
    severity = forms.ChoiceField(
        choices=[("", _("All Severities"))] + list(AuditSeverity.CHOICES),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
        label=_("Severity"),
    )

    def clean(self) -> Dict[str, Any]:
        return super().clean()