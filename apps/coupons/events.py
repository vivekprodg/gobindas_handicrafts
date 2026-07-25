"""
Event handlers and cache invalidation listeners for the coupons app.
"""
from __future__ import annotations

import logging
from typing import Any
from django.core.cache import cache

from . import constants as c

logger = logging.getLogger(c.LOGGER_NAME)

def invalidate_coupon_caches(coupon_code: str = "") -> None:
    """
    Invalidates all cached coupon data in memory and Redis.
    """
    try:
        cache.delete(c.CACHE_KEY_PUBLIC_COUPONS.format(ns=c.CACHE_NAMESPACE))
        cache.delete(c.CACHE_KEY_CMS_SETTINGS.format(ns=c.CACHE_NAMESPACE))
        if coupon_code:
            clean_code = str(coupon_code).strip().upper()
            cache.delete(c.CACHE_KEY_COUPON_DETAIL.format(ns=c.CACHE_NAMESPACE, code=clean_code))
    except Exception as exc:
        logger.warning("Cache invalidation failed for coupons: %s", exc)

def handle_coupon_saved(coupon: Any) -> None:
    """
    Signal hook triggered when a coupon instance is saved.
    """
    if coupon and getattr(coupon, "code", None):
        invalidate_coupon_caches(coupon.code)

def handle_coupon_deleted(coupon: Any) -> None:
    """
    Signal hook triggered when a coupon instance is deleted.
    """
    if coupon and getattr(coupon, "code", None):
        invalidate_coupon_caches(coupon.code)