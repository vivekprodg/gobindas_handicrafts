import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from django.utils.translation import gettext_lazy as _

from . import constants as c
from .models import ShipmentTrackingRecord, ShippingMethod, ShippingSettings, WeightTierRate
from .selectors import (
    get_eligible_shipping_methods,
    get_shipping_method_by_code,
    get_shipping_settings,
    get_tracking_by_number,
    match_shipping_zone,
)

logger = logging.getLogger(c.LOGGER_NAME)

class ShippingCalculationService:
    """
    Core engine calculating real-time delivery rates, parcel weights,
    and carrier options for carts and orders.
    """

    @classmethod
    def calculate_cart_weight(cls, cart: Any) -> Decimal:
        """
        Sums line-item weight in kilograms across active cart items.
        """
        if not cart or not getattr(cart, "pk", None):
            return c.ZERO_DECIMAL

        active_items = cart.items.filter(status="active").select_related("product", "variant")
        total_weight = c.ZERO_DECIMAL

        for item in active_items:
            item_weight = c.ZERO_DECIMAL
            if item.variant and getattr(item.variant, "weight", None):
                item_weight = item.variant.weight
            elif item.product and getattr(item.product, "weight", None):
                item_weight = item.product.weight

            total_weight += Decimal(item_weight or 0) * Decimal(item.quantity)

        return total_weight.quantize(Decimal("0.001"))

    @classmethod
    def calculate_method_fee(
        cls,
        method: ShippingMethod,
        subtotal: Decimal,
        total_weight_kg: Decimal,
    ) -> Decimal:
        """
        Calculates exact delivery fee for a specific ShippingMethod.
        """
        shipping_settings = get_shipping_settings()

        # Check Global Free Shipping Threshold
        if (
            shipping_settings.free_shipping_subtotal_threshold
            and subtotal >= shipping_settings.free_shipping_subtotal_threshold
        ):
            return c.ZERO_DECIMAL

        if method.rate_type == c.ShippingRateType.FREE_SHIPPING:
            return c.ZERO_DECIMAL

        if method.rate_type == c.ShippingRateType.LOCAL_PICKUP:
            return c.ZERO_DECIMAL

        if method.rate_type == c.ShippingRateType.FLAT_RATE:
            return method.flat_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if method.rate_type == c.ShippingRateType.WEIGHT_BASED:
            tiers = list(method.weight_tiers.all())
            for tier in tiers:
                if tier.min_weight_kg <= total_weight_kg <= tier.max_weight_kg:
                    return tier.rate_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            # Fallback to flat_rate if outside configured tiers
            return method.flat_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        return method.flat_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @classmethod
    def calculate_shipping_options(
        cls,
        cart: Any,
        country_code: str = "NP",
        state_or_province: str = "",
        selected_method_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates destination address and returns all eligible shipping methods with computed fees.
        """
        shipping_settings = get_shipping_settings()
        if not shipping_settings.enable_shipping_calculation:
            return {
                "success": True,
                "shipping_fee": c.ZERO_DECIMAL,
                "selected_method": None,
                "available_methods": [],
                "is_free_shipping": True,
                "message": str(_("Shipping calculation disabled.")),
            }

        subtotal = cart.subtotal if cart else c.ZERO_DECIMAL
        total_weight_kg = cls.calculate_cart_weight(cart)

        zone = match_shipping_zone(country_code, state_or_province)
        eligible_methods = get_eligible_shipping_methods(zone, subtotal, total_weight_kg)

        methods_payload: List[Dict[str, Any]] = []
        selected_method_payload: Optional[Dict[str, Any]] = None
        selected_fee = shipping_settings.default_fallback_rate

        for method in eligible_methods:
            fee = cls.calculate_method_fee(method, subtotal, total_weight_kg)
            payload = {
                "id": method.pk,
                "name": method.name,
                "code": method.code,
                "carrier": method.carrier,
                "carrier_display": method.get_carrier_display(),
                "fee": str(fee),
                "is_free": fee == c.ZERO_DECIMAL,
                "estimated_delivery": method.estimated_delivery_text,
            }
            methods_payload.append(payload)

            if selected_method_code and method.code == selected_method_code.upper():
                selected_method_payload = payload
                selected_fee = fee

        if not selected_method_payload and methods_payload:
            selected_method_payload = methods_payload[0]
            selected_fee = Decimal(methods_payload[0]["fee"])

        return {
            "success": True,
            "shipping_fee": selected_fee,
            "total_weight_kg": str(total_weight_kg),
            "zone_code": zone.code if zone else "GLOBAL",
            "selected_method": selected_method_payload,
            "available_methods": methods_payload,
            "is_free_shipping": selected_fee == c.ZERO_DECIMAL,
        }