from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import AuditLog, SecurityEventLog

@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "actor", "action", "severity_badge", "target_repr", "ip_address")
    list_filter = ("action", "severity", "timestamp")
    search_fields = ("actor__username", "actor__email", "target_repr", "ip_address")
    readonly_fields = ("actor", "action", "severity", "content_type", "object_id", "target_repr", "changes", "ip_address", "user_agent", "timestamp", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Severity"))
    def severity_badge(self, obj: AuditLog) -> str:
        colors = {
            "info": "#0D47A1",
            "warning": "#9A7B54",
            "security": "#C62828",
            "critical": "#B71C1C",
        }
        color = colors.get(obj.severity, "#767676")
        return format_html(
            '<span style="padding: 3px 8px; background: {}; color: #FFF; font-weight: bold; border-radius: 4px; font-size: 11px;">{}</span>',
            color,
            obj.get_severity_display().upper(),
        )

@admin.register(SecurityEventLog)
class SecurityEventLogAdmin(admin.ModelAdmin):
    list_display = ("event_type", "ip_address", "user", "is_resolved", "created_at")
    list_filter = ("is_resolved", "created_at")
    search_fields = ("event_type", "ip_address", "user__username")
    readonly_fields = ("event_type", "ip_address", "user", "details", "created_at", "updated_at")
    actions = ["mark_resolved"]

    @admin.action(description=_("Mark selected security events as resolved"))
    def mark_resolved(self, request, queryset):
        queryset.update(is_resolved=True)
        self.message_user(request, _("Selected events marked as resolved."))