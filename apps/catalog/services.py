"""
Enterprise-grade service layer for the Catalog application.
Provides domain services for products, taxonomy, recommendations, and search/filtering pipelines.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from django.core.cache import cache
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import models
from django.db.models import (
    Case,
    Count,
    Max,
    Min,
    Prefetch,
    Q,
    QuerySet,
    When,
)

from .models import (
    Artisan,
    CatalogSettings,
    Category,
    EthicalStandard,
    Hue,
    Material,
    Product,
    ProductCollection,
    ProductFAQ,
    ProductSpecification,
    ProductTag,
    ProductVariant,
    ProductVideo,
    RecentlyViewedProduct,
)
from .selectors import (
    get_facet_counts_for_queryset,
    get_filtered_products_queryset,
    get_price_bounds_for_queryset,
)

logger = logging.getLogger(__name__)

CATALOG_CACHE_VERSION = 1
CATALOG_CACHE_TIMEOUT = 60 * 30
CATALOG_LIST_CACHE_PREFIX = "catalog:list:"

def invalidate_catalog_cache() -> None:
    try:
        cache.clear()
    except Exception as exc:
        logger.warning("Failed to clear catalog cache: %s", exc)

def _get_inventory_models() -> Tuple:
    try:
        from apps.inventory.models import (
            Inventory,
            InventoryTransaction,
            StockReservation,
            Warehouse,
        )
        return Inventory, InventoryTransaction, StockReservation, Warehouse
    except ImportError:
        logger.warning(
            "Inventory models could not be imported. Catalog services operating in fallback mode."
        )
        return ()

def _get_inventory_selectors() -> Optional[Any]:
    try:
        from apps.inventory import selectors
        return selectors
    except ImportError:
        logger.warning("Inventory selectors could not be imported.")
        return None

def _get_inventory_services() -> Optional[Any]:
    try:
        from apps.inventory import services
        return services
    except ImportError:
        logger.warning("Inventory services could not be imported.")
        return None

def _safe_decimal(value: Any, *, allow_none: bool = True) -> Decimal:
    if value is None:
        return Decimal("0.00")
    try:
        d = Decimal(str(value))
        if d.is_nan() or d.is_infinite():
            return Decimal("0.00")
        return d
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")

def _empty_inventory_payload() -> Dict[str, Any]:
    return {
        "exists": False,
        "available_quantity": "0.00",
        "reserved_quantity": "0.00",
        "incoming_quantity": "0.00",
        "free_stock": "0.00",
        "total_stock": "0.00",
        "warehouse_count": 0,
        "warehouse_ids": [],
        "warehouse_summary": "No warehouse",
        "is_out_of_stock": True,
        "is_low_stock": False,
        "is_overstock": False,
        "needs_reorder": False,
    }

def _safe_inventory_summary_for_target(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
) -> Dict[str, Any]:
    selectors = _get_inventory_selectors()
    if selectors is None:
        return _empty_inventory_payload()
    try:
        result = selectors.get_inventory_summary_for_target(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
        )
        if isinstance(result, dict):
            return result
    except Exception as exc:
        logger.debug("Safe inventory summary failed: %s", exc)
    return _empty_inventory_payload()

def _safe_inventory_check(
    *,
    product: Any = None,
    product_variant: Any = None,
    warehouse: Any = None,
    quantity: Any = 1,
    include_all_warehouses: bool = False,
) -> Dict[str, Any]:
    services = _get_inventory_services()
    if services is None:
        return {
            "is_available": False,
            "free_stock": "0.00",
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "warehouses_checked": 0,
            "per_warehouse": [],
        }
    try:
        return services.check_stock(
            product=product,
            product_variant=product_variant,
            warehouse=warehouse,
            quantity=quantity,
            include_all_warehouses=include_all_warehouses,
        )
    except Exception as exc:
        logger.debug("Safe inventory check failed: %s", exc)
        return {
            "is_available": False,
            "free_stock": "0.00",
            "available_quantity": "0.00",
            "reserved_quantity": "0.00",
            "warehouses_checked": 0,
            "per_warehouse": [],
        }

def _optimize_product_for_listing(qs: QuerySet) -> QuerySet:
    qs = qs.select_related(
        "category",
        "category__parent",
        "artisan",
        "material",
        "hue",
    )

    inventory_models = _get_inventory_models()
    if inventory_models:
        Inventory = inventory_models[0]

        variant_inventory_prefetch = Prefetch(
            "variants__inventory_records",
            queryset=Inventory.objects.filter(is_active=True).select_related("warehouse"),
        )
        product_inventory_prefetch = Prefetch(
            "inventory_records",
            queryset=Inventory.objects.filter(
                is_active=True,
                product_variant__isnull=True,
            ).select_related("warehouse"),
        )

        qs = qs.prefetch_related(
            "ethical_standards",
            "tags",
            "in_collections",
            "highlights",
            "trust_badges",
            "labels",
            "icons",
            "gallery_images",
            "additional_galleries",
            "variants",
            variant_inventory_prefetch,
            product_inventory_prefetch,
        )
    else:
        qs = qs.prefetch_related(
            "ethical_standards",
            "tags",
            "in_collections",
            "highlights",
            "trust_badges",
            "labels",
            "icons",
            "gallery_images",
            "additional_galleries",
            "variants",
        )

    return qs

def _optimize_product_for_detail(qs: QuerySet) -> QuerySet:
    qs = qs.select_related(
        "category",
        "category__parent",
        "artisan",
        "material",
        "hue",
        "seo_config",
        "schema_config",
    )
    qs = qs.prefetch_related(
        "ethical_standards",
        "tags",
        "in_collections",
        "highlights",
        "trust_badges",
        "labels",
        "icons",
        "gallery_images",
        "additional_galleries",
        "specifications",
        "faqs",
        "videos",
        "related_products",
        "upsell_products",
        "cross_sell_products",
    )

    inventory_models = _get_inventory_models()
    if inventory_models:
        Inventory = inventory_models[0]
        variant_inventory_prefetch = Prefetch(
            "variants__inventory_records",
            queryset=Inventory.objects.filter(is_active=True).select_related("warehouse"),
        )
        product_inventory_prefetch = Prefetch(
            "inventory_records",
            queryset=Inventory.objects.filter(
                is_active=True,
                product_variant__isnull=True,
            ).select_related("warehouse"),
        )
        qs = qs.prefetch_related(
            "variants",
            variant_inventory_prefetch,
            product_inventory_prefetch,
        )
    else:
        qs = qs.prefetch_related("variants")

    return qs

def _optimize_variant_queryset(qs: QuerySet) -> QuerySet:
    inventory_models = _get_inventory_models()
    if inventory_models:
        Inventory = inventory_models[0]
        return qs.select_related("product").prefetch_related(
            Prefetch(
                "inventory_records",
                queryset=Inventory.objects.filter(is_active=True).select_related("warehouse"),
            )
        )
    return qs.select_related("product")

def _attach_inventory_to_product(product: Product) -> None:
    if product is None or getattr(product, "pk", None) is None:
        return
    try:
        product.inventory_summary = _build_product_inventory_payload(product)
    except Exception as exc:
        logger.debug(
            "Failed to attach inventory summary for product %s: %s",
            getattr(product, "pk", "?"),
            exc,
        )
        product.inventory_summary = _empty_inventory_payload()

def _build_product_inventory_payload(product: Product) -> Dict[str, Any]:
    if product is None or getattr(product, "pk", None) is None:
        return _empty_inventory_payload()

    inventory_models = _get_inventory_models()
    if not inventory_models:
        return _safe_inventory_summary_for_target(product=product)

    variants_qs = getattr(product, "variants", None)
    variants = list(variants_qs.all()) if variants_qs is not None else []

    if variants:
        return _aggregate_variants_inventory(variants)

    product_records_qs = getattr(product, "inventory_records", None)
    if product_records_qs is not None:
        return _aggregate_inventory_records(list(product_records_qs.all()))

    return _safe_inventory_summary_for_target(product=product)

def _aggregate_inventory_records(records: Iterable[Any]) -> Dict[str, Any]:
    records_list = [r for r in records if r is not None]
    if not records_list:
        return _empty_inventory_payload()

    total_available = sum(
        (_safe_decimal(getattr(r, "available_quantity", 0)) for r in records_list),
        Decimal("0.00"),
    )
    total_reserved = sum(
        (_safe_decimal(getattr(r, "reserved_quantity", 0)) for r in records_list),
        Decimal("0.00"),
    )
    total_incoming = sum(
        (_safe_decimal(getattr(r, "incoming_quantity", 0)) for r in records_list),
        Decimal("0.00"),
    )
    warehouse_ids = {
        getattr(r, "warehouse_id", None)
        for r in records_list
        if getattr(r, "warehouse_id", None) is not None
    }
    warehouse_ids.discard(None)

    free_stock = max(Decimal("0.00"), total_available - total_reserved)
    total_stock = total_available + total_reserved

    is_out_of_stock = free_stock <= Decimal("0.00")
    is_overstock = any(bool(getattr(r, "is_overstock", False)) for r in records_list)
    needs_reorder = any(bool(getattr(r, "needs_reorder", False)) for r in records_list)
    is_low_stock = (
        not is_out_of_stock
        and any(bool(getattr(r, "is_low_stock", False)) for r in records_list)
    )

    return {
        "exists": True,
        "available_quantity": str(total_available),
        "reserved_quantity": str(total_reserved),
        "incoming_quantity": str(total_incoming),
        "free_stock": str(free_stock),
        "total_stock": str(total_stock),
        "warehouse_count": len(warehouse_ids),
        "warehouse_ids": list(warehouse_ids),
        "warehouse_summary": (
            f"{len(warehouse_ids)} warehouse(s)"
            if len(warehouse_ids) != 1
            else "1 warehouse"
        ),
        "is_out_of_stock": is_out_of_stock,
        "is_low_stock": is_low_stock,
        "is_overstock": is_overstock,
        "needs_reorder": needs_reorder,
    }

def _aggregate_variants_inventory(variants: Iterable[Any]) -> Dict[str, Any]:
    variants_list = [v for v in variants if v is not None]
    if not variants_list:
        return _empty_inventory_payload()

    total_available = Decimal("0.00")
    total_reserved = Decimal("0.00")
    total_incoming = Decimal("0.00")
    warehouse_ids: set = set()
    in_stock_variants = 0
    low_stock_variants = 0
    out_of_stock_variants = 0
    considered_variants = 0

    for variant in variants_list:
        records_qs = getattr(variant, "inventory_records", None)
        if records_qs is None:
            continue
        records = list(records_qs.all())
        if not records:
            continue
        considered_variants += 1
        v_available = sum(
            (_safe_decimal(getattr(r, "available_quantity", 0)) for r in records),
            Decimal("0.00"),
        )
        v_reserved = sum(
            (_safe_decimal(getattr(r, "reserved_quantity", 0)) for r in records),
            Decimal("0.00"),
        )
        v_incoming = sum(
            (_safe_decimal(getattr(r, "incoming_quantity", 0)) for r in records),
            Decimal("0.00"),
        )
        v_free = max(Decimal("0.00"), v_available - v_reserved)
        v_out = v_free <= Decimal("0.00")
        v_low = (
            not v_out
            and any(bool(getattr(r, "is_low_stock", False)) for r in records)
        )

        total_available += v_available
        total_reserved += v_reserved
        total_incoming += v_incoming
        for record in records:
            wid = getattr(record, "warehouse_id", None)
            if wid is not None:
                warehouse_ids.add(wid)

        if v_out:
            out_of_stock_variants += 1
        elif v_low:
            low_stock_variants += 1
        else:
            in_stock_variants += 1

    if considered_variants == 0:
        return _empty_inventory_payload()

    free_stock = max(Decimal("0.00"), total_available - total_reserved)
    total_stock = total_available + total_reserved
    is_out_of_stock = free_stock <= Decimal("0.00")
    is_low_stock = (
        not is_out_of_stock
        and (low_stock_variants > 0 or out_of_stock_variants < considered_variants)
    )

    return {
        "exists": True,
        "available_quantity": str(total_available),
        "reserved_quantity": str(total_reserved),
        "incoming_quantity": str(total_incoming),
        "free_stock": str(free_stock),
        "total_stock": str(total_stock),
        "warehouse_count": len(warehouse_ids),
        "warehouse_ids": list(warehouse_ids),
        "warehouse_summary": (
            f"{len(warehouse_ids)} warehouse(s)"
            if len(warehouse_ids) != 1
            else "1 warehouse"
        ),
        "is_out_of_stock": is_out_of_stock,
        "is_low_stock": is_low_stock,
        "is_overstock": False,
        "needs_reorder": False,
        "variant_count": considered_variants,
        "in_stock_variants": in_stock_variants,
        "low_stock_variants": low_stock_variants,
        "out_of_stock_variants": out_of_stock_variants,
    }

def _build_variant_inventory_payload(variant: Any) -> Dict[str, Any]:
    if variant is None or getattr(variant, "pk", None) is None:
        return _empty_inventory_payload()

    records_qs = getattr(variant, "inventory_records", None)
    if records_qs is not None:
        return _aggregate_inventory_records(list(records_qs.all()))

    return _safe_inventory_summary_for_target(product_variant=variant)

def _attach_inventory_summaries(products: Iterable[Product]) -> List[Product]:
    product_list = list(products)
    for product in product_list:
        _attach_inventory_to_product(product)
    return product_list

def _process_products_with_inventory(qs: QuerySet) -> List[Product]:
    optimized = _optimize_product_for_listing(qs)
    products = list(optimized)
    return _attach_inventory_summaries(products)

def get_catalog_settings() -> CatalogSettings:
    return ProductService.get_catalog_settings()

def get_category_by_slug(slug: str) -> Optional[Category]:
    return CategoryService.get_category_by_slug(slug)

def get_active_categories_hierarchy() -> List[Dict[str, Any]]:
    return CategoryService.get_active_categories_hierarchy()

def query_products_for_category(
    category: Optional[Category] = None,
    *,
    sort_by: str = "featured",
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
) -> QuerySet:
    return SearchService.query_products_for_category(
        category=category,
        sort_by=sort_by,
        min_price=min_price,
        max_price=max_price,
        in_stock_only=in_stock_only,
        min_rating=min_rating,
        selected_materials=selected_materials,
        selected_artisans=selected_artisans,
        selected_origins=selected_origins,
        selected_hues=selected_hues,
        selected_ethical_standards=selected_ethical_standards,
        selected_tags=selected_tags,
        selected_collections=selected_collections,
        on_sale_only=on_sale_only,
        min_discount_pct=min_discount_pct,
        variant_attributes=variant_attributes,
        search_query=search_query,
    )

def get_sidebar_filter_metadata(
    category: Optional[Category] = None,
    current_selections: Optional[Dict[str, Any]] = None,
    base_qs: Optional[QuerySet[Product]] = None,
) -> Dict[str, Any]:
    return SearchService.get_sidebar_filter_metadata(
        category=category, current_selections=current_selections, base_qs=base_qs
    )

def paginate_products(
    products_qs: QuerySet, page_number: Union[str, int], items_per_page: int
) -> Any:
    return SearchService.paginate_products(products_qs, page_number, items_per_page)

class ProductService:
    @staticmethod
    def get_catalog_settings() -> CatalogSettings:
        try:
            settings = CatalogSettings.objects.first()
            if settings is not None:
                return settings
        except Exception as exc:
            logger.warning(
                "Failed to fetch catalog settings: %s. Creating default.",
                exc,
            )
        try:
            return CatalogSettings.objects.create(
                default_items_per_page=12,
                price_filter_min=500,
                price_filter_max=100000,
                show_stock_warning_threshold=5,
            )
        except Exception as exc:
            logger.exception("Failed to create default catalog settings: %s", exc)
            return CatalogSettings(
                default_items_per_page=12,
                price_filter_min=500,
                price_filter_max=100000,
                show_stock_warning_threshold=5,
            )

    @staticmethod
    def get_optimized_product_queryset() -> QuerySet:
        return _optimize_product_for_listing(Product.objects.all())

    @classmethod
    def get_product_by_id(cls, product_id: int) -> Optional[Product]:
        if not isinstance(product_id, int) or product_id <= 0:
            return None
        try:
            return cls.get_optimized_product_queryset().filter(id=product_id).first()
        except Product.DoesNotExist:
            return None
        except Exception as exc:
            logger.debug("get_product_by_id failed for %s: %s", product_id, exc)
            return None

    @classmethod
    def get_product_by_slug(cls, slug: str) -> Optional[Product]:
        if not slug or not isinstance(slug, str):
            return None
        slug = slug.strip()
        if not slug:
            return None
        try:
            qs = _optimize_product_for_detail(Product.objects.filter(slug=slug))
            return qs.first()
        except Product.DoesNotExist:
            return None
        except Exception as exc:
            logger.debug("get_product_by_slug failed for %s: %s", slug, exc)
            return None

    @staticmethod
    def get_product_specifications(product: Product) -> QuerySet:
        if product is None or getattr(product, "pk", None) is None:
            return ProductSpecification.objects.none()
        try:
            return product.specifications.all().order_by("display_order", "label")
        except Exception as exc:
            logger.debug("get_product_specifications failed: %s", exc)
            return ProductSpecification.objects.none()

    @staticmethod
    def get_product_faqs(product: Product) -> QuerySet:
        if product is None or getattr(product, "pk", None) is None:
            return ProductFAQ.objects.none()
        try:
            return product.faqs.filter(is_active=True).order_by(
                "display_order", "id"
            )
        except Exception as exc:
            logger.debug("get_product_faqs failed: %s", exc)
            return ProductFAQ.objects.none()

    @staticmethod
    def get_product_videos(product: Product) -> QuerySet:
        if product is None or getattr(product, "pk", None) is None:
            return ProductVideo.objects.none()
        try:
            return product.videos.all().order_by("display_order", "id")
        except Exception as exc:
            logger.debug("get_product_videos failed: %s", exc)
            return ProductVideo.objects.none()

    @staticmethod
    def get_product_variants(product: Product) -> QuerySet:
        if product is None or getattr(product, "pk", None) is None:
            return ProductVariant.objects.none()
        try:
            qs = product.variants.filter(is_active=True).order_by("sort_order", "id")
            return _optimize_variant_queryset(qs)
        except Exception as exc:
            logger.debug("get_product_variants failed: %s", exc)
            return ProductVariant.objects.none()

    @staticmethod
    def get_inventory_summary(product: Product) -> Dict[str, Any]:
        return _build_product_inventory_payload(product)

    @staticmethod
    def get_inventory_check(
        product: Product,
        *,
        quantity: Any = 1,
        warehouse: Any = None,
    ) -> Dict[str, Any]:
        if product is None:
            return {
                "is_available": False,
                "free_stock": "0.00",
                "available_quantity": "0.00",
                "warehouses_checked": 0,
                "per_warehouse": [],
            }
        return _safe_inventory_check(
            product=product,
            warehouse=warehouse,
            quantity=quantity,
            include_all_warehouses=warehouse is None,
        )

    @staticmethod
    def get_featured_products(limit: int = 12) -> List[Product]:
        qs = Product.objects.featured().order_by("position", "-created_at")
        qs = _optimize_product_for_listing(qs)
        products = list(qs[: max(1, int(limit) if limit else 12)])
        return _attach_inventory_summaries(products)

    @staticmethod
    def get_new_arrivals(limit: int = 12) -> List[Product]:
        qs = (
            Product.objects.filter(
                status=Product.ProductStatus.PUBLISHED,
                is_active=True,
            )
            .order_by("-published_at", "-created_at")
        )
        qs = _optimize_product_for_listing(qs)
        products = list(qs[: max(1, int(limit) if limit else 12)])
        return _attach_inventory_summaries(products)

    @staticmethod
    def get_best_sellers(limit: int = 12) -> List[Product]:
        qs = (
            Product.objects.filter(
                status=Product.ProductStatus.PUBLISHED,
                is_active=True,
            )
            .order_by("-wishlist_count", "-view_count", "-reviews_count")
        )
        qs = _optimize_product_for_listing(qs)
        products = list(qs[: max(1, int(limit) if limit else 12)])
        return _attach_inventory_summaries(products)

    @staticmethod
    def get_trending_products(limit: int = 12) -> List[Product]:
        qs = (
            Product.objects.filter(
                status=Product.ProductStatus.PUBLISHED,
                is_active=True,
            )
            .order_by("-view_count", "-wishlist_count")
        )
        qs = _optimize_product_for_listing(qs)
        products = list(qs[: max(1, int(limit) if limit else 12)])
        return _attach_inventory_summaries(products)

    @staticmethod
    def get_popular_products(limit: int = 12) -> List[Product]:
        qs = (
            Product.objects.filter(
                status=Product.ProductStatus.PUBLISHED,
                is_active=True,
            )
            .order_by("-view_count", "-reviews_count")
        )
        qs = _optimize_product_for_listing(qs)
        products = list(qs[: max(1, int(limit) if limit else 12)])
        return _attach_inventory_summaries(products)

class CategoryService:
    @staticmethod
    def get_category_by_slug(slug: str) -> Optional[Category]:
        if not slug or not isinstance(slug, str):
            return None
        slug = slug.strip()
        if not slug:
            return None
        try:
            return (
                Category.objects.filter(slug=slug, is_active=True)
                .select_related("parent")
                .first()
            )
        except Exception as exc:
            logger.debug("get_category_by_slug failed for %s: %s", slug, exc)
            return None

    @staticmethod
    def get_active_categories_hierarchy() -> List[Dict[str, Any]]:
        try:
            top_categories = (
                Category.objects.filter(parent__isnull=True, is_active=True)
                .prefetch_related(
                    Prefetch(
                        "subcategories",
                        queryset=Category.objects.filter(is_active=True),
                    )
                )
            )
            hierarchy: List[Dict[str, Any]] = []
            for cat in top_categories:
                subcats = [
                    {"name": sub.name, "slug": sub.slug}
                    for sub in cat.subcategories.all()
                ]
                hierarchy.append(
                    {
                        "id": cat.id,
                        "name": cat.name,
                        "slug": cat.slug,
                        "subcategories": subcats,
                    }
                )
            return hierarchy
        except Exception as exc:
            logger.debug("get_active_categories_hierarchy failed: %s", exc)
            return []

    @staticmethod
    def get_category_product_counts() -> Dict[int, int]:
        try:
            counts = Category.objects.filter(is_active=True).annotate(
                active_product_count=Count(
                    "products",
                    filter=Q(
                        products__is_active=True,
                        products__status=Product.ProductStatus.PUBLISHED,
                    ),
                    distinct=True,
                )
            )
            return {c.id: c.active_product_count for c in counts}
        except Exception as exc:
            logger.debug("get_category_product_counts failed: %s", exc)
            return {}

class CollectionService:
    @staticmethod
    def get_collection_by_slug(slug: str) -> Optional[ProductCollection]:
        if not slug or not isinstance(slug, str):
            return None
        slug = slug.strip()
        if not slug:
            return None
        try:
            return (
                ProductCollection.objects.filter(slug=slug, is_active=True)
                .prefetch_related(
                    Prefetch(
                        "products",
                        queryset=Product.objects.filter(
                            is_active=True,
                            status=Product.ProductStatus.PUBLISHED,
                        ),
                    )
                )
                .first()
            )
        except ProductCollection.DoesNotExist:
            return None
        except Exception as exc:
            logger.debug("get_collection_by_slug failed for %s: %s", slug, exc)
            return None

    @staticmethod
    def get_active_collections() -> QuerySet:
        try:
            return ProductCollection.objects.filter(is_active=True).order_by(
                "sort_order", "name"
            )
        except Exception as exc:
            logger.debug("get_active_collections failed: %s", exc)
            return ProductCollection.objects.none()

    @staticmethod
    def get_products_in_collection(
        slug: str, limit: Optional[int] = None
    ) -> QuerySet:
        try:
            collection = ProductCollection.objects.filter(
                slug=slug, is_active=True
            ).first()
        except Exception as exc:
            logger.debug("get_products_in_collection lookup failed: %s", exc)
            return Product.objects.none()
        if not collection:
            return Product.objects.none()
        try:
            qs = collection.products.filter(
                is_active=True, status=Product.ProductStatus.PUBLISHED
            ).select_related("category", "artisan", "material", "hue").prefetch_related(
                "ethical_standards"
            )
            qs = _optimize_product_for_listing(qs)
            if limit:
                return qs[: int(limit)]
            return qs
        except Exception as exc:
            logger.debug("get_products_in_collection failed: %s", exc)
            return Product.objects.none()

class RecommendationService:
    @staticmethod
    def get_related_products(
        product: Product, limit: int = 4
    ) -> List[Product]:
        if product is None or getattr(product, "pk", None) is None:
            return []
        try:
            related = (
                product.related_products.filter(
                    is_active=True, status=Product.ProductStatus.PUBLISHED
                )
                .select_related("category", "artisan", "material", "hue")
                .prefetch_related("ethical_standards")
            )
            result = list(related[: int(limit) if limit else 4])
            if result:
                return _attach_inventory_summaries(result)

            if product.artisan_id:
                artisan_qs = (
                    Product.objects.filter(
                        artisan=product.artisan,
                        is_active=True,
                        status=Product.ProductStatus.PUBLISHED,
                    )
                    .exclude(id=product.id)
                    .select_related(
                        "category", "artisan", "material", "hue"
                    )
                    .prefetch_related("ethical_standards")
                )
                artisan_qs = _optimize_product_for_listing(artisan_qs)
                result = list(artisan_qs[: int(limit) if limit else 4])
                if result:
                    return _attach_inventory_summaries(result)

            if product.category_id:
                cat_qs = (
                    Product.objects.filter(
                        category=product.category,
                        is_active=True,
                        status=Product.ProductStatus.PUBLISHED,
                    )
                    .exclude(id=product.id)
                    .select_related(
                        "category", "artisan", "material", "hue"
                    )
                    .prefetch_related("ethical_standards")
                    .order_by("-wishlist_count", "-view_count")
                )
                cat_qs = _optimize_product_for_listing(cat_qs)
                result = list(cat_qs[: int(limit) if limit else 4])
                return _attach_inventory_summaries(result)
            return []
        except Exception as exc:
            logger.debug("get_related_products failed: %s", exc)
            return []

class RecentlyViewedService:
    _MAX_RECENTLY_VIEWED = 20

    @staticmethod
    def add_to_recently_viewed(
        product: Product, user: Any = None, session_key: Optional[str] = None
    ) -> Optional[RecentlyViewedProduct]:
        if product is None or getattr(product, "pk", None) is None:
            return None
        if not user and not session_key:
            return None

        try:
            user_id = user.id if (user and getattr(user, "is_authenticated", False)) else None

            lookup_kwargs: Dict[str, Any] = {"product": product}
            if user_id:
                lookup_kwargs["user_id"] = user_id
            else:
                lookup_kwargs["session_key"] = session_key

            rv, _ = RecentlyViewedProduct.objects.update_or_create(
                **lookup_kwargs, defaults={}
            )

            trim_qs = (
                RecentlyViewedProduct.objects.filter(user_id=user_id)
                if user_id
                else RecentlyViewedProduct.objects.filter(session_key=session_key)
            )
            excess_ids = list(
                trim_qs.order_by("-viewed_at").values_list("id", flat=True)[
                    RecentlyViewedService._MAX_RECENTLY_VIEWED :
                ]
            )
            if excess_ids:
                RecentlyViewedProduct.objects.filter(id__in=excess_ids).delete()

            return rv
        except Exception as exc:
            logger.debug("add_to_recently_viewed failed: %s", exc)
            return None

class BreadcrumbService:
    @staticmethod
    def build_for_home() -> List[Dict[str, str]]:
        return [{"label": "Home", "url": "/"}]

    @classmethod
    def build_for_category(cls, category: Category) -> List[Dict[str, str]]:
        breadcrumbs = cls.build_for_home()
        breadcrumbs.append({"label": "Handicrafts", "url": "#"})
        if category is not None and getattr(category, "parent_id", None):
            try:
                parent_name = (
                    category.parent.name if category.parent else "Unnamed"
                )
            except Exception:
                parent_name = "Unnamed"
            breadcrumbs.append(
                {
                    "label": parent_name,
                    "url": f"/category/{category.parent.slug}/"
                    if getattr(category.parent, "slug", None)
                    else "#",
                }
            )
        if category is not None:
            breadcrumbs.append(
                {
                    "label": category.name or "Unnamed",
                    "url": f"/category/{category.slug}/"
                    if getattr(category, "slug", None)
                    else "#",
                }
            )
        return breadcrumbs

    @classmethod
    def build_for_product(cls, product: Product) -> List[Dict[str, str]]:
        if product is None:
            return cls.build_for_home()

        if getattr(product, "category", None):
            breadcrumbs = cls.build_for_category(product.category)
        else:
            breadcrumbs = cls.build_for_home()
            breadcrumbs.append({"label": "Handicrafts", "url": "#"})

        breadcrumbs.append(
            {
                "label": getattr(product, "title", "Product") or "Product",
                "url": f"/product/{product.slug}/"
                if getattr(product, "slug", None)
                else "#",
            }
        )
        return breadcrumbs

class SearchService:
    """
    Service facade orchestrating product queries, multi-criteria filtering, and facet generation.
    """

    @staticmethod
    def query_products_for_category(
        category: Optional[Category] = None,
        *,
        sort_by: str = "featured",
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
    ) -> QuerySet:
        """
        Queries products using selector functions with full multi-criteria filter support.
        """
        return get_filtered_products_queryset(
            category=category,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
            min_rating=min_rating,
            selected_materials=selected_materials,
            selected_artisans=selected_artisans,
            selected_origins=selected_origins,
            selected_hues=selected_hues,
            selected_ethical_standards=selected_ethical_standards,
            selected_tags=selected_tags,
            selected_collections=selected_collections,
            on_sale_only=on_sale_only,
            min_discount_pct=min_discount_pct,
            variant_attributes=variant_attributes,
            search_query=search_query,
            sort_by=sort_by,
        )

    @staticmethod
    def get_sidebar_filter_metadata(
        category: Optional[Category] = None,
        current_selections: Optional[Dict[str, Any]] = None,
        base_qs: Optional[QuerySet[Product]] = None,
    ) -> Dict[str, Any]:
        """
        Generates dynamic group-by item counts (facets) and price bounds for the sidebar filter workspace.
        """
        if base_qs is None:
            base_qs = Product.objects.published()
            if category:
                base_qs = base_qs.in_category(category)

        return get_facet_counts_for_queryset(
            qs=base_qs,
            category=category,
            current_selections=current_selections,
        )

    @staticmethod
    def paginate_products(
        products_qs: QuerySet, page_number: Union[str, int], items_per_page: int
    ) -> Any:
        try:
            safe_per_page = int(items_per_page) if items_per_page else 12
            if safe_per_page < 1:
                safe_per_page = 12
            if safe_per_page > 200:
                safe_per_page = 200
        except (TypeError, ValueError):
            safe_per_page = 12

        try:
            paginator = Paginator(products_qs, safe_per_page)
            try:
                page_obj = paginator.page(page_number)
            except PageNotAnInteger:
                page_obj = paginator.page(1)
            except EmptyPage:
                if paginator.num_pages < 1:
                    return paginator.page(1) if paginator.count else []
                page_obj = paginator.page(paginator.num_pages)
            return page_obj
        except Exception as exc:
            logger.debug("paginate_products failed: %s", exc)
            return []

    @staticmethod
    def global_search(
        query: str,
        limit: int = 50,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Product]:
        """
        Executes full-text keyword searches enhanced with structured facet criteria.
        """
        if not query or not isinstance(query, str):
            return []
        clean_query = query.strip()
        if not clean_query:
            return []

        filters = filters or {}
        try:
            qs = get_filtered_products_queryset(
                search_query=clean_query,
                min_price=filters.get("min_price"),
                max_price=filters.get("max_price"),
                in_stock_only=filters.get("in_stock_only", False),
                min_rating=filters.get("min_rating"),
                selected_materials=filters.get("materials"),
                selected_artisans=filters.get("artisans"),
                selected_origins=filters.get("origins"),
                selected_hues=filters.get("hues"),
                selected_ethical_standards=filters.get("ethical"),
                selected_tags=filters.get("tags"),
                selected_collections=filters.get("collections"),
                on_sale_only=filters.get("on_sale", False),
                sort_by=filters.get("sort", "featured"),
            )
            qs = _optimize_product_for_listing(qs)
            products = list(qs[: int(limit) if limit else 50])
            return _attach_inventory_summaries(products)
        except Exception as exc:
            logger.debug("global_search failed: %s", exc)
            return []

def optimize_product_queryset(qs: QuerySet) -> QuerySet:
    return _optimize_product_for_listing(qs)

def optimize_product_for_listing(qs: QuerySet) -> QuerySet:
    return _optimize_product_for_listing(qs)

def optimize_product_for_detail(qs: QuerySet) -> QuerySet:
    return _optimize_product_for_detail(qs)

def optimize_variant_queryset(qs: QuerySet) -> QuerySet:
    return _optimize_variant_queryset(qs)

def attach_inventory_summaries(products: Iterable[Product]) -> List[Product]:
    return _attach_inventory_summaries(products)

def build_product_inventory_payload(product: Product) -> Dict[str, Any]:
    return _build_product_inventory_payload(product)

def build_variant_inventory_payload(variant: Any) -> Dict[str, Any]:
    return _build_variant_inventory_payload(variant)

def get_inventory_summary(product: Product) -> Dict[str, Any]:
    return ProductService.get_inventory_summary(product)

def get_inventory_check(
    product: Product,
    *,
    quantity: Any = 1,
    warehouse: Any = None,
) -> Dict[str, Any]:
    return ProductService.get_inventory_check(
        product=product, quantity=quantity, warehouse=warehouse
    )

def get_featured_products(limit: int = 12) -> List[Product]:
    return ProductService.get_featured_products(limit=limit)

def get_new_arrivals(limit: int = 12) -> List[Product]:
    return ProductService.get_new_arrivals(limit=limit)

def get_best_sellers(limit: int = 12) -> List[Product]:
    return ProductService.get_best_sellers(limit=limit)

def get_trending_products(limit: int = 12) -> List[Product]:
    return ProductService.get_trending_products(limit=limit)

def get_popular_products(limit: int = 12) -> List[Product]:
    return ProductService.get_popular_products(limit=limit)

def global_search(query: str, limit: int = 50, filters: Optional[Dict[str, Any]] = None) -> List[Product]:
    return SearchService.global_search(query, limit=limit, filters=filters)

__all__ = [
    "CATALOG_CACHE_VERSION",
    "CATALOG_CACHE_TIMEOUT",
    "CATALOG_LIST_CACHE_PREFIX",
    "invalidate_catalog_cache",
    "get_catalog_settings",
    "get_category_by_slug",
    "get_active_categories_hierarchy",
    "query_products_for_category",
    "get_sidebar_filter_metadata",
    "paginate_products",
    "ProductService",
    "CategoryService",
    "CollectionService",
    "RecommendationService",
    "RecentlyViewedService",
    "BreadcrumbService",
    "SearchService",
    "optimize_product_queryset",
    "optimize_product_for_listing",
    "optimize_product_for_detail",
    "optimize_variant_queryset",
    "attach_inventory_summaries",
    "build_product_inventory_payload",
    "build_variant_inventory_payload",
    "get_inventory_summary",
    "get_inventory_check",
    "get_featured_products",
    "get_new_arrivals",
    "get_best_sellers",
    "get_trending_products",
    "get_popular_products",
    "global_search",
]