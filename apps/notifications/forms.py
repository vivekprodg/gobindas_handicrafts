from typing import Any, Dict

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import NotificationPreference, NotificationSetting, NotificationTemplate

class NotificationSettingAdminForm(forms.ModelForm):
    """
    Form for validating and masking Notification/SMTP settings in Django Admin.
    """

    class Meta:
        model = NotificationSetting
        fields = "__all__"
        widgets = {
            "smtp_password": forms.PasswordInput(render_value=True, attrs={"class": "vTextField"}),
        }

    def clean_smtp_host(self) -> str:
        host = self.cleaned_data.get("smtp_host", "").strip()
        is_active = self.cleaned_data.get("is_active", True)
        if is_active and not host:
            raise forms.ValidationError(_("SMTP Host is required when configuration is active."))
        return host

    def clean_smtp_port(self) -> int:
        port = self.cleaned_data.get("smtp_port")
        if port is not None and (port < 1 or port > 65535):
            raise forms.ValidationError(_("Port number must be between 1 and 65535."))
        return port

    def clean_default_from_email(self) -> str:
        email = self.cleaned_data.get("default_from_email", "").strip()
        if not email:
            raise forms.ValidationError(_("Default From Email cannot be blank."))
        return email

    def clean_company_notification_email(self) -> str:
        email = self.cleaned_data.get("company_notification_email", "").strip()
        if not email:
            raise forms.ValidationError(_("Company Notification Email cannot be blank."))
        return email

class NotificationTemplateAdminForm(forms.ModelForm):
    class Meta:
        model = NotificationTemplate
        fields = "__all__"

    def clean_code(self) -> str:
        code = self.cleaned_data.get("code", "").strip().lower()
        if not code:
            raise forms.ValidationError(_("Template code cannot be empty."))
        return code

class NotificationPreferenceForm(forms.ModelForm):
    class Meta:
        model = NotificationPreference
        fields = ["email_notifications", "sms_notifications", "marketing_emails"]
        widgets = {
            "email_notifications": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
            "sms_notifications": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
            "marketing_emails": forms.CheckboxInput(attrs={"class": "form-checkbox"}),
        }