"""
Enterprise-grade Django Signals for the Catalog application.
Manages automatic cache invalidation and audit logging across catalog models.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.core.cache import cache
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.homepage.services import invalidate_homepage_cache

from .models import (
    Artisan,
    CatalogSettings,
    Category,
    EthicalStandard,
    Hue,
    Material,
    Product,
    ProductCollection,
    ProductTag,
    ProductVariant,
    RecentlyViewedProduct,
)
from .services import invalidate_catalog_cache

logger = logging.getLogger(__name__)

_state_lock = threading.local()

def _safe_log(scope: str, exc: Exception, **extra: Any) -> None:
    try:
        logger.error(
            "Catalog signal failure [%s]: %s | extra=%s",
            scope,
            exc,
            extra,
            exc_info=True,
        )
    except Exception:
        pass

def _safe_cache_key(prefix: str, *parts: Any) -> str:
    safe_parts = [prefix]
    for part in parts:
        if part is None:
            safe_parts.append("none")
        else:
            try:
                safe_parts.append(str(part))
            except Exception:
                safe_parts.append("unknown")
    return ":".join(safe_parts)

def _safe_cache_delete_many(keys: list) -> None:
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception as exc:
        _safe_log("cache.delete_many", exc, keys=keys)

@receiver([post_save, post_delete], sender=Product)
@receiver([post_save, post_delete], sender=Category)
@receiver([post_save, post_delete], sender=Artisan)
@receiver([post_save, post_delete], sender=Material)
@receiver([post_save, post_delete], sender=Hue)
@receiver([post_save, post_delete], sender=EthicalStandard)
@receiver([post_save, post_delete], sender=CatalogSettings)
def handle_catalog_cms_change(sender, instance, **kwargs):
    if kwargs.get("raw", False):
        return

    keys_to_invalidate: list = []

    try:
        if isinstance(instance, Product):
            if getattr(instance, "slug", None):
                keys_to_invalidate.append(_safe_cache_key("catalog:product:slug", instance.slug))
            if getattr(instance, "pk", None):
                keys_to_invalidate.append(_safe_cache_key("catalog:product:id", instance.pk))
            if getattr(instance, "category", None) and instance.category is not None:
                category_slug = getattr(instance.category, "slug", None)
                if category_slug:
                    keys_to_invalidate.append(_safe_cache_key("catalog:cat:slug", category_slug))

        elif isinstance(instance, Category):
            if getattr(instance, "slug", None):
                keys_to_invalidate.append(_safe_cache_key("catalog:cat:slug", instance.slug))
            keys_to_invalidate.append("catalog:active_categories_hierarchy")

        elif isinstance(instance, Artisan):
            if getattr(instance, "slug", None):
                keys_to_invalidate.append(_safe_cache_key("catalog:artisan:slug", instance.slug))

        elif isinstance(instance, CatalogSettings):
            keys_to_invalidate.append("catalog:settings")

        keys_to_invalidate.extend([
            "catalog:trending_products",
            "catalog:popular_products",
            "catalog:new_arrivals",
            "catalog:active_categories_hierarchy",
        ])

        _safe_cache_delete_many(keys_to_invalidate)
        try:
            invalidate_catalog_cache()
        except Exception as exc:
            _safe_log("invalidate_catalog_cache", exc)
        try:
            invalidate_homepage_cache()
        except Exception as exc:
            _safe_log("invalidate_homepage_cache", exc)

    except Exception as exc:
        _safe_log("handle_catalog_cms_change", exc, sender=getattr(sender, "__name__", None))

@receiver(m2m_changed, sender=Product.tags.through)
@receiver(m2m_changed, sender=Product.in_collections.through)
@receiver(m2m_changed, sender=Product.ethical_standards.through)
def handle_product_m2m_facet_invalidation(sender, instance, action, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    try:
        invalidate_catalog_cache()
        cache.delete("catalog:active_categories_hierarchy")
    except Exception as exc:
        _safe_log("handle_product_m2m_facet_invalidation", exc)

@receiver(post_save, sender=ProductVariant)
def handle_product_variant_catalog_change(sender, instance, created, **kwargs):
    if kwargs.get("raw", False):
        return

    keys_to_invalidate: list = []

    try:
        if getattr(instance, "product_id", None):
            keys_to_invalidate.append(_safe_cache_key("catalog:product", instance.product_id, "variants"))
        _safe_cache_delete_many(keys_to_invalidate)
        try:
            invalidate_catalog_cache()
        except Exception as exc:
            _safe_log("invalidate_catalog_cache", exc)

    except Exception as exc:
        _safe_log("handle_product_variant_catalog_change", exc)

@receiver(post_save, sender=RecentlyViewedProduct)
def handle_recently_viewed_cache_invalidation(sender, instance, created, **kwargs):
    if not created:
        return

    keys_to_invalidate: list = []
    try:
        if getattr(instance, "user_id", None):
            keys_to_invalidate.append(_safe_cache_key("catalog:recently_viewed:user", instance.user_id))
        if getattr(instance, "session_key", None):
            keys_to_invalidate.append(_safe_cache_key("catalog:recently_viewed:session", instance.session_key))
        _safe_cache_delete_many(keys_to_invalidate)
    except Exception as exc:
        _safe_log("handle_recently_viewed_cache_invalidation", exc)

@receiver(post_save, sender=Product)
@receiver(post_delete, sender=Product)
def handle_catalog_audit_log(sender, instance, created=False, **kwargs):
    if kwargs.get("raw", False):
        return
    try:
        action = "created" if created else "updated"
        logger.info(
            "catalog.audit | action=%s model=%s pk=%s",
            action,
            sender.__name__,
            getattr(instance, "pk", None),
        )
    except Exception:
        pass

__all__ = [
    "handle_catalog_cms_change",
    "handle_product_m2m_facet_invalidation",
    "handle_product_variant_catalog_change",
    "handle_recently_viewed_cache_invalidation",
    "handle_catalog_audit_log",
]