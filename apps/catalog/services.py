from __future__ import annotations

from decimal import Decimal
from typing import Any
from django.core.cache import cache
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import QuerySet, Min, Max
from .models import (
    CatalogSettings,
    Category,
    Product,
    Artisan,
    Material,
    Hue,
    EthicalStandard,
)

CATALOG_CACHE_VERSION = 1
CATALOG_CACHE_TIMEOUT = 60 * 30  # 30 minutes
CATALOG_LIST_CACHE_PREFIX = "catalog:list:"

def invalidate_catalog_cache() -> None:
    """
    Clears all catalog-related caches.
    This should be called from Signals on Product/Category/Artisan save/delete.
    """
    # Simple pattern: clear all cache keys containing the catalog namespace
    # Or rely on standard django cache.clear() if needed, but standard is delete_pattern or manual key tracking.
    # Since standard backend might not support delete_pattern, we clear catalog pages or clear cache.
    cache.clear()

def get_catalog_settings() -> CatalogSettings:
    """
    Retrieves the singleton CatalogSettings instance.
    """
    settings = CatalogSettings.objects.first()
    if not settings:
        settings = CatalogSettings.objects.create(
            default_items_per_page=9,
            price_filter_min=500,
            price_filter_max=100000,
            show_stock_warning_threshold=5
        )
    return settings

def get_category_by_slug(slug: str) -> Category | None:
    try:
        return Category.objects.filter(slug=slug, is_active=True).select_related('parent').first()
    except Category.DoesNotExist:
        return None

def get_active_categories_hierarchy() -> list[dict[str, Any]]:
    """
    Compiles category hierarchy tree dynamically for sidebar filtering.
    """
    top_categories = Category.objects.filter(parent=None, is_active=True).prefetch_related('subcategories')
    hierarchy = []
    for cat in top_categories:
        subcats = [{"name": sub.name, "slug": sub.slug} for sub in cat.subcategories.filter(is_active=True)]
        hierarchy.append({
            "name": cat.name,
            "slug": cat.slug,
            "subcategories": subcats
        })
    return hierarchy

def query_products_for_category(
    category: Category,
    *,
    sort_by: str = "featured",
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    selected_materials: list[str] | None = None,
    selected_artisans: list[str] | None = None,
    selected_origins: list[str] | None = None,
    selected_hues: list[str] | None = None,
    selected_ethical_standards: list[str] | None = None,
) -> QuerySet[Product]:
    """
    Builds the filtered, sorted product QuerySet for a given Category (including subcategories).
    """
    # 1. Resolve subcategories
    category_ids = [category.id]
    if not category.parent:
        sub_ids = list(category.subcategories.filter(is_active=True).values_list('id', flat=True))
        category_ids.extend(sub_ids)

    # 2. Base Active QuerySet
    qs = Product.objects.filter(category_id__in=category_ids, is_active=True).select_related(
        'category', 'artisan', 'material', 'hue'
    ).prefetch_related('ethical_standards')

    # 3. Apply Filters
    if min_price is not None:
        qs = qs.filter(price__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price__leq=max_price)
    if selected_materials:
        qs = qs.filter(material__name__in=selected_materials)
    if selected_artisans:
        qs = qs.filter(artisan__slug__in=selected_artisans)
    if selected_origins:
        qs = qs.filter(artisan__region__in=selected_origins)
    if selected_hues:
        qs = qs.filter(hue__name__in=selected_hues)
    if selected_ethical_standards:
        qs = qs.filter(ethical_standards__name__in=selected_ethical_standards)

    # 4. Apply Sorting
    if sort_by == "newest":
        qs = qs.order_by("-created_at", "position")
    elif sort_by == "price-low":
        qs = qs.order_by("price", "position")
    elif sort_by == "price-high":
        qs = qs.order_by("-price", "position")
    elif sort_by == "rating":
        qs = qs.order_by("-rating", "position")
    else: # Default is "featured" (position then created_at)
        qs = qs.order_by("position", "-created_at")

    return qs.distinct()

def get_sidebar_filter_metadata(category: Category, current_selections: dict[str, Any]) -> dict[str, Any]:
    """
    Compiles distinct filter values from all active products in this category tree
    to populate sidebar options dynamically.
    """
    category_ids = [category.id]
    if not category.parent:
        category_ids.extend(list(category.subcategories.filter(is_active=True).values_list('id', flat=True)))

    base_qs = Product.objects.filter(category_id__in=category_ids, is_active=True)

    # Fetch distinct criteria from matching products
    materials = list(
        Material.objects.filter(products__in=base_qs).distinct().values_list('name', flat=True)
    )
    artisans = list(
        Artisan.objects.filter(products__in=base_qs, is_active=True).distinct().values('name', 'slug')
    )
    origins = list(
        base_qs.exclude(artisan__region="").values_list('artisan__region', flat=True).distinct()
    )
    hues = list(
        Hue.objects.filter(products__in=base_qs).distinct().values('name', 'color_code')
    )
    ethical_standards = list(
        EthicalStandard.objects.filter(products__in=base_qs).distinct().values_list('name', flat=True)
    )

    # Aggregate min/max prices
    price_stats = base_qs.aggregate(min_p=Min('price'), max_p=Max('price'))
    min_price_found = int(price_stats['min_p'] or 0)
    max_price_found = int(price_stats['max_p'] or 0)

    # Resolve active/checked statuses
    return {
        "categories": get_active_categories_hierarchy(),
        "materials": [
            {"name": mat, "checked": mat in current_selections.get('materials', [])}
            for mat in materials
        ],
        "artisans": [
            {"name": art['name'], "slug": art['slug'], "checked": art['slug'] in current_selections.get('artisans', [])}
            for art in artisans
        ],
        "origins": [
            {"name": orig, "checked": orig in current_selections.get('origins', [])}
            for orig in origins
        ],
        "hues": [
            {"name": hue['name'], "color": hue['color_code'], "checked": hue['name'] in current_selections.get('hues', [])}
            for hue in hues
        ],
        "ethical": [
            {"name": std, "checked": std in current_selections.get('ethical', [])}
            for std in ethical_standards
        ],
        "price_bounds": {
            "min": min_price_found,
            "max": max_price_found,
        }
    }

def paginate_products(products_qs: QuerySet[Product], page_number: str | int, items_per_page: int) -> Any:
    paginator = Paginator(products_qs, items_per_page)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    return page_obj