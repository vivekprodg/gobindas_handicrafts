from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .forms import CustomerTaxExemptionForm, TaxClassAdminForm, TaxRuleAdminForm, TaxSettingsAdminForm, TaxZoneAdminForm
from .models import CustomerTaxExemption, TaxClass, TaxRule, TaxSettings, TaxZone

@admin.register(TaxSettings)
class TaxSettingsAdmin(admin.ModelAdmin):
    form = TaxSettingsAdminForm
    list_display = ("__str__", "enable_tax_calculation", "default_calculation_mode", "fallback_tax_rate", "apply_tax_to_shipping")

    def has_add_permission(self, request):
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(TaxClass)
class TaxClassAdmin(admin.ModelAdmin):
    form = TaxClassAdminForm
    list_display = ("name", "code", "is_default", "is_active", "created_at")
    list_filter = ("is_default", "is_active")
    search_fields = ("name", "code", "description")

@admin.register(TaxZone)
class TaxZoneAdmin(admin.ModelAdmin):
    form = TaxZoneAdminForm
    list_display = ("name", "code", "priority", "display_countries", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code", "postal_code_pattern")
    ordering = ("priority", "name")

    @admin.display(description=_("Countries"))
    def display_countries(self, obj: TaxZone) -> str:
        if not obj.countries:
            return _("Global (All Countries)")
        return ", ".join(obj.countries[:5]) + ("..." if len(obj.countries) > 5 else "")

@admin.register(TaxRule)
class TaxRuleAdmin(admin.ModelAdmin):
    form = TaxRuleAdminForm
    list_display = ("name", "tax_class", "tax_zone", "tax_type", "rate_type", "rate_value", "is_compound", "priority", "is_active")
    list_filter = ("tax_type", "rate_type", "is_compound", "is_active", "tax_class", "tax_zone")
    search_fields = ("name", "tax_class__name", "tax_zone__name")
    ordering = ("priority", "name")

@admin.register(CustomerTaxExemption)
class CustomerTaxExemptionAdmin(admin.ModelAdmin):
    form = CustomerTaxExemptionForm
    list_display = ("user", "exemption_number", "reason", "status_badge", "valid_until", "created_at")
    list_filter = ("is_verified", "created_at")
    search_fields = ("user__username", "user__email", "exemption_number", "reason")
    raw_id_fields = ("user",)

    @admin.display(description=_("Verification Status"))
    def status_badge(self, obj: CustomerTaxExemption) -> str:
        if obj.is_valid_now:
            return format_html('<span style="color:#2E7D32; font-weight:bold;">Verified</span>')
        return format_html('<span style="color:#C62828; font-weight:bold;">Pending / Expired</span>')