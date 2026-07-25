from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from .forms import (
    ShipmentTrackingRecord,
    ShippingMethodAdminForm,
    ShippingSettingsAdminForm,
    ShippingZoneAdminForm,
    WeightTierRateAdminForm,
)
from .models import ShippingMethod, ShippingSettings, ShippingZone, WeightTierRate

class WeightTierRateInline(admin.TabularInline):
    model = WeightTierRate
    form = WeightTierRateAdminForm
    extra = 1
    fields = ("min_weight_kg", "max_weight_kg", "rate_amount")

@admin.register(ShippingSettings)
class ShippingSettingsAdmin(admin.ModelAdmin):
    form = ShippingSettingsAdminForm
    list_display = (
        "__str__",
        "enable_shipping_calculation",
        "default_fallback_rate",
        "free_shipping_subtotal_threshold",
        "origin_country_code",
    )

    def has_add_permission(self, request):
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False

@admin.register(ShippingZone)
class ShippingZoneAdmin(admin.ModelAdmin):
    form = ShippingZoneAdminForm
    list_display = ("name", "code", "priority", "display_countries", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")

    @admin.display(description=_("Countries"))
    def display_countries(self, obj: ShippingZone) -> str:
        if not obj.countries:
            return _("Global (All Countries)")
        return ", ".join(obj.countries[:6])

@admin.register(ShippingMethod)
class ShippingMethodAdmin(admin.ModelAdmin):
    form = ShippingMethodAdminForm
    list_display = (
        "name",
        "code",
        "carrier",
        "zone",
        "rate_type",
        "flat_rate",
        "estimated_delivery_text",
        "is_active",
    )
    list_filter = ("carrier", "rate_type", "is_active", "zone")
    search_fields = ("name", "code")
    inlines = [WeightTierRateInline]

@admin.register(ShipmentTrackingRecord)
class ShipmentTrackingRecordAdmin(admin.ModelAdmin):
    list_display = ("tracking_number", "carrier", "order", "current_status", "estimated_delivery", "updated_at")
    list_filter = ("carrier", "current_status", "updated_at")
    search_fields = ("tracking_number", "order__order_number", "current_status")
    raw_id_fields = ("order",)