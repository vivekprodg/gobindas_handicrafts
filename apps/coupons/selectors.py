"""
Fast, cached query selectors for reading coupon records and cms configurations.
"""
from __future__ import annotations

from typing import Any, List, Optional
from django.core.cache import cache
from django.db.models import QuerySet

from . import constants as c
from .models import Coupon, CouponCMSSetting, CouponUsageRecord

def get_coupon_cms_settings() -> CouponCMSSetting:
    """
    Fetches the singleton Coupon CMS setting, with local cache support.
    """
    setting = cache.get(c.CACHE_KEY_CMS_SETTINGS)
    if setting is None:
        setting, _ = CouponCMSSetting.objects.get_or_create(id=1)
        cache.set(c.CACHE_KEY_CMS_SETTINGS, setting, c.CACHE_TIMEOUT_PUBLIC)
    return setting

def get_coupon_by_code(code: str, use_cache: bool = True) -> Optional[Coupon]:
    """
    Looks up a coupon by code with case-insensitivity.
    """
    clean_code = str(code or "").strip().upper()
    if not clean_code:
        return None

    cache_key = c.CACHE_KEY_COUPON_DETAIL.format(ns=c.CACHE_NAMESPACE, code=clean_code)
    if use_cache:
        cached_coupon = cache.get(cache_key)
        if cached_coupon is not None:
            return cached_coupon

    coupon = Coupon.objects.filter(code=clean_code).prefetch_related(
        "target_categories",
        "target_products",
        "target_artisans",
        "target_collections",
        "target_customers",
    ).first()

    if coupon and use_cache:
        cache.set(cache_key, coupon, c.CACHE_TIMEOUT_PUBLIC)

    return coupon

def get_public_active_coupons() -> List[Coupon]:
    """
    Retrieves all currently active and public coupons for display in the UI.
    """
    cache_key = c.CACHE_KEY_PUBLIC_COUPONS.format(ns=c.CACHE_NAMESPACE)
    public_coupons = cache.get(cache_key)

    if public_coupons is None:
        public_coupons = list(
            Coupon.objects.public().order_by("-valid_from", "code")
        )
        cache.set(cache_key, public_coupons, c.CACHE_TIMEOUT_PUBLIC)

    return public_coupons

def get_user_coupon_redemption_count(user: Any, coupon: Coupon) -> int:
    """
    Returns total non-reversed redemptions of a specific coupon by a user.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return 0

    return CouponUsageRecord.objects.filter(
        user=user,
        coupon=coupon,
        is_reversed=False,
    ).count()

def get_auto_applicable_coupons() -> QuerySet[Coupon]:
    """
    Returns queryset of currently active auto-apply coupons.
    """
    return Coupon.objects.auto_applicable()