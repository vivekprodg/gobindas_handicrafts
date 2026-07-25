from typing import Any, Optional

from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import path
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .forms import NotificationSettingAdminForm, NotificationTemplateAdminForm
from .models import NotificationLog, NotificationPreference, NotificationSetting, NotificationTemplate

@admin.register(NotificationSetting)
class NotificationSettingAdmin(admin.ModelAdmin):
    form = NotificationSettingAdminForm
    list_display = (
        "name",
        "provider",
        "smtp_host",
        "smtp_port",
        "encryption",
        "default_from_email",
        "company_notification_email",
        "is_active",
        "test_connection_button",
    )
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (_("General Configuration"), {
            "fields": ("name", "is_active")
        }),
        (_("Provider Settings"), {
            "fields": ("provider", "smtp_host", "smtp_port", "encryption", "auth_mode")
        }),
        (_("Authentication Credentials"), {
            "fields": ("smtp_username", "smtp_password")
        }),
        (_("Default Sender Identity"), {
            "fields": ("default_from_email", "default_sender_name", "company_notification_email")
        }),
        (_("Reliability & Timeouts"), {
            "fields": ("timeout", "max_retries", "retry_delay")
        }),
        (_("System Metadata"), {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at")
        }),
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request: HttpRequest, obj: Optional[Any] = None) -> bool:
        return False

    @admin.display(description=_("Diagnostics"))
    def test_connection_button(self, obj: NotificationSetting) -> str:
        if not obj.pk:
            return "-"
        return format_html(
            '<a class="button" href="{}">{}</a>',
            f"./{obj.pk}/test-smtp/",
            _("Test Connection")
        )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/test-smtp/",
                self.admin_site.admin_view(self.test_smtp_view),
                name="notification_setting_test_smtp",
            ),
        ]
        return custom_urls + urls

    def test_smtp_view(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        setting = self.get_object(request, object_id)
        if not setting:
            self.message_user(request, _("Notification settings not found."), level=messages.ERROR)
            return HttpResponseRedirect("../")

        try:
            backend = setting.get_smtp_connection()
            backend.open()
            backend.close()
            self.message_user(
                request,
                _("SMTP Diagnostic Success: Connected to %s:%s successfully.") % (setting.smtp_host, setting.smtp_port),
                level=messages.SUCCESS,
            )
        except Exception as exc:
            self.message_user(
                request,
                _("SMTP Diagnostic Failure: %s") % str(exc),
                level=messages.ERROR,
            )

        return HttpResponseRedirect("../")

@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    form = NotificationTemplateAdminForm
    list_display = ("title", "code", "channel", "is_active", "created_at")
    list_filter = ("channel", "is_active", "created_at")
    search_fields = ("title", "code", "subject_template", "body_template")

@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("recipient", "channel", "subject", "status_badge", "sent_at", "created_at")
    list_filter = ("channel", "status", "created_at")
    search_fields = ("recipient", "subject", "body", "error_message")
    readonly_fields = (
        "recipient",
        "user",
        "channel",
        "template",
        "subject",
        "body",
        "status",
        "error_message",
        "sent_at",
        "context_data",
        "created_at",
        "updated_at",
    )

    @admin.display(description=_("Status"))
    def status_badge(self, obj: NotificationLog) -> str:
        colors = {
            "sent": "#2E7D32",
            "queued": "#0D47A1",
            "failed": "#C62828",
            "read": "#00695C",
        }
        color = colors.get(obj.status, "#767676")
        return format_html(
            '<span style="padding: 3px 8px; background: {}; color: #FFF; font-weight: bold; border-radius: 4px; font-size: 11px;">{}</span>',
            color,
            obj.get_status_display().upper(),
        )

@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "email_notifications", "sms_notifications", "marketing_emails")
    list_filter = ("email_notifications", "sms_notifications", "marketing_emails")
    search_fields = ("user__username", "user__email")
    raw_id_fields = ("user",)