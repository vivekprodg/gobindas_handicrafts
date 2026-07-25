from decimal import Decimal
from typing import Any, List, Optional
from django.core.cache import cache
from django.db import models
from django.db.models import QuerySet
from django.utils import timezone

from . import constants as c
from .models import (
    ShipmentTrackingRecord,
    ShippingMethod,
    ShippingSettings,
    ShippingZone,
)

def get_shipping_settings() -> ShippingSettings:
    """
    Fetches singleton ShippingSettings with caching support.
    """
    key = c.CACHE_KEY_GLOBAL_SETTINGS.format(ns=c.CACHE_NAMESPACE)
    settings_obj = cache.get(key)
    if settings_obj is None:
        settings_obj, _ = ShippingSettings.objects.get_or_create(id=1)
        cache.set(key, settings_obj, c.CACHE_TIMEOUT_SHIPPING)
    return settings_obj

def match_shipping_zone(
    country_code: str = "NP",
    state_or_province: str = "",
) -> Optional[ShippingZone]:
    """
    Finds the highest-priority active ShippingZone matching destination country and region.
    """
    clean_country = str(country_code or "NP").strip().upper()
    clean_state = str(state_or_province or "").strip().lower()

    active_zones = list(
        ShippingZone.objects.filter(is_active=True).order_by("priority", "id")
    )

    for zone in active_zones:
        # 1. Country Check
        country_match = False
        if not zone.countries:
            country_match = True  # Global zone
        elif clean_country in [str(code).upper() for c in zone.countries for code in (c if isinstance(c, list) else [c])]:
            country_match = True

        if not country_match:
            continue

        # 2. State Check if defined
        state_match = False
        if not zone.states_or_provinces:
            state_match = True
        elif clean_state in [str(st).lower() for st in zone.states_or_provinces]:
            state_match = True

        if not state_match:
            continue

        return zone

    return None

def get_eligible_shipping_methods(
    zone: Optional[ShippingZone],
    subtotal: Decimal = c.ZERO_DECIMAL,
    total_weight_kg: Decimal = c.ZERO_DECIMAL,
) -> List[ShippingMethod]:
    """
    Returns active delivery methods available for a zone, subtotal, and weight parcel.
    """
    if not zone:
        return []

    methods = ShippingMethod.objects.filter(
        zone=zone,
        is_active=True,
        min_order_subtotal__lte=subtotal,
    ).prefetch_related("weight_tiers").order_by("position", "id")

    eligible: List[ShippingMethod] = []
    for method in list(methods):
        if method.max_weight_kg and total_weight_kg > method.max_weight_kg:
            continue
        eligible.append(method)

    return eligible

def get_shipping_method_by_code(code: str) -> Optional[ShippingMethod]:
    if not code:
        return None
    return ShippingMethod.objects.filter(
        code=str(code).strip().upper(),
        is_active=True,
    ).select_related("zone").first()

def get_tracking_by_number(tracking_number: str) -> Optional[ShipmentTrackingRecord]:
    if not tracking_number:
        return None
    return ShipmentTrackingRecord.objects.filter(
        tracking_number=str(tracking_number).strip(),
    ).select_related("order").first()