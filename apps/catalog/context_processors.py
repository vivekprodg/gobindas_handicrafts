"""
Global Context Processor for the Catalog application.
Exposes root category trees, top search filter tags, global price boundaries,
and active query parameters globally to all Django templates.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from django.core.cache import cache
from django.http import HttpRequest

from .models import CatalogSettings, Category
from .selectors import get_price_bounds_for_queryset
from .services import (
    CATALOG_CACHE_TIMEOUT,
    CategoryService,
    ProductService,
    get_catalog_settings,
)

logger = logging.getLogger(__name__)

def catalog_context(request: HttpRequest) -> Dict[str, Any]:
    """
    Injects catalog metadata into every template context.
    """
    try:
        categories_hierarchy = cache.get_or_set(
            "catalog:active_categories_hierarchy",
            CategoryService.get_active_categories_hierarchy,
            CATALOG_CACHE_TIMEOUT,
        )

        catalog_settings = cache.get_or_set(
            "catalog:settings",
            get_catalog_settings,
            CATALOG_CACHE_TIMEOUT,
        )

        return {
            "global_categories": categories_hierarchy,
            "catalog_settings": catalog_settings,
            "current_search_query": request.GET.get("q", "").strip(),
            "has_active_filters": any(
                k in request.GET
                for k in (
                    "material",
                    "artisan",
                    "origin",
                    "hue",
                    "ethical",
                    "tag",
                    "collection",
                    "min_price",
                    "price_max",
                    "in_stock_only",
                    "min_rating",
                    "on_sale",
                )
            ),
        }
    except Exception as exc:
        logger.debug("catalog_context processor failed: %s", exc)
        return {
            "global_categories": [],
            "catalog_settings": None,
            "current_search_query": "",
            "has_active_filters": False,
        }