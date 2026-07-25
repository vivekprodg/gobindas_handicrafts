from typing import Any, List, Optional
from django.core.cache import cache
from django.db import models
from django.db.models import QuerySet
from django.utils import timezone

from . import constants as c
from .models import CustomerTaxExemption, TaxClass, TaxRule, TaxSettings, TaxZone

def get_tax_settings() -> TaxSettings:
    """
    Fetches the singleton TaxSettings instance, utilizing Redis/locmem caching.
    """
    key = c.CACHE_KEY_GLOBAL_SETTINGS.format(ns=c.CACHE_NAMESPACE)
    settings_obj = cache.get(key)
    if settings_obj is None:
        settings_obj, _ = TaxSettings.objects.get_or_create(id=1)
        cache.set(key, settings_obj, c.CACHE_TIMEOUT_TAX_CONFIG)
    return settings_obj

def get_default_tax_class() -> Optional[TaxClass]:
    """
    Returns the default designated TaxClass or the first active TaxClass.
    """
    return TaxClass.objects.filter(is_active=True).order_by("-is_default", "id").first()

def get_tax_class_by_code(code: str) -> Optional[TaxClass]:
    if not code:
        return get_default_tax_class()

    clean_code = str(code).strip().upper()
    tax_class = TaxClass.objects.filter(code=clean_code, is_active=True).first()
    return tax_class or get_default_tax_class()

def match_tax_zone(
    country_code: str = "",
    state_or_province: str = "",
    postal_code: str = "",
) -> Optional[TaxZone]:
    """
    Evaluates geographical destination parameters to find the highest-priority active TaxZone.
    """
    clean_country = str(country_code or "").strip().upper()
    clean_state = str(state_or_province or "").strip().lower()
    clean_zip = str(postal_code or "").strip().upper()

    active_zones = list(
        TaxZone.objects.filter(is_active=True).order_by("priority", "id")
    )

    best_match: Optional[TaxZone] = None

    for zone in active_zones:
        # Check Country Match
        country_match = False
        if not zone.countries:
            country_match = True  # Empty means global zone
        elif clean_country in [str(c).upper() for c in zone.countries]:
            country_match = True

        if not country_match:
            continue

        # Check State/Province Match if defined
        state_match = False
        if not zone.states_or_provinces:
            state_match = True
        elif clean_state in [str(s).lower() for s in zone.states_or_provinces]:
            state_match = True

        if not state_match:
            continue

        # Postal code prefix check
        if zone.postal_code_pattern and clean_zip:
            pattern = zone.postal_code_pattern.replace("*", "").upper()
            if not clean_zip.startswith(pattern):
                continue

        best_match = zone
        break

    return best_match

def get_applicable_tax_rules(tax_class: TaxClass, tax_zone: Optional[TaxZone]) -> QuerySet[TaxRule]:
    """
    Queries active TaxRules matching a specific TaxClass and TaxZone, ordered by priority.
    """
    if not tax_class or not tax_zone:
        return TaxRule.objects.none()

    return TaxRule.objects.filter(
        tax_class=tax_class,
        tax_zone=tax_zone,
        is_active=True,
    ).order_by("priority", "id")

def is_customer_tax_exempt(user: Any) -> bool:
    """
    Checks if an authenticated user possesses a verified active tax exemption certificate.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False

    return CustomerTaxExemption.objects.filter(
        user=user,
        is_verified=True,
    ).filter(
        models.Q(valid_until__isnull=True) | models.Q(valid_until__gte=timezone.now())
    ).exists()