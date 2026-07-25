"""
Enterprise-grade Read Selectors and Dynamic Facet Aggregation Engine for Catalog.
Isolates read-only database lookups, group-by Count aggregations, and price boundary queries.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

from django.db.models import Count, F, Max, Min, Q, QuerySet

from .models import (
    Artisan,
    Category,
    EthicalStandard,
    Hue,
    Material,
    Product,
    ProductCollection,
    ProductTag,
    ProductVariant,
)

logger = logging.getLogger(__name__)

def get_base_product_queryset() -> QuerySet[Product]:
    """
    Returns base published product queryset with standardized joins and optimizations.
    """
    return Product.objects.published().select_related(
        "category",
        "category__parent",
        "artisan",
        "material",
        "hue",
    ).prefetch_related(
        "ethical_standards",
        "tags",
        "in_collections",
        "highlights",
        "trust_badges",
        "labels",
        "icons",
        "variants",
    )

def get_filtered_products_queryset(
    qs: Optional[QuerySet[Product]] = None,
    *,
    category: Optional[Category] = None,
    min_price: Optional[Decimal] = None,
    max_price: Optional[Decimal] = None,
    in_stock_only: bool = False,
    min_rating: Optional[Union[int, str]] = None,
    selected_materials: Optional[List[str]] = None,
    selected_artisans: Optional[List[str]] = None,
    selected_origins: Optional[List[str]] = None,
    selected_hues: Optional[List[str]] = None,
    selected_ethical_standards: Optional[List[str]] = None,
    selected_tags: Optional[List[str]] = None,
    selected_collections: Optional[List[str]] = None,
    on_sale_only: bool = False,
    min_discount_pct: Optional[Union[int, str, Decimal]] = None,
    variant_attributes: Optional[Dict[str, List[str]]] = None,
    search_query: Optional[str] = None,
    sort_by: str = "featured",
) -> QuerySet[Product]:
    """
    Central selector function for filtering a Product queryset across all criteria.
    """
    if qs is None:
        qs = get_base_product_queryset()

    if category is not None:
        qs = qs.in_category(category)

    if search_query and str(search_query).strip():
        qs = qs.search(search_query)

    if min_price is not None or max_price is not None:
        qs = qs.by_price_range(min_price=min_price, max_price=max_price)

    if in_stock_only:
        qs = qs.in_stock_only()

    if min_rating:
        qs = qs.by_rating(min_rating)

    if selected_materials:
        mats = [m for m in selected_materials if m]
        if mats:
            qs = qs.filter(material__name__in=mats)

    if selected_artisans:
        arts = [a for a in selected_artisans if a]
        if arts:
            qs = qs.filter(Q(artisan__slug__in=arts) | Q(artisan__name__in=arts))

    if selected_origins:
        origins = [o for o in selected_origins if o]
        if origins:
            qs = qs.filter(artisan__region__in=origins)

    if selected_hues:
        hues = [h for h in selected_hues if h]
        if hues:
            qs = qs.filter(hue__name__in=hues)

    if selected_ethical_standards:
        standards = [s for s in selected_ethical_standards if s]
        if standards:
            qs = qs.with_ethical_standards(*EthicalStandard.objects.filter(name__in=standards))

    if selected_tags:
        qs = qs.by_tags(selected_tags)

    if selected_collections:
        qs = qs.by_collections(selected_collections)

    if on_sale_only or min_discount_pct:
        if min_discount_pct:
            qs = qs.by_discount_percentage(min_discount_pct)
        else:
            qs = qs.on_sale()

    if variant_attributes:
        qs = qs.by_variant_attributes(variant_attributes)

    # Sorting
    allowed_sorts = {
        "featured": ("position", "-created_at"),
        "newest": ("-created_at", "position"),
        "oldest": ("created_at", "position"),
        "price_low": ("price", "position"),
        "price-asc": ("price", "position"),
        "price_high": ("-price", "position"),
        "price-desc": ("-price", "position"),
        "rating": ("-rating", "position"),
        "popularity": ("-wishlist_count", "-view_count"),
        "name_asc": ("title", "position"),
        "name-asc": ("title", "position"),
        "name_desc": ("-title", "position"),
        "name-desc": ("-title", "position"),
    }
    ordering = allowed_sorts.get(sort_by, ("position", "-created_at"))
    return qs.order_by(*ordering).distinct()

def get_price_bounds_for_queryset(qs: QuerySet[Product]) -> Dict[str, float]:
    """
    Computes true minimum and maximum price boundaries for the current product selection.
    """
    try:
        stats = qs.aggregate(min_p=Min("price"), max_p=Max("price"))
        min_p = float(stats["min_p"] or 0)
        max_p = float(stats["max_p"] or 0)
        return {"min": min_p, "max": max_p}
    except Exception as exc:
        logger.debug("Failed to calculate price bounds: %s", exc)
        return {"min": 0.0, "max": 0.0}

def get_facet_counts_for_queryset(
    qs: QuerySet[Product],
    *,
    category: Optional[Category] = None,
    current_selections: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generates dynamic group-by item counts (facets) for the given queryset.
    Returns structured lists with labels, values, counts, and selection states.
    """
    current_selections = current_selections or {}

    try:
        # Category Facets
        if category and not category.parent_id:
            cat_qs = category.subcategories.filter(is_active=True)
        else:
            cat_qs = Category.objects.filter(parent__isnull=True, is_active=True)

        categories_data = []
        for cat in cat_qs:
            count = qs.filter(Q(category=cat) | Q(category__parent=cat)).distinct().count()
            categories_data.append({
                "id": cat.id,
                "name": cat.name,
                "slug": cat.slug,
                "count": count,
                "checked": cat.slug == (category.slug if category else ""),
            })

        # Materials
        materials_qs = Material.objects.filter(products__in=qs).annotate(
            item_count=Count("products", filter=Q(products__in=qs), distinct=True)
        ).order_by("-item_count", "name")
        materials_data = [
            {
                "name": m.name,
                "count": m.item_count,
                "checked": m.name in current_selections.get("materials", []),
            }
            for m in materials_qs
        ]

        # Artisans
        artisans_qs = Artisan.objects.filter(products__in=qs, is_active=True).annotate(
            item_count=Count("products", filter=Q(products__in=qs), distinct=True)
        ).order_by("-item_count", "name")
        artisans_data = [
            {
                "name": a.name,
                "slug": a.slug,
                "count": a.item_count,
                "checked": a.slug in current_selections.get("artisans", []),
            }
            for a in artisans_qs
        ]

        # Origins / Regions
        origins_raw = qs.exclude(artisan__region="").values("artisan__region").annotate(
            item_count=Count("id", distinct=True)
        ).order_by("-item_count")
        origins_data = [
            {
                "name": o["artisan__region"],
                "count": o["item_count"],
                "checked": o["artisan__region"] in current_selections.get("origins", []),
            }
            for o in origins_raw if o["artisan__region"]
        ]

        # Hues / Colors
        hues_qs = Hue.objects.filter(products__in=qs).annotate(
            item_count=Count("products", filter=Q(products__in=qs), distinct=True)
        ).order_by("-item_count", "name")
        hues_data = [
            {
                "name": h.name,
                "color": h.color_code,
                "count": h.item_count,
                "checked": h.name in current_selections.get("hues", []),
            }
            for h in hues_qs
        ]

        # Ethical Standards
        ethical_qs = EthicalStandard.objects.filter(products__in=qs, is_active=True).annotate(
            item_count=Count("products", filter=Q(products__in=qs), distinct=True)
        ).order_by("-item_count", "name")
        ethical_data = [
            {
                "name": e.name,
                "count": e.item_count,
                "checked": e.name in current_selections.get("ethical", []),
            }
            for e in ethical_qs
        ]

        # Product Tags
        tags_qs = ProductTag.objects.filter(products__in=qs, is_active=True).annotate(
            item_count=Count("products", filter=Q(products__in=qs), distinct=True)
        ).order_by("-item_count", "name")[:20]
        tags_data = [
            {
                "id": t.id,
                "name": t.name,
                "slug": t.slug,
                "count": t.item_count,
                "checked": t.slug in current_selections.get("tags", []),
            }
            for t in tags_qs
        ]

        # Collections
        collections_qs = ProductCollection.objects.filter(products__in=qs, is_active=True).annotate(
            item_count=Count("products", filter=Q(products__in=qs), distinct=True)
        ).order_by("-item_count", "name")
        collections_data = [
            {
                "id": col.id,
                "name": col.name,
                "slug": col.slug,
                "count": col.item_count,
                "checked": col.slug in current_selections.get("collections", []),
            }
            for col in collections_qs
        ]

        # Star Rating Facets
        rating_counts = {}
        for r in range(5, 0, -1):
            rating_counts[r] = qs.filter(rating__gte=r).count()

        # In-Stock Count
        in_stock_count = qs.in_stock_only().count()
        on_sale_count = qs.on_sale().count()

        price_bounds = get_price_bounds_for_queryset(qs)

        return {
            "categories": categories_data,
            "materials": materials_data,
            "artisans": artisans_data,
            "origins": origins_data,
            "hues": hues_data,
            "ethical": ethical_data,
            "tags": tags_data,
            "collections": collections_data,
            "rating_counts": rating_counts,
            "in_stock_count": in_stock_count,
            "on_sale_count": on_sale_count,
            "price_bounds": price_bounds,
        }
    except Exception as exc:
        logger.exception("Failed to build facet counts: %s", exc)
        return {
            "categories": [],
            "materials": [],
            "artisans": [],
            "origins": [],
            "hues": [],
            "ethical": [],
            "tags": [],
            "collections": [],
            "rating_counts": {5: 0, 4: 0, 3: 0, 2: 0, 1: 0},
            "in_stock_count": 0,
            "on_sale_count": 0,
            "price_bounds": {"min": 0.0, "max": 0.0},
        }

__all__ = [
    "get_base_product_queryset",
    "get_filtered_products_queryset",
    "get_price_bounds_for_queryset",
    "get_facet_counts_for_queryset",
]