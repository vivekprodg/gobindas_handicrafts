"""
Enterprise-grade service layer for the Catalog application.

ARCHITECTURE OVERVIEW
====================

This module implements the COMPLETE service layer for the catalog domain.
The catalog is intentionally INVENTORY-AGNOSTIC:

    * Product and ProductVariant models do NOT carry stock fields.
    * Catalog services NEVER calculate stock, quantity, or availability.
    * All inventory data is read dynamically from the Inventory app
      via lazy imports of its selectors and services.
    * Read-only inventory context is attached as standardized payloads.

The services in this module:
    * Build optimized, deeply-prefetched Product / ProductVariant
      querysets.
    * Compose dynamic inventory summaries on top of those querysets.
    * Provide category, collection, search, breadcrumb, and
      recommendation services that remain pure to the catalog domain.
    * Expose cached helper functions for singleton settings.
    * Maintain full backward compatibility with existing views and
      admin consumers.

ARCHITECTURE PRINCIPLES
=======================

* **Inventory Agnostic**: Catalog does not own or calculate stock.
  Every inventory read is delegated to the Inventory app.

* **Service Layer Purity**: Services expose read-only operations.
  No mutation of Product / Variant / Category from these services.

* **Lazy Imports**: The inventory app is imported lazily to avoid
  circular dependency issues and to allow the catalog app to boot
  even when inventory is partially configured.

* **Deep Optimization**: select_related, prefetch_related, Prefetch,
  annotated subqueries, Exists, OuterRef, Case / When are used
  wherever beneficial.

* **Backward Compatibility**: All legacy function names and module
  level helpers (``invalidate_catalog_cache``, ``get_catalog_settings``,
  etc.) are preserved so existing view / admin / template code
  continues to work without modification.

* **Future Proof**: Designed to integrate seamlessly with future
  modules (Purchase Orders, Manufacturing, Batch / Lot, Serial
  Numbers, Barcode / QR, Expiry, Mobile ERP, Notifications, etc.)
  without requiring major refactors.

* **CMS Driven**: Default page sizes, price filter bounds, and stock
  warning thresholds come from the ``CatalogSettings`` singleton.

* **OWASP Secure Coding**: Every user-supplied parameter is validated,
  every ORM ordering is whitelisted, every dynamic lookup is
  parameterized. No SQL injection, no mass-assignment, no
  information disclosure.

Author: Handicraft E-commerce Engineering Team
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
    F,
    FilteredRelation,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    When,
)
from django.db.models.functions import Coalesce

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
    ProductVariant,
    ProductVideo,
    RecentlyViewedProduct,
)

logger = logging.getLogger(__name__)

# ==============================================================================
# CACHING & SETTINGS CONFIGURATIONS
# ==============================================================================
CATALOG_CACHE_VERSION = 1
CATALOG_CACHE_TIMEOUT = 60 * 30  # 30 minutes
CATALOG_LIST_CACHE_PREFIX = "catalog:list:"

def invalidate_catalog_cache() -> None:
    """
    Clears all catalog-related caches.

    This should be called from Signals on Product / Category / Artisan
    save / delete. Safe to call multiple times.
    """
    try:
        cache.clear()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("Failed to clear catalog cache: %s", exc)

# ==============================================================================
# INVENTORY ACCESS HELPERS (LAZY)
# ==============================================================================
# The catalog app must NEVER own inventory. Every inventory read below
# uses these lazy accessors to reach the Inventory app without making
# it a hard dependency. If the inventory app is unavailable or not
# installed, the catalog continues to function with safe defaults.
# ==============================================================================
def _get_inventory_models() -> Tuple:
    """
    Lazy accessor for inventory models.

    Returns a tuple of
        (Inventory, InventoryTransaction, StockReservation, Warehouse)
    on success, or an empty tuple on ImportError. Callers MUST handle
    the empty-tuple case gracefully.
    """
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
            "Inventory models could not be imported. Catalog services "
            "will operate in inventory-blind mode."
        )
        return ()

def _get_inventory_selectors() -> Optional[Any]:
    """
    Lazy accessor for the inventory selectors module. Returns None
    on ImportError.
    """
    try:
        from apps.inventory import selectors
        return selectors
    except ImportError:
        logger.warning("Inventory selectors could not be imported.")
        return None

def _get_inventory_services() -> Optional[Any]:
    """
    Lazy accessor for the inventory services module. Returns None
    on ImportError.
    """
    try:
        from apps.inventory import services
        return services
    except ImportError:
        logger.warning("Inventory services could not be imported.")
        return None

# ==============================================================================
# SAFE INVENTORY WRAPPERS
# ==============================================================================
def _safe_decimal(value: Any, *, allow_none: bool = True) -> Decimal:
    """
    Safely coerces a value into a Decimal. Returns Decimal("0.00")
    on any failure. Never raises.
    """
    if value is None:
        if allow_none:
            return Decimal("0.00")
        return Decimal("0.00")
    try:
        d = Decimal(str(value))
        if d.is_nan() or d.is_infinite():
            return Decimal("0.00")
        return d
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")

def _empty_inventory_payload() -> Dict[str, Any]:
    """
    Returns a complete, safe-default inventory context dictionary.
    Every inventory-related key is present, even when no inventory
    data is available. This guarantees that templates never
    encounter undefined variables.
    """
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
    """
    Safely retrieve a read-only inventory summary for a product or
    variant. Returns a safe empty payload on any failure. Never
    mutates inventory.
    """
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
    """
    Safely invoke the Inventory service ``check_stock`` function.
    Returns a safe empty payload on any failure.
    """
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

# ==============================================================================
# QUERYSET OPTIMIZATION HELPERS
# ==============================================================================
def _optimize_product_for_listing(qs: QuerySet) -> QuerySet:
    """
    Returns a deeply-optimized Product queryset for list views.

    Applies:
        * select_related on category, artisan, material, hue
        * prefetch_related on M2M relations
        * Nested Prefetch on ProductVariant -> InventoryRecord (only
          when the inventory app is available)
        * Nested Prefetch on Product -> InventoryRecord (product level)

    Designed to avoid N+1 queries at scale.
    """
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
            queryset=Inventory.objects.filter(is_active=True).select_related(
                "warehouse"
            ),
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
    """
    Returns a deeply-optimized Product queryset for the detail view.

    Includes all relations used by the product detail template
    plus variant and inventory prefetches.
    """
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
            queryset=Inventory.objects.filter(is_active=True).select_related(
                "warehouse"
            ),
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
    """
    Returns an optimized ProductVariant queryset.
    """
    inventory_models = _get_inventory_models()
    if inventory_models:
        Inventory = inventory_models[0]
        return qs.select_related("product").prefetch_related(
            Prefetch(
                "inventory_records",
                queryset=Inventory.objects.filter(is_active=True).select_related(
                    "warehouse"
                ),
            )
        )
    return qs.select_related("product")

# ==============================================================================
# INVENTORY ATTACHMENT (READ-ONLY)
# ==============================================================================
def _attach_inventory_to_product(product: Product) -> None:
    """
    Attach a standardized ``inventory_summary`` dictionary to a
    single product instance. Uses prefetched data when available.

    Mutates the product in place by setting
    ``product.inventory_summary``. Never mutates the database.
    """
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
    """
    Build a standardized inventory payload for a single product.

    Strategy:
        1. If the product has active variants, aggregate inventory
           across all variants (variant-level stock).
        2. Otherwise, aggregate product-level inventory rows
           (where ``product_variant`` is NULL).
        3. If neither exists, return the safe-empty payload.

    Uses prefetched data when available to avoid N+1 queries.
    Falls back to a lazy lookup via the Inventory selector layer
    when prefetched data is absent.
    """
    if product is None or getattr(product, "pk", None) is None:
        return _empty_inventory_payload()

    inventory_models = _get_inventory_models()
    if not inventory_models:
        return _safe_inventory_summary_for_target(product=product)

    # Variant-level aggregation
    variants_qs = getattr(product, "variants", None)
    variants = list(variants_qs.all()) if variants_qs is not None else []

    if variants:
        return _aggregate_variants_inventory(variants)

    # Product-level inventory aggregation
    product_records_qs = getattr(product, "inventory_records", None)
    if product_records_qs is not None:
        return _aggregate_inventory_records(list(product_records_qs.all()))

    return _safe_inventory_summary_for_target(product=product)

def _aggregate_inventory_records(records: Iterable[Any]) -> Dict[str, Any]:
    """
    Aggregate a list of inventory records into a single payload.
    """
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
    is_overstock = any(
        bool(getattr(r, "is_overstock", False)) for r in records_list
    )
    needs_reorder = any(
        bool(getattr(r, "needs_reorder", False)) for r in records_list
    )
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
    """
    Aggregate inventory across multiple product variants.

    Uses prefetched ``inventory_records`` when available. For each
    variant, reads its inventory records and sums up available /
    reserved stock. Returns a structured inventory payload.
    """
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
    """
    Build an inventory payload for a single product variant.
    """
    if variant is None or getattr(variant, "pk", None) is None:
        return _empty_inventory_payload()

    records_qs = getattr(variant, "inventory_records", None)
    if records_qs is not None:
        return _aggregate_inventory_records(list(records_qs.all()))

    return _safe_inventory_summary_for_target(product_variant=variant)

def _attach_inventory_summaries(products: Iterable[Product]) -> List[Product]:
    """
    Process an iterable of products and attach a standardized
    ``inventory_summary`` attribute to each one. Returns the list
    of products for convenience.
    """
    product_list = list(products)
    for product in product_list:
        _attach_inventory_to_product(product)
    return product_list

def _process_products_with_inventory(qs: QuerySet) -> List[Product]:
    """
    Optimize a product queryset with inventory prefetching and
    attach inventory summaries to each product. This is the
    canonical helper for listing views.
    """
    optimized = _optimize_product_for_listing(qs)
    products = list(optimized)
    return _attach_inventory_summaries(products)

# ==============================================================================
# MODULE-LEVEL BACKWARD-COMPATIBLE FUNCTIONS
# ==============================================================================
# These top-level functions preserve the public API of the
# original services module so existing call sites continue to work
# without modification. They delegate to the new service classes
# defined below.
# ==============================================================================
def get_catalog_settings() -> CatalogSettings:
    """
    Retrieves the singleton CatalogSettings instance, creating a
    default one if none exists.

    Kept for backward compatibility with views and external
    components. Delegates to ``ProductService.get_catalog_settings``.
    """
    return ProductService.get_catalog_settings()

def get_category_by_slug(slug: str) -> Optional[Category]:
    """
    Retrieves an active Category instance by its slug.

    Kept for backward compatibility with views.
    """
    return CategoryService.get_category_by_slug(slug)

def get_active_categories_hierarchy() -> List[Dict[str, Any]]:
    """
    Compiles category hierarchy tree dynamically for sidebar
    filtering. Kept for backward compatibility.
    """
    return CategoryService.get_active_categories_hierarchy()

def query_products_for_category(
    category: Category,
    *,
    sort_by: str = "featured",
    min_price: Optional[Decimal] = None,
    max_price: Optional[Decimal] = None,
    selected_materials: Optional[List[str]] = None,
    selected_artisans: Optional[List[str]] = None,
    selected_origins: Optional[List[str]] = None,
    selected_hues: Optional[List[str]] = None,
    selected_ethical_standards: Optional[List[str]] = None,
) -> QuerySet:
    """
    Builds the filtered, sorted product QuerySet for a given
    Category (including subcategories). Kept for backward
    compatibility.
    """
    return SearchService.query_products_for_category(
        category=category,
        sort_by=sort_by,
        min_price=min_price,
        max_price=max_price,
        selected_materials=selected_materials,
        selected_artisans=selected_artisans,
        selected_origins=selected_origins,
        selected_hues=selected_hues,
        selected_ethical_standards=selected_ethical_standards,
    )

def get_sidebar_filter_metadata(
    category: Category, current_selections: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Compiles distinct filter values from all active products in
    this category tree to populate sidebar options dynamically.
    Kept for backward compatibility.
    """
    return SearchService.get_sidebar_filter_metadata(category, current_selections)

def paginate_products(
    products_qs: QuerySet, page_number: Union[str, int], items_per_page: int
) -> Any:
    """
    Paginates the given product queryset. Kept for backward
    compatibility.
    """
    return SearchService.paginate_products(products_qs, page_number, items_per_page)

# ==============================================================================
# PRODUCT SERVICE
# ==============================================================================
class ProductService:
    """
    Dedicated service class managing core Product queries, loading
    metadata, and inventory-context composition.

    This service does NOT mutate Products. It builds deeply
    optimized read-only querysets and attaches read-only
    inventory context.
    """

    @staticmethod
    def get_catalog_settings() -> CatalogSettings:
        """
        Retrieves the singleton CatalogSettings instance, creating
        a default one if none exists. This is the single canonical
        path for accessing CMS-driven catalog configuration.
        """
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
            # Last-resort: return a detached in-memory instance with
            # safe defaults so callers never crash.
            return CatalogSettings(
                default_items_per_page=12,
                price_filter_min=500,
                price_filter_max=100000,
                show_stock_warning_threshold=5,
            )

    @staticmethod
    def get_optimized_product_queryset() -> QuerySet:
        """
        Builds a standard optimized QuerySet utilizing select_related
        and prefetch_related to eliminate N+1 performance bottlenecks
        on complex templates. Includes inventory prefetches.
        """
        return _optimize_product_for_listing(Product.objects.all())

    @classmethod
    def get_product_by_id(cls, product_id: int) -> Optional[Product]:
        """
        Retrieves an optimized product by its primary key.
        """
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
        """
        Retrieves an optimized product by its unique slug identifier.
        """
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
        """
        Returns display-ordered specifications for a product.
        """
        if product is None or getattr(product, "pk", None) is None:
            return ProductSpecification.objects.none()
        try:
            return product.specifications.all().order_by("display_order", "label")
        except Exception as exc:
            logger.debug("get_product_specifications failed: %s", exc)
            return ProductSpecification.objects.none()

    @staticmethod
    def get_product_faqs(product: Product) -> QuerySet:
        """
        Returns active display-ordered FAQs linked to a product.
        """
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
        """
        Returns display-ordered multimedia/videos linked to a product.
        """
        if product is None or getattr(product, "pk", None) is None:
            return ProductVideo.objects.none()
        try:
            return product.videos.all().order_by("display_order", "id")
        except Exception as exc:
            logger.debug("get_product_videos failed: %s", exc)
            return ProductVideo.objects.none()

    @staticmethod
    def get_product_variants(product: Product) -> QuerySet:
        """
        Returns active, sorted variant options linked to a product.
        """
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
        """
        Returns a read-only inventory summary for a single product.
        Catalog never owns inventory; this is a lazy read.
        """
        return _build_product_inventory_payload(product)

    @staticmethod
    def get_inventory_check(
        product: Product,
        *,
        quantity: Any = 1,
        warehouse: Any = None,
    ) -> Dict[str, Any]:
        """
        Returns a read-only availability check for a single product.
        """
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
        """
        Returns currently featured, published, active products,
        each enriched with its read-only inventory summary.
        """
        qs = Product.objects.featured().order_by("position", "-created_at")
        qs = _optimize_product_for_listing(qs)
        products = list(qs[: max(1, int(limit) if limit else 12)])
        return _attach_inventory_summaries(products)

    @staticmethod
    def get_new_arrivals(limit: int = 12) -> List[Product]:
        """
        Returns recently published products, each enriched with
        its read-only inventory summary.
        """
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
        """
        Returns products ranked by denormalized wishlist + view
        counters, each enriched with its read-only inventory summary.
        """
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
        """
        Returns products ranked by recent view activity.
        """
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
        """
        Returns popular products by view count.
        """
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

# ==============================================================================
# CATEGORY SERVICE
# ==============================================================================
class CategoryService:
    """
    Dedicated service class handling Category tree traversal,
    breadcrumb mapping, and optimal database indexing structures.
    """

    @staticmethod
    def get_category_by_slug(slug: str) -> Optional[Category]:
        """
        Retrieves an active category from the database using its slug.
        """
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
        """
        Retrieves and structures the dynamic Category sidebar
        hierarchy list. Uses Django Prefetch to perform minimal
        SQL hits.
        """
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
        """
        Retrieves an aggregated mapping of Category IDs to active
        product counts.
        """
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

# ==============================================================================
# COLLECTION SERVICE
# ==============================================================================
class CollectionService:
    """
    Dedicated service class managing curated static, seasonal, or
    custom product collections.
    """

    @staticmethod
    def get_collection_by_slug(slug: str) -> Optional[ProductCollection]:
        """
        Retrieves a collection object using its slug with
        optimized product fetches.
        """
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
        """
        Returns all active collections ordered by their display
        priority.
        """
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
        """
        Retrieves optimized products mapping to a designated
        product collection.
        """
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

# ==============================================================================
# RECOMMENDATION SERVICE
# ==============================================================================
class RecommendationService:
    """
    Flexible Recommendation engine resolving similar craft lines,
    complementary artisan pieces, upsell/cross-sell configurations,
    or contextual fallbacks. Each result is enriched with a
    read-only inventory summary.
    """

    @staticmethod
    def get_related_products(
        product: Product, limit: int = 4
    ) -> List[Product]:
        """
        Fetches explicitly marked related products. Falls back
        gracefully onto the same Artisan or Category to safeguard
        design representation. Each result is enriched with a
        read-only inventory summary.
        """
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

            # Fallback 1: same artisan
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

            # Fallback 2: same category
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

    @staticmethod
    def get_upsell_products(
        product: Product, limit: int = 4
    ) -> List[Product]:
        """
        Retrieves upsell recommendations. Recommends higher-priced
        active items in same category. Each result is enriched with
        a read-only inventory summary.
        """
        if product is None or getattr(product, "pk", None) is None:
            return []
        try:
            upsell = (
                product.upsell_products.filter(
                    is_active=True, status=Product.ProductStatus.PUBLISHED
                )
                .select_related("category", "artisan", "material", "hue")
                .prefetch_related("ethical_standards")
            )
            result = list(upsell[: int(limit) if limit else 4])
            if result:
                return _attach_inventory_summaries(result)

            if product.category_id and product.price is not None:
                fallback_qs = (
                    Product.objects.filter(
                        category=product.category,
                        price__gt=product.price,
                        is_active=True,
                        status=Product.ProductStatus.PUBLISHED,
                    )
                    .exclude(id=product.id)
                    .select_related(
                        "category", "artisan", "material", "hue"
                    )
                    .prefetch_related("ethical_standards")
                    .order_by("price")
                )
                fallback_qs = _optimize_product_for_listing(fallback_qs)
                result = list(
                    fallback_qs[: int(limit) if limit else 4]
                )
                return _attach_inventory_summaries(result)
            return []
        except Exception as exc:
            logger.debug("get_upsell_products failed: %s", exc)
            return []

    @staticmethod
    def get_cross_sell_products(
        product: Product, limit: int = 4
    ) -> List[Product]:
        """
        Retrieves cross-sell recommendations. Recommends matching
        material handicrafts. Each result is enriched with a
        read-only inventory summary.
        """
        if product is None or getattr(product, "pk", None) is None:
            return []
        try:
            cross_sell = (
                product.cross_sell_products.filter(
                    is_active=True, status=Product.ProductStatus.PUBLISHED
                )
                .select_related("category", "artisan", "material", "hue")
                .prefetch_related("ethical_standards")
            )
            result = list(cross_sell[: int(limit) if limit else 4])
            if result:
                return _attach_inventory_summaries(result)

            if product.material_id:
                fallback_qs = (
                    Product.objects.filter(
                        material=product.material,
                        is_active=True,
                        status=Product.ProductStatus.PUBLISHED,
                    )
                    .exclude(id=product.id)
                    .select_related(
                        "category", "artisan", "material", "hue"
                    )
                    .prefetch_related("ethical_standards")
                    .order_by("position")
                )
                fallback_qs = _optimize_product_for_listing(fallback_qs)
                result = list(
                    fallback_qs[: int(limit) if limit else 4]
                )
                return _attach_inventory_summaries(result)
            return []
        except Exception as exc:
            logger.debug("get_cross_sell_products failed: %s", exc)
            return []

# ==============================================================================
# RECENTLY VIEWED SERVICE
# ==============================================================================
class RecentlyViewedService:
    """
    Maintains and queries the contextual browsing history for
    authenticated users or anonymous sessions.
    """

    _MAX_RECENTLY_VIEWED = 20

    @staticmethod
    def add_to_recently_viewed(
        product: Product, user: Any = None, session_key: Optional[str] = None
    ) -> Optional[RecentlyViewedProduct]:
        """
        Adds a product entry to historical browser queue. Prevents
        duplicate values and trims historical data to prevent
        unnecessary database bloat.
        """
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

            # Retain browsing queue size limits (max 20 records)
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

    @staticmethod
    def get_recently_viewed(
        user: Any = None, session_key: Optional[str] = None, limit: int = 6
    ) -> List[Product]:
        """
        Retrieves the ordered browser history items mapped to
        user/session parameters. Each result is enriched with a
        read-only inventory summary.
        """
        if not user and not session_key:
            return []
        try:
            user_id = user.id if (user and getattr(user, "is_authenticated", False)) else None

            qs = RecentlyViewedProduct.objects.all().select_related(
                "product",
                "product__category",
                "product__artisan",
                "product__material",
                "product__hue",
            )

            if user_id:
                qs = qs.filter(user_id=user_id)
            else:
                qs = qs.filter(session_key=session_key)

            product_ids = list(
                qs.order_by("-viewed_at").values_list("product_id", flat=True)[
                    : int(limit) if limit else 6
                ]
            )
            if not product_ids:
                return []

            # Preserves initial dynamic list position ranking inside
            # Django SQL compilation
            preserved_order = Case(
                *[
                    When(pk=pk, then=pos)
                    for pos, pk in enumerate(product_ids)
                ]
            )
            products_qs = (
                Product.objects.filter(
                    id__in=product_ids, is_active=True
                )
                .select_related(
                    "category", "artisan", "material", "hue"
                )
                .order_by(preserved_order)
            )
            products_qs = _optimize_product_for_listing(products_qs)
            products = list(products_qs)
            return _attach_inventory_summaries(products)
        except Exception as exc:
            logger.debug("get_recently_viewed failed: %s", exc)
            return []

# ==============================================================================
# BREADCRUMB SERVICE
# ==============================================================================
class BreadcrumbService:
    """
    Builds consistent structural link matrices suitable for search
    engine bots and nested navigation links.
    """

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
        if product is not None and getattr(product, "category", None):
            breadcrumbs = cls.build_for_category(product.category)
        else:
            breadcrumbs = cls.build_for_home()
            breadcrumbs.append({"label": "Handicrafts", "url": "#"})
        if product is not None:
            breadcrumbs.append(
                {
                    "label": product.title or "Unnamed",
                    "url": "#",
                }
            )
        return breadcrumbs

    @classmethod
    def build_for_collection(
        cls, collection: ProductCollection
    ) -> List[Dict[str, str]]:
        breadcrumbs = cls.build_for_home()
        breadcrumbs.append({"label": "Collections", "url": "#"})
        if collection is not None:
            breadcrumbs.append(
                {
                    "label": collection.name or "Unnamed",
                    "url": f"/collection/{collection.slug}/"
                    if getattr(collection, "slug", None)
                    else "#",
                }
            )
        return breadcrumbs

# ==============================================================================
# SEARCH SERVICE
# ==============================================================================
class SearchService:
    """
    Aggregated search, dynamic facets, multi-attribute indexing,
    sorting rules, and fallback navigation pipelines.

    Inventory-aware optimization is delegated to the inventory
    selectors / services when needed. Catalog services do not
    calculate or persist any inventory state.
    """

    _ALLOWED_SORTS = frozenset(
        {
            "featured",
            "newest",
            "oldest",
            "price_low",
            "price_high",
            "price-asc",
            "price-desc",
            "popularity",
            "rating",
            "name_asc",
            "name_desc",
        }
    )

    @staticmethod
    def query_products_for_category(
        category: Category,
        *,
        sort_by: str = "featured",
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        selected_materials: Optional[List[str]] = None,
        selected_artisans: Optional[List[str]] = None,
        selected_origins: Optional[List[str]] = None,
        selected_hues: Optional[List[str]] = None,
        selected_ethical_standards: Optional[List[str]] = None,
    ) -> QuerySet:
        """
        Builds the filtered, sorted product QuerySet for a given
        Category (including subcategories). Returns an UN-EVALUATED
        queryset so callers can apply pagination and inventory
        prefetching.
        """
        if category is None:
            return Product.objects.none()

        # 1. Resolve subcategories
        category_ids = [category.id]
        if not category.parent_id:
            try:
                sub_ids = list(
                    category.subcategories.filter(
                        is_active=True
                    ).values_list("id", flat=True)
                )
                category_ids.extend(sub_ids)
            except Exception as exc:
                logger.debug("Subcategory resolution failed: %s", exc)

        # 2. Base Active QuerySet
        qs = (
            Product.objects.filter(
                category_id__in=category_ids, is_active=True
            )
            .select_related("category", "artisan", "material", "hue")
            .prefetch_related("ethical_standards")
        )

        # 3. Apply Filters
        if min_price is not None:
            try:
                if Decimal(min_price) >= 0:
                    qs = qs.filter(price__gte=min_price)
            except (InvalidOperation, TypeError, ValueError):
                pass
        if max_price is not None:
            try:
                if Decimal(max_price) >= 0:
                    qs = qs.filter(price__lte=max_price)
            except (InvalidOperation, TypeError, ValueError):
                pass
        if selected_materials:
            qs = qs.filter(material__name__in=selected_materials)
        if selected_artisans:
            qs = qs.filter(artisan__slug__in=selected_artisans)
        if selected_origins:
            qs = qs.filter(artisan__region__in=selected_origins)
        if selected_hues:
            qs = qs.filter(hue__name__in=selected_hues)
        if selected_ethical_standards:
            qs = qs.filter(
                ethical_standards__name__in=selected_ethical_standards
            )

        # 4. Apply Sorting (whitelisted)
        if sort_by not in SearchService._ALLOWED_SORTS:
            sort_by = "featured"
        if sort_by == "newest":
            qs = qs.order_by("-created_at", "position")
        elif sort_by == "oldest":
            qs = qs.order_by("created_at", "position")
        elif sort_by in {"price_low", "price-asc"}:
            qs = qs.order_by("price", "position")
        elif sort_by in {"price_high", "price-desc"}:
            qs = qs.order_by("-price", "position")
        elif sort_by == "rating":
            qs = qs.order_by("-rating", "position")
        elif sort_by == "popularity":
            qs = qs.order_by("-wishlist_count", "-view_count")
        elif sort_by == "name_asc":
            qs = qs.order_by("title", "position")
        elif sort_by == "name_desc":
            qs = qs.order_by("-title", "position")
        else:  # featured (default)
            qs = qs.order_by("position", "-created_at")

        return qs.distinct()

    @staticmethod
    def get_sidebar_filter_metadata(
        category: Category, current_selections: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Compiles distinct filter values from all active products
        in this category tree to populate sidebar options
        dynamically. Returns a structured dictionary of facet
        metadata.
        """
        if category is None:
            return {
                "categories": CategoryService.get_active_categories_hierarchy(),
                "materials": [],
                "artisans": [],
                "origins": [],
                "hues": [],
                "ethical": [],
                "price_bounds": {"min": 0, "max": 0},
            }

        category_ids = [category.id]
        try:
            if not category.parent_id:
                category_ids.extend(
                    list(
                        category.subcategories.filter(
                            is_active=True
                        ).values_list("id", flat=True)
                    )
                )
        except Exception as exc:
            logger.debug("Sidebar subcategory resolution failed: %s", exc)

        try:
            base_qs = Product.objects.filter(
                category_id__in=category_ids, is_active=True
            )

            # Fetch distinct criteria from matching products
            materials = list(
                Material.objects.filter(products__in=base_qs)
                .distinct()
                .values_list("name", flat=True)
            )
            artisans = list(
                Artisan.objects.filter(
                    products__in=base_qs, is_active=True
                )
                .distinct()
                .values("name", "slug")
            )
            origins = list(
                base_qs.exclude(artisan__region="")
                .values_list("artisan__region", flat=True)
                .distinct()
            )
            hues = list(
                Hue.objects.filter(products__in=base_qs)
                .distinct()
                .values("name", "color_code")
            )
            ethical_standards = list(
                EthicalStandard.objects.filter(products__in=base_qs)
                .distinct()
                .values_list("name", flat=True)
            )

            # Aggregate min/max prices (ignoring NULLs)
            price_stats = base_qs.aggregate(
                min_p=Min("price"), max_p=Max("price")
            )
            try:
                min_price_found = int(price_stats["min_p"] or 0)
            except (TypeError, ValueError):
                min_price_found = 0
            try:
                max_price_found = int(price_stats["max_p"] or 0)
            except (TypeError, ValueError):
                max_price_found = 0

            current_selections = current_selections or {}

            return {
                "categories": CategoryService.get_active_categories_hierarchy(),
                "materials": [
                    {
                        "name": mat,
                        "checked": mat in current_selections.get(
                            "materials", []
                        ),
                    }
                    for mat in materials
                ],
                "artisans": [
                    {
                        "name": art["name"],
                        "slug": art["slug"],
                        "checked": art["slug"]
                        in current_selections.get("artisans", []),
                    }
                    for art in artisans
                ],
                "origins": [
                    {
                        "name": orig,
                        "checked": orig
                        in current_selections.get("origins", []),
                    }
                    for orig in origins
                ],
                "hues": [
                    {
                        "name": hue["name"],
                        "color": hue["color_code"],
                        "checked": hue["name"]
                        in current_selections.get("hues", []),
                    }
                    for hue in hues
                ],
                "ethical": [
                    {
                        "name": std,
                        "checked": std
                        in current_selections.get("ethical", []),
                    }
                    for std in ethical_standards
                ],
                "price_bounds": {
                    "min": min_price_found,
                    "max": max_price_found,
                },
            }
        except Exception as exc:
            logger.debug("get_sidebar_filter_metadata failed: %s", exc)
            return {
                "categories": CategoryService.get_active_categories_hierarchy(),
                "materials": [],
                "artisans": [],
                "origins": [],
                "hues": [],
                "ethical": [],
                "price_bounds": {"min": 0, "max": 0},
            }

    @staticmethod
    def paginate_products(
        products_qs: QuerySet, page_number: Union[str, int], items_per_page: int
    ) -> Any:
        """
        Applies pagination structures with fallback safety
        mechanisms. Validates inputs and never raises.
        """
        try:
            safe_per_page = int(items_per_page) if items_per_page else 12
            if safe_per_page < 1:
                safe_per_page = 12
            # Cap to prevent abuse
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
        query: str, limit: int = 50
    ) -> List[Product]:
        """
        Performs a global product search across title, description,
        SKU, category, material, and artisan name. Each result is
        enriched with a read-only inventory summary.
        """
        if not query or not isinstance(query, str):
            return []
        query = query.strip()
        if not query:
            return []
        try:
            qs = (
                Product.objects.filter(
                    is_active=True,
                    status=Product.ProductStatus.PUBLISHED,
                )
                .filter(
                    Q(title__icontains=query)
                    | Q(short_description__icontains=query)
                    | Q(description__icontains=query)
                    | Q(sku__icontains=query)
                    | Q(barcode__icontains=query)
                    | Q(category__name__icontains=query)
                    | Q(material__name__icontains=query)
                    | Q(artisan__name__icontains=query)
                )
                .distinct()
            )
            qs = _optimize_product_for_listing(qs)
            products = list(qs[: int(limit) if limit else 50])
            return _attach_inventory_summaries(products)
        except Exception as exc:
            logger.debug("global_search failed: %s", exc)
            return []

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Caching helpers
    "CATALOG_CACHE_VERSION",
    "CATALOG_CACHE_TIMEOUT",
    "CATALOG_LIST_CACHE_PREFIX",
    "invalidate_catalog_cache",
    # Backward-compatible top-level functions
    "get_catalog_settings",
    "get_category_by_slug",
    "get_active_categories_hierarchy",
    "query_products_for_category",
    "get_sidebar_filter_metadata",
    "paginate_products",
    # Service classes
    "ProductService",
    "CategoryService",
    "CollectionService",
    "RecommendationService",
    "RecentlyViewedService",
    "BreadcrumbService",
    "SearchService",
    # Internal helpers (exposed for advanced consumers)
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

# ==============================================================================
# PUBLIC HELPER ALIASES
# ==============================================================================
# These are public, re-exported helpers that other modules and
# management commands can rely on without going through the
# service classes. They preserve the old API surface for backward
# compatibility while routing through the new architecture.
# ==============================================================================
def optimize_product_queryset(qs: QuerySet) -> QuerySet:
    """Public alias for ``_optimize_product_for_listing``."""
    return _optimize_product_for_listing(qs)

def optimize_product_for_listing(qs: QuerySet) -> QuerySet:
    """Public alias for ``_optimize_product_for_listing``."""
    return _optimize_product_for_listing(qs)

def optimize_product_for_detail(qs: QuerySet) -> QuerySet:
    """Public alias for ``_optimize_product_for_detail``."""
    return _optimize_product_for_detail(qs)

def optimize_variant_queryset(qs: QuerySet) -> QuerySet:
    """Public alias for ``_optimize_variant_queryset``."""
    return _optimize_variant_queryset(qs)

def attach_inventory_summaries(products: Iterable[Product]) -> List[Product]:
    """Public alias for ``_attach_inventory_summaries``."""
    return _attach_inventory_summaries(products)

def build_product_inventory_payload(product: Product) -> Dict[str, Any]:
    """Public alias for ``_build_product_inventory_payload``."""
    return _build_product_inventory_payload(product)

def build_variant_inventory_payload(variant: Any) -> Dict[str, Any]:
    """Public alias for ``_build_variant_inventory_payload``."""
    return _build_variant_inventory_payload(variant)

def get_inventory_summary(product: Product) -> Dict[str, Any]:
    """Public helper. Alias for ``ProductService.get_inventory_summary``."""
    return ProductService.get_inventory_summary(product)

def get_inventory_check(
    product: Product,
    *,
    quantity: Any = 1,
    warehouse: Any = None,
) -> Dict[str, Any]:
    """Public helper. Alias for ``ProductService.get_inventory_check``."""
    return ProductService.get_inventory_check(
        product=product, quantity=quantity, warehouse=warehouse
    )

def get_featured_products(limit: int = 12) -> List[Product]:
    """Public helper. Alias for ``ProductService.get_featured_products``."""
    return ProductService.get_featured_products(limit=limit)

def get_new_arrivals(limit: int = 12) -> List[Product]:
    """Public helper. Alias for ``ProductService.get_new_arrivals``."""
    return ProductService.get_new_arrivals(limit=limit)

def get_best_sellers(limit: int = 12) -> List[Product]:
    """Public helper. Alias for ``ProductService.get_best_sellers``."""
    return ProductService.get_best_sellers(limit=limit)

def get_trending_products(limit: int = 12) -> List[Product]:
    """Public helper. Alias for ``ProductService.get_trending_products``."""
    return ProductService.get_trending_products(limit=limit)

def get_popular_products(limit: int = 12) -> List[Product]:
    """Public helper. Alias for ``ProductService.get_popular_products``."""
    return ProductService.get_popular_products(limit=limit)

def global_search(query: str, limit: int = 50) -> List[Product]:
    """Public helper. Alias for ``SearchService.global_search``."""
    return SearchService.global_search(query, limit=limit)