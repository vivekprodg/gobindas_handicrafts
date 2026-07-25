from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .forms import PaymentSettingsAdminForm
from .models import PaymentSettings, PaymentTransaction, PaymentWebhookLog

@admin.register(PaymentSettings)
class PaymentSettingsAdmin(admin.ModelAdmin):
    form = PaymentSettingsAdminForm
    list_display = (
        "__str__",
        "enable_esewa",
        "enable_khalti",
        "enable_stripe",
        "enable_cod",
        "enable_bank_transfer",
    )

    def has_add_permission(self, request):
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_id",
        "order",
        "gateway",
        "amount",
        "currency",
        "status_badge",
        "paid_at",
        "created_at",
    )
    list_filter = ("gateway", "status", "created_at")
    search_fields = ("transaction_id", "order__order_number", "gateway_reference_id")
    readonly_fields = ("transaction_id", "order", "amount", "currency", "raw_request_payload", "raw_response_payload", "created_at", "updated_at")
    raw_id_fields = ("order",)

    @admin.display(description=_("Status"))
    def status_badge(self, obj: PaymentTransaction) -> str:
        colors = {
            "success": "#2E7D32",
            "initiated": "#0D47A1",
            "pending": "#9A7B54",
            "failed": "#C62828",
        }
        color = colors.get(obj.status, "#767676")
        return format_html(
            '<span style="padding: 3px 8px; background: {}; color: #FFF; font-weight: bold; border-radius: 4px; font-size: 11px;">{}</span>',
            color,
            obj.get_status_display().upper(),
        )

@admin.register(PaymentWebhookLog)
class PaymentWebhookLogAdmin(admin.ModelAdmin):
    list_display = ("gateway", "is_processed", "created_at")
    list_filter = ("gateway", "is_processed", "created_at")
    readonly_fields = ("gateway", "payload", "is_processed", "error_message", "created_at")