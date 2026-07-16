"""
Enterprise-grade views for the Catalog application.

ARCHITECTURE OVERVIEW
====================

This module implements the complete presentation layer for the catalog domain.
The catalog is intentionally INVENTORY-AGNOSTIC:

    * Products and variants NEVER carry stock information directly.
    * All inventory data is dynamically retrieved from the Inventory
      application via lazy imports and the selector / service layers.
    * Read-only inventory context is attached to the template context
      for backward compatibility with existing templates.

The views delegate:
    * Filtering / sorting / pagination to ``apps.catalog.services``
    * Read-only inventory access to ``apps.inventory.selectors``
    * Computed stock calculations to ``apps.inventory.services``
    * Breadcrumb / recommendation logic to dedicated services

Every product-related view enriches its template context with a
standardized inventory payload (read-only) sourced from the Inventory
application. Templates can safely rely on these keys:

    * ``inventory``             - Full serialized inventory summary
    * ``inventory_summary``     - Short human-readable summary string
    * ``inventory_status``      - One of: in_stock / low_stock / out_of_stock / unknown
    * ``available_quantity``    - Total gross available stock (string Decimal)
    * ``reserved_quantity``     - Total reserved stock (string Decimal)
    * ``free_stock``            - Sellable stock = available - reserved
    * ``warehouse_summary``     - Human-readable warehouse description
    * ``stock_message``         - UI-friendly stock message
    * ``is_in_stock``           - Convenience boolean
    * ``is_out_of_stock``       - Convenience boolean
    * ``is_low_stock``          - Convenience boolean

Backward compatibility aliases are also provided:
    * ``stock_status``          - Alias for ``inventory_status``
    * ``stock_text``            - Alias for ``stock_message``

Author: Handicraft E-commerce Engineering Team
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

# Core catalog models
from apps.catalog.models import (
    Artisan,
    Category,
    EthicalStandard,
    Hue,
    Material,
    Product,
    ProductCollection,
    ProductGalleryImage,
    ProductImage,
    ProductSchema,
    ProductSEO,
    ProductTag,
    ProductVariant,
)

# Catalog services
from apps.catalog.services import (
    CATALOG_CACHE_TIMEOUT,
    BreadcrumbService,
    CategoryService,
    CollectionService,
    ProductService,
    RecommendationService,
    RecentlyViewedService,
    SearchService,
    get_catalog_settings,
)

# Catalog forms
from apps.catalog.forms import (
    ProductForm,
    ProductSchemaForm,
    ProductSEOForm,
    PublishingWorkflowForm,
)

logger = logging.getLogger(__name__)

# ==============================================================================
# LAZY INVENTORY ACCESSORS
# ==============================================================================
# These helpers perform lazy imports of inventory modules. This is the
# authoritative pattern used throughout the catalog views to:
#   * Avoid circular imports between catalog and inventory
#   * Allow the catalog app to boot even if inventory is partially configured
#   * Keep the import graph small at module-load time
#   * Make it trivial to mock inventory in tests
# ==============================================================================
def _get_inventory_models() -> Tuple:
    """
    Lazy accessor for inventory models.

    Returns a tuple of (Inventory, InventoryTransaction,
    StockReservation, Warehouse) on success, or an empty tuple on
    ImportError. Callers MUST handle the empty-tuple case gracefully.
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
            "Inventory models could not be imported. Inventory context "
            "will be unavailable in catalog views."
        )
        return ()

def _get_inventory_selectors():
    """
    Lazy accessor for the inventory selectors module.
    Returns None on ImportError.
    """
    try:
        from apps.inventory import selectors
        return selectors
    except ImportError:
        logger.warning("Inventory selectors could not be imported.")
        return None

def _get_inventory_services():
    """
    Lazy accessor for the inventory services module.
    Returns None on ImportError.
    """
    try:
        from apps.inventory import services
        return services
    except ImportError:
        logger.warning("Inventory services could not be imported.")
        return None

# ==============================================================================
# SAFE INVENTORY WRAPPER
# ==============================================================================
def _safe_inventory_lookup(callable_obj, *args, **kwargs) -> Dict[str, Any]:
    """
    Safely invoke an inventory selector or service function, returning
    a structured empty payload on any failure. This guarantees that
    the catalog view layer NEVER crashes because of an inventory
    misconfiguration.
    """
    empty = _empty_inventory_context()
    if callable_obj is None:
        return empty
    try:
        result = callable_obj(*args, **kwargs)
        if isinstance(result, dict):
            return result
        return empty
    except Exception as exc:
        logger.debug("Safe inventory lookup failed: %s", exc)
        return empty

# ==============================================================================
# INVENTORY CONTEXT BUILDERS
# ==============================================================================
def _empty_inventory_context() -> Dict[str, Any]:
    """
    Returns a complete, safe-default inventory context dictionary.

    Every inventory-related context key is present, even when no
    inventory data is available. This guarantees that templates
    never encounter undefined variables.
    """
    return {
        "exists": False,
        "inventory": None,
        "inventory_summary": "Stock status unavailable",
        "inventory_status": "unknown",
        "available_quantity": "0.00",
        "reserved_quantity": "0.00",
        "free_stock": "0.00",
        "total_stock": "0.00",
        "warehouse_summary": "No warehouse",
        "warehouse_count": 0,
        "warehouse_id": None,
        "warehouse_name": None,
        "stock_message": "Stock status unavailable",
        "stock_status": "unknown",
        "stock_text": "Stock status unavailable",
        "is_in_stock": False,
        "is_out_of_stock": True,
        "is_low_stock": False,
        "variant_count": 0,
        "in_stock_variants": 0,
        "low_stock_variants": 0,
        "out_of_stock_variants": 0,
    }

def _get_low_stock_threshold() -> int:
    """
    Returns the CMS-driven low-stock threshold from catalog settings.

    Falls back to a safe default of 5 if the settings cannot be loaded.
    The catalog exposes this as a hint only - the Inventory app
    remains the single source of truth for actual stock state.
    """
    try:
        settings = get_catalog_settings()
        threshold = getattr(settings, "show_stock_warning_threshold", None)
        if threshold is None:
            return 5
        return int(threshold)
    except Exception:
        return 5


def _generate_stock_message(
    free_stock: Decimal,
    is_out_of_stock: bool,
    is_low_stock: bool,
) -> str:
    """
    Generate a user-friendly stock message for template rendering.
    """
    if is_out_of_stock:
        return "Out of stock"
    if is_low_stock:
        try:
            return f"Only {int(free_stock)} left in stock"
        except (TypeError, ValueError, InvalidOperation):
            return "Low stock - order soon"
    return "In stock"

def _generate_warehouse_summary(warehouse_ids: set, warehouse_name: Optional[str] = None) -> str:
    """
    Generate a human-readable warehouse summary string.
    """
    if not warehouse_ids:
        return "No warehouse"
    if len(warehouse_ids) == 1:
        if warehouse_name:
            return warehouse_name
        return "1 warehouse"
    return f"{len(warehouse_ids)} warehouses"

def _build_inventory_payload(
    *,
    total_available: Decimal,
    total_reserved: Decimal,
    warehouse_ids: set,
    warehouse_name: Optional[str] = None,
    variant_count: int = 0,
    in_stock_variants: int = 0,
    low_stock_variants: int = 0,
    out_of_stock_variants: int = 0,
) -> Dict[str, Any]:
    """
    Compose a complete inventory context payload from raw aggregate
    numbers. This is the single point where the standardized inventory
    context shape is defined.
    """
    free_stock = max(Decimal("0.00"), total_available - total_reserved)
    total_stock = total_available + total_reserved
    is_out_of_stock = free_stock <= Decimal("0.00")
    threshold = _get_low_stock_threshold()
    is_low_stock = (
        not is_out_of_stock
        and (low_stock_variants > 0 or (free_stock > 0 and free_stock <= Decimal(threshold)))
    )
    is_in_stock = free_stock > Decimal("0.00")

    if is_out_of_stock:
        status = "out_of_stock"
        status_label = "Out of Stock"
    elif is_low_stock:
        status = "low_stock"
        status_label = "Low Stock"
    elif is_in_stock:
        status = "in_stock"
        status_label = "In Stock"
    else:
        status = "unknown"
        status_label = "Stock Status Unknown"

    stock_message = _generate_stock_message(free_stock, is_out_of_stock, is_low_stock)
    warehouse_summary = _generate_warehouse_summary(warehouse_ids, warehouse_name)

    return {
        "exists": True,
        "inventory": {
            "available_quantity": str(total_available),
            "reserved_quantity": str(total_reserved),
            "free_stock": str(free_stock),
            "total_stock": str(total_stock),
            "warehouse_count": len(warehouse_ids),
            "warehouse_ids": list(warehouse_ids),
        },
        "inventory_summary": f"{status_label} - {stock_message}",
        "inventory_status": status,
        "available_quantity": str(total_available),
        "reserved_quantity": str(total_reserved),
        "free_stock": str(free_stock),
        "total_stock": str(total_stock),
        "warehouse_summary": warehouse_summary,
        "warehouse_count": len(warehouse_ids),
        "warehouse_id": next(iter(warehouse_ids)) if len(warehouse_ids) == 1 else None,
        "warehouse_name": warehouse_name if warehouse_name and len(warehouse_ids) <= 1 else None,
        "stock_message": stock_message,
        "stock_status": status,
        "stock_text": stock_message,
        "is_in_stock": is_in_stock,
        "is_out_of_stock": is_out_of_stock,
        "is_low_stock": is_low_stock,
        "variant_count": variant_count,
        "in_stock_variants": in_stock_variants,
        "low_stock_variants": low_stock_variants,
        "out_of_stock_variants": out_of_stock_variants,
    }

# ==============================================================================
# PRODUCT-LEVEL INVENTORY HELPERS
# ==============================================================================
def _summarize_variant_inventory_records(inventory_records: Iterable) -> Dict[str, Any]:
    """
    Summarize inventory across multiple inventory records
    (typically all inventory rows for a single variant or product).
    """
    records = list(inventory_records)
    if not records:
        return _empty_inventory_context()

    total_available = sum(
        (Decimal(str(r.available_quantity)) for r in records),
        Decimal("0.00"),
    )
    total_reserved = sum(
        (Decimal(str(r.reserved_quantity)) for r in records),
        Decimal("0.00"),
    )
    warehouse_ids = {r.warehouse_id for r in records if r.warehouse_id is not None}
    warehouse_name = None
    if len(records) == 1 and getattr(records[0], "warehouse", None) is not None:
        warehouse_name = getattr(records[0].warehouse, "display_name", None) or getattr(
            records[0].warehouse, "name", None
        )

    return _build_inventory_payload(
        total_available=total_available,
        total_reserved=total_reserved,
        warehouse_ids=warehouse_ids,
        warehouse_name=warehouse_name,
    )

def _summarize_product_inventory(
    product: Product,
    *,
    use_prefetch: bool = True,
) -> Dict[str, Any]:
    """
    Build a complete inventory payload for a single product.

    Strategy:
        1. If the product has active variants, aggregate inventory
           across all variants (variant-level stock).
        2. Otherwise, aggregate product-level inventory rows
           (where ``product_variant`` is NULL).
        3. If neither exists, return the safe-empty context.

    Uses prefetched data when available to avoid N+1 queries.
    Falls back to direct queries when prefetched data is absent.
    """
    if product is None or getattr(product, "pk", None) is None:
        return _empty_inventory_context()

    # 1. Try variant-level inventory
    variants_qs = product.variants.all() if use_prefetch else product.variants.filter(is_active=True)
    variants = list(variants_qs)

    if variants:
        return _summarize_variants_aggregated_inventory(variants)

    # 2. Try product-level inventory
    inv_qs = product.inventory_records.all() if use_prefetch else None
    if inv_qs is not None:
        product_records = list(inv_qs)
    else:
        models = _get_inventory_models()
        if not models:
            return _empty_inventory_context()
        Inventory = models[0]
        try:
            product_records = list(
                Inventory.objects.filter(
                    product=product,
                    product_variant__isnull=True,
                    is_active=True,
                ).select_related("warehouse")
            )
        except Exception as exc:
            logger.debug("Product-level inventory fetch failed: %s", exc)
            return _empty_inventory_context()

    if not product_records:
        return _empty_inventory_context()

    return _summarize_variant_inventory_records(product_records)

def _summarize_variants_aggregated_inventory(variants: List[ProductVariant]) -> Dict[str, Any]:
    """
    Aggregate inventory across multiple product variants.

    Uses prefetched data when available. For each variant, reads
    its ``inventory_records`` and sums up available/reserved stock.

    Tracks per-variant stock status to surface a meaningful
    aggregate status (e.g. "3 in stock, 1 low, 1 out of stock").
    """
    total_available = Decimal("0.00")
    total_reserved = Decimal("0.00")
    warehouse_ids = set()
    variant_count = 0
    in_stock_variants = 0
    low_stock_variants = 0
    out_of_stock_variants = 0

    for variant in variants:
        # Use prefetched inventory_records when available
        records_qs = variant.inventory_records.all() if hasattr(
            variant, "inventory_records"
        ) else None

        if records_qs is not None:
            records = list(records_qs)
        else:
            # Fallback: lazy query
            models = _get_inventory_models()
            if not models:
                continue
            Inventory = models[0]
            try:
                records = list(
                    Inventory.objects.filter(
                        product_variant=variant,
                        is_active=True,
                    ).select_related("warehouse")
                )
            except Exception:
                continue

        if not records:
            continue

        variant_count += 1
        variant_available = sum(
            (Decimal(str(r.available_quantity)) for r in records),
            Decimal("0.00"),
        )
        variant_reserved = sum(
            (Decimal(str(r.reserved_quantity)) for r in records),
            Decimal("0.00"),
        )
        variant_free = max(Decimal("0.00"), variant_available - variant_reserved)

        total_available += variant_available
        total_reserved += variant_reserved
        for record in records:
            if record.warehouse_id is not None:
                warehouse_ids.add(record.warehouse_id)

        if variant_free <= Decimal("0.00"):
            out_of_stock_variants += 1
        elif any(
            getattr(r, "is_low_stock", False) for r in records
        ):
            low_stock_variants += 1
        else:
            in_stock_variants += 1

    if variant_count == 0:
        return _empty_inventory_context()

    return _build_inventory_payload(
        total_available=total_available,
        total_reserved=total_reserved,
        warehouse_ids=warehouse_ids,
        variant_count=variant_count,
        in_stock_variants=in_stock_variants,
        low_stock_variants=low_stock_variants,
        out_of_stock_variants=out_of_stock_variants,
    )

def _build_inventory_context_for_variant(variant: ProductVariant) -> Dict[str, Any]:
    """
    Build inventory context for a single product variant.

    Uses prefetched data when available (i.e. when the parent
    product queryset was optimized with the inventory prefetch).
    Falls back to a direct query when prefetched data is absent.
    """
    if variant is None or getattr(variant, "pk", None) is None:
        return _empty_inventory_context()

    # Use prefetched data if available
    records_qs = variant.inventory_records.all() if hasattr(
        variant, "inventory_records"
    ) else None

    if records_qs is not None:
        records = list(records_qs)
    else:
        models = _get_inventory_models()
        if not models:
            return _empty_inventory_context()
        Inventory = models[0]
        try:
            records = list(
                Inventory.objects.filter(
                    product_variant=variant,
                    is_active=True,
                ).select_related("warehouse")
            )
        except Exception as exc:
            logger.debug("Variant inventory fetch failed: %s", exc)
            return _empty_inventory_context()

    if not records:
        return _empty_inventory_context()

    return _summarize_variant_inventory_records(records)

def _build_inventory_context_for_product(product: Product) -> Dict[str, Any]:
    """
    Public helper: build the standardized inventory context for a
    single product. Uses prefetched data when available.
    """
    if product is None:
        return _empty_inventory_context()
    return _summarize_product_inventory(product, use_prefetch=True)

# ==============================================================================
# PRODUCT QUERYSET OPTIMIZATION FOR INVENTORY
# ==============================================================================
def _optimize_product_queryset_with_inventory(qs):
    """
    Optimize a product queryset to avoid N+1 queries when reading
    inventory data. Adds nested prefetches for:

        * Product variants (active only)
        * Variant-level inventory records (active only)
        * Product-level inventory records (active only, variant is NULL)
    """
    models = _get_inventory_models()
    if not models:
        return qs
    Inventory = models[0]

    variant_inventory_prefetch = Prefetch(
        "inventory_records",
        queryset=Inventory.objects.filter(is_active=True).select_related("warehouse"),
    )

    variant_prefetch = Prefetch(
        "variants",
        queryset=ProductVariant.objects.filter(is_active=True).prefetch_related(
            variant_inventory_prefetch
        ),
    )

    product_inventory_prefetch = Prefetch(
        "inventory_records",
        queryset=Inventory.objects.filter(
            is_active=True,
            product_variant__isnull=True,
        ).select_related("warehouse"),
    )

    return qs.prefetch_related(variant_prefetch, product_inventory_prefetch)

def _attach_inventory_summaries(products: Iterable[Product]) -> List[Product]:
    """
    Process an iterable of products and attach a standardized
    ``inventory_summary`` attribute to each one. Returns the list
    of products for convenience.

    Uses prefetched data when available. The attachment is done
    as a view-layer concern, never as a model concern.
    """
    product_list = list(products)
    for product in product_list:
        try:
            product.inventory_summary = _build_inventory_context_for_product(product)
        except Exception as exc:
            logger.debug(
                "Failed to attach inventory summary for product %s: %s",
                getattr(product, "pk", "?"),
                exc,
            )
            product.inventory_summary = _empty_inventory_context()
    return product_list

def _process_products_with_inventory(qs) -> List[Product]:
    """
    Optimize a product queryset with inventory prefetching and
    attach inventory summaries to each product. This is the
    canonical helper for listing views.
    """
    optimized_qs = _optimize_product_queryset_with_inventory(qs)
    products = list(optimized_qs)
    return _attach_inventory_summaries(products)

# ==============================================================================
# PERMISSION MIXIN
# ==============================================================================
class ProductManagementMixin(UserPassesTestMixin):
    """
    Base access mixin for the Product Management module.

    Restricts access to staff or authorized product managers. This
    mixin is intentionally minimal and serves ONLY as an access
    control gate. It does NOT touch inventory.
    """

    def test_func(self) -> bool:
        return bool(
            self.request.user
            and self.request.user.is_authenticated
            and self.request.user.is_staff
        )

    def handle_no_permission(self):
        try:
            messages.error(
                self.request,
                "You do not have permission to access the Product Management Module.",
            )
        except Exception:
            pass
        return super().handle_no_permission()

# ==============================================================================
# PUBLIC-FACING CATALOG VIEWS
# ==============================================================================
class CategoryListingView(TemplateView):
    """
    Public product listing and merchandising discovery view for
    categories. Delegates dynamic faceted filtering, sorting,
    pagination, and structural breadcrumbs to modular services.

    Inventory is loaded dynamically and attached to every product
    in the page via ``product.inventory_summary`` (a standardized
    read-only context payload).
    """

    template_name = "catalog/product-list.html"
    slug: Optional[str] = None  # Set via URL conf for legacy routing

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        slug = self.slug or self.kwargs.get("slug", "")

        # Safe cache extraction of Category details
        category = cache.get_or_set(
            f"catalog:cat:slug:{slug}",
            lambda: CategoryService.get_category_by_slug(slug),
            CATALOG_CACHE_TIMEOUT,
        )

        if not category:
            raise Http404("Category not found")

        # Extract parameters for faceted searches (validated)
        sort_by = self.request.GET.get("sort", "featured") or "featured"

        price_max_raw = self.request.GET.get("price_max")
        price_max: Optional[Decimal] = None
        if price_max_raw:
            try:
                price_max = Decimal(price_max_raw)
                if price_max < 0:
                    price_max = None
            except (InvalidOperation, ValueError, TypeError):
                price_max = None

        selected_materials = self.request.GET.getlist("material")
        selected_artisans = self.request.GET.getlist("artisan")
        selected_origins = self.request.GET.getlist("origin")
        selected_hues = self.request.GET.getlist("hue")
        selected_ethical = self.request.GET.getlist("ethical")

        # Whitelist sort values to prevent SQL injection / unsafe ordering
        allowed_sort_values = {
            "featured", "newest", "oldest", "price_low", "price_high",
            "popularity", "rating", "name_asc", "name_desc",
        }
        if sort_by not in allowed_sort_values:
            sort_by = "featured"

        # Delegation of query generation to the search services layer
        products_qs = SearchService.query_products_for_category(
            category=category,
            sort_by=sort_by,
            min_price=None,
            max_price=price_max,
            selected_materials=selected_materials,
            selected_artisans=selected_artisans,
            selected_origins=selected_origins,
            selected_hues=selected_hues,
            selected_ethical_standards=selected_ethical,
        )

        # Optimize with inventory prefetching BEFORE pagination
        products_qs = _optimize_product_queryset_with_inventory(products_qs)

        # Pagination resolution
        catalog_settings = cache.get_or_set(
            "catalog:settings",
            get_catalog_settings,
            CATALOG_CACHE_TIMEOUT,
        )
        page_number = self.request.GET.get("page", 1)
        try:
            page_number = int(page_number)
            if page_number < 1:
                page_number = 1
        except (TypeError, ValueError):
            page_number = 1

        paginated_products = SearchService.paginate_products(
            products_qs,
            page_number,
            catalog_settings.default_items_per_page,
        )

        # Attach inventory summaries to the current page
        page_products = list(getattr(paginated_products, "object_list", paginated_products))
        _attach_inventory_summaries(page_products)

        # Facets and UI criteria details
        current_selections = {
            "materials": selected_materials,
            "artisans": selected_artisans,
            "origins": selected_origins,
            "hues": selected_hues,
            "ethical": selected_ethical,
        }
        sidebar_filters = SearchService.get_sidebar_filter_metadata(
            category, current_selections
        )

        min_bound = sidebar_filters["price_bounds"]["min"] or catalog_settings.price_filter_min
        max_bound = sidebar_filters["price_bounds"]["max"] or catalog_settings.price_filter_max
        current_val = int(price_max) if price_max is not None else max_bound

        # Breadcrumbs built by dedicated builder service
        breadcrumbs = BreadcrumbService.build_for_category(category)

        context.update(
            {
                "category": category,
                "title": category.seo_title or category.name,
                "description": category.seo_description or category.description,
                "products": paginated_products,
                "total_products": products_qs.count(),
                "breadcrumbs": breadcrumbs,
                "filter_categories": sidebar_filters["categories"],
                "filter_materials": sidebar_filters["materials"],
                "filter_craftsmen": sidebar_filters["artisans"],
                "filter_provenance": sidebar_filters["origins"],
                "filter_hues": sidebar_filters["hues"],
                "filter_ethical": sidebar_filters["ethical"],
                "filter_price": {
                    "min": min_bound,
                    "max": max_bound,
                    "current": current_val,
                    "min_formatted": f"{min_bound:,.0f}",
                    "current_formatted": f"{current_val:,.0f}",
                },
                "current_sort": sort_by,
            }
        )
        return context

class ArtisansListView(ListView):
    """
    Public listing of all active Master Craftsmen.
    Inventory is not relevant at the artisan level.
    """

    model = Artisan
    template_name = "catalog/artisans-list.html"
    context_object_name = "artisans"
    paginate_by = 24

    def get_queryset(self):
        return (
            Artisan.objects.filter(is_active=True)
            .annotate(product_count=Count("products", distinct=True))
            .order_by("position", "name")
        )

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "title": "Meet the Makers",
                "description": (
                    "We partner directly with over 150 master craftsmen, "
                    "ensuring fair wages, safe workshops, and the survival "
                    "of ancestral lineages."
                ),
            }
        )
        return context

class ArtisanDetailView(DetailView):
    """
    Public profile detail view for a single Artisan, including
    their masterpieces. Products are enriched with inventory
    summaries loaded dynamically from the Inventory app.
    """

    model = Artisan
    template_name = "catalog/artisan-detail.html"
    context_object_name = "artisan"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return Artisan.objects.filter(is_active=True)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        artisan = self.get_object()

        # Retrieve optimized artisan masterpieces
        products_qs = (
            Product.objects.filter(artisan=artisan, is_active=True)
            .select_related("category", "material", "hue")
            .prefetch_related("ethical_standards", "tags", "in_collections")
        )
        products = _process_products_with_inventory(products_qs)

        context.update(
            {
                "title": f"Master {artisan.name}",
                "description": (
                    artisan.bio
                    or f"Explore the exclusive collection and craft lineage "
                       f"of Master {artisan.name}."
                ),
                "products": products,
            }
        )
        return context

class ProductDetailView(DetailView):
    """
    Public product detail view. Enriches rendering with structured
    microdata schemas, SEO configuration profiles, and records
    client-side browsing context safely using the Recently Viewed
    Service layer.

    Inventory is loaded dynamically and exposed via a standardized
    read-only context payload. Backward-compatible aliases are
    provided for templates that previously assumed inventory was
    owned by the Product model.
    """

    model = Product
    template_name = "catalog/product-detail.html"
    context_object_name = "product"
    slug_url_kwarg = "slug"

    def get_object(self, queryset: Optional[Any] = None) -> Product:
        slug = self.kwargs.get(self.slug_url_kwarg)
        product = ProductService.get_product_by_slug(slug)
        if not product:
            raise Http404("Product not found")
        return product

    def get_queryset(self) -> Any:
        # Optimize the base queryset with related data and inventory
        qs = super().get_queryset()
        return _optimize_product_queryset_with_inventory(qs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        product = self.object

        # Build the standardized inventory payload for the main product
        inventory = _build_inventory_context_for_product(product)

        # Build breadcrumbs using the breadcrumb builder service
        breadcrumbs = BreadcrumbService.build_for_product(product)

        # Pull matching product recommendations safely through the
        # recommendation service. Enrich each related product with
        # inventory data for the "You may also like" section.
        try:
            related_products_qs = RecommendationService.get_related_products(
                product, limit=4
            )
            related_products = _process_products_with_inventory(related_products_qs)
        except Exception as exc:
            logger.debug("Related products fetch failed: %s", exc)
            related_products = []

        # Get variants and enrich each variant with its own inventory
        # context (so product detail pages can show per-variant stock).
        try:
            variants = list(ProductService.get_product_variants(product))
        except Exception as exc:
            logger.debug("Variant fetch failed: %s", exc)
            variants = list(product.get_active_variants()) if hasattr(product, "get_active_variants") else []

        variant_inventory_map: Dict[int, Dict[str, Any]] = {}
        for variant in variants:
            try:
                variant_inventory_map[variant.pk] = _build_inventory_context_for_variant(
                    variant
                )
            except Exception as exc:
                logger.debug(
                    "Variant inventory build failed for variant %s: %s",
                    getattr(variant, "pk", "?"),
                    exc,
                )
                variant_inventory_map[variant.pk] = _empty_inventory_context()

        # Resolve SEO fields cleanly
        seo_title = product.title
        seo_desc = product.short_description
        try:
            if hasattr(product, "seo_config") and product.seo_config:
                seo_title = (
                    product.seo_config.meta_title
                    or product.seo_title
                    or product.title
                )
                seo_desc = (
                    product.seo_config.meta_description
                    or product.seo_description
                    or product.short_description
                )
            else:
                seo_title = product.seo_title or product.title
                seo_desc = product.seo_description or product.short_description
        except Exception:
            seo_title = product.seo_title or product.title or ""
            seo_desc = product.seo_description or product.short_description or ""

        # Safely track browser browsing context
        if not self.request.session.session_key:
            try:
                self.request.session.create()
            except Exception:
                pass
        session_key = self.request.session.session_key or ""

        try:
            RecentlyViewedService.add_to_recently_viewed(
                product=product,
                user=self.request.user,
                session_key=session_key,
            )
        except Exception as exc:
            logger.debug("Recently viewed tracking failed: %s", exc)

        # Increment analytical display metric counts (catalog-side analytics)
        try:
            product.increment_view_count(commit=True)
        except Exception as exc:
            logger.debug("View count increment failed: %s", exc)

        # Pull active tags and collections safely
        try:
            active_tags = list(product.tags.filter(is_active=True))
        except Exception:
            active_tags = []
        try:
            active_collections = list(product.in_collections.filter(is_active=True))
        except Exception:
            active_collections = []

        context.update(
            {
                "title": seo_title,
                "description": seo_desc,
                "breadcrumbs": breadcrumbs,
                "related_products": related_products,
                "variants": variants,
                "variant_inventory_map": variant_inventory_map,
                "active_tags": active_tags,
                "active_collections": active_collections,
                # Standardized inventory context
                "inventory": inventory.get("inventory"),
                "inventory_summary": inventory.get("inventory_summary"),
                "inventory_status": inventory.get("inventory_status"),
                "available_quantity": inventory.get("available_quantity"),
                "reserved_quantity": inventory.get("reserved_quantity"),
                "free_stock": inventory.get("free_stock"),
                "warehouse_summary": inventory.get("warehouse_summary"),
                "stock_message": inventory.get("stock_message"),
                "is_in_stock": inventory.get("is_in_stock"),
                "is_out_of_stock": inventory.get("is_out_of_stock"),
                "is_low_stock": inventory.get("is_low_stock"),
                # Backward-compatible aliases
                "stock_status": inventory.get("stock_status"),
                "stock_text": inventory.get("stock_text"),
                "product_inventory": inventory,
            }
        )
        return context

# ==============================================================================
# DISCOVERY & MERCHANDISING VIEWS
# ==============================================================================
class ProductQuickViewView(DetailView):
    """
    Lightweight product detail view intended for AJAX quick-view
    modals. Inventory is loaded dynamically.
    """

    model = Product
    template_name = "catalog/product-quick-view.html"
    context_object_name = "product"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        qs = (
            Product.objects.filter(
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            )
            .select_related("category", "artisan", "material", "hue")
            .prefetch_related(
                "gallery_images",
                "variants",
                "ethical_standards",
                "tags",
                "in_collections",
            )
        )
        return _optimize_product_queryset_with_inventory(qs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        product = self.object

        inventory = _build_inventory_context_for_product(product)

        # Per-variant inventory for quick-view selectors
        variant_inventory_map: Dict[int, Dict[str, Any]] = {}
        try:
            variants = list(product.variants.filter(is_active=True))
        except Exception:
            variants = []
        for variant in variants:
            try:
                variant_inventory_map[variant.pk] = _build_inventory_context_for_variant(
                    variant
                )
            except Exception:
                variant_inventory_map[variant.pk] = _empty_inventory_context()

        try:
            related_products = _process_products_with_inventory(
                RecommendationService.get_related_products(product, limit=4)
            )
        except Exception:
            related_products = []

        context.update(
            {
                "variants": variants,
                "variant_inventory_map": variant_inventory_map,
                "gallery": list(product.gallery_images.all()),
                "related_products": related_products,
                "inventory": inventory.get("inventory"),
                "inventory_summary": inventory.get("inventory_summary"),
                "inventory_status": inventory.get("inventory_status"),
                "available_quantity": inventory.get("available_quantity"),
                "reserved_quantity": inventory.get("reserved_quantity"),
                "free_stock": inventory.get("free_stock"),
                "warehouse_summary": inventory.get("warehouse_summary"),
                "stock_message": inventory.get("stock_message"),
                "is_in_stock": inventory.get("is_in_stock"),
                "is_out_of_stock": inventory.get("is_out_of_stock"),
                "is_low_stock": inventory.get("is_low_stock"),
                "stock_status": inventory.get("stock_status"),
                "stock_text": inventory.get("stock_text"),
                "product_inventory": inventory,
            }
        )
        return context

class ProductSearchView(ListView):
    """
    Product search results with dynamic inventory enrichment.
    """

    model = Product
    template_name = "catalog/product-search.html"
    context_object_name = "products"
    paginate_by = 12

    def get_queryset(self):
        query = (self.request.GET.get("q", "") or "").strip()

        qs = (
            Product.objects.filter(
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            )
            .select_related(
                "category",
                "artisan",
                "material",
                "hue",
            )
            .prefetch_related(
                "tags",
                "in_collections",
            )
        )

        if query:
            qs = qs.filter(
                Q(title__icontains=query)
                | Q(short_description__icontains=query)
                | Q(description__icontains=query)
                | Q(sku__icontains=query)
                | Q(category__name__icontains=query)
                | Q(material__name__icontains=query)
                | Q(artisan__name__icontains=query)
            ).distinct()

        # Whitelist ordering to prevent unsafe user-controlled ordering
        allowed_orderings = {
            "position": "position",
            "-position": "-position",
            "-created_at": "-created_at",
            "created_at": "created_at",
            "title": "title",
            "-title": "-title",
            "price": "price",
            "-price": "-price",
        }
        order_by = self.request.GET.get("order_by", "position")
        if order_by not in allowed_orderings:
            order_by = "position"
        qs = qs.order_by(allowed_orderings[order_by], "-created_at")

        return qs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)

        query = (self.request.GET.get("q", "") or "").strip()
        products_qs = self.get_queryset()

        # Get total count BEFORE pagination
        total_count = products_qs.count()

        # Optimize with inventory and paginate
        products_qs = _optimize_product_queryset_with_inventory(products_qs)
        paginator_attr = context.get("paginator")
        page_obj = context.get("page_obj")
        if paginator_attr is not None and page_obj is not None:
            page_products = list(page_obj.object_list)
            _attach_inventory_summaries(page_products)
            page_obj.object_list = page_products

        context.update(
            {
                "search_query": query,
                "title": f"Search: {query}" if query else "Search Products",
                "description": (
                    f"Search results for '{query}'." if query
                    else "Browse our handcrafted products."
                ),
                "total_products": total_count,
            }
        )
        return context

class CollectionView(DetailView):
    """
    Displays all products belonging to a ProductCollection.
    Each product is enriched with dynamic inventory data.
    """

    model = ProductCollection
    template_name = "catalog/collection-detail.html"
    context_object_name = "collection"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return ProductCollection.objects.filter(is_active=True)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        collection = self.object

        products_qs = (
            collection.products.filter(
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            )
            .select_related(
                "category",
                "artisan",
                "material",
                "hue",
            )
            .prefetch_related(
                "tags",
                "gallery_images",
            )
        )
        products = _process_products_with_inventory(products_qs)

        context.update(
            {
                "products": products,
                "title": collection.name,
                "description": collection.description,
                "breadcrumbs": [
                    (
                        "Collections",
                        reverse(
                            "catalog:collection_detail",
                            kwargs={"slug": collection.slug},
                        ),
                    ),
                    (collection.name, ""),
                ],
            }
        )
        return context

class MaterialView(DetailView):
    """
    Displays all products using a particular material. Each
    product is enriched with dynamic inventory data.
    """

    model = Material
    template_name = "catalog/material-detail.html"
    context_object_name = "material"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return Material.objects.all()

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        material = self.object

        products_qs = (
            Product.objects.filter(
                material=material,
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            )
            .select_related(
                "category",
                "artisan",
                "hue",
            )
            .prefetch_related(
                "gallery_images",
                "tags",
            )
            .order_by("position", "-created_at")
        )
        products = _process_products_with_inventory(products_qs)

        context.update(
            {
                "products": products,
                "title": material.name,
                "description": f"Products crafted from {material.name}.",
                "breadcrumbs": [
                    ("Materials", ""),
                    (material.name, ""),
                ],
            }
        )
        return context

# ==============================================================================
# ENTERPRISE PRODUCT MANAGEMENT MODULE (Staff / CMS Facing)
# ==============================================================================
class ProductManageListView(ProductManagementMixin, ListView):
    """
    Enterprise product listing for staff/admin dashboard.
    Supports deep searching, filtering by status/activity, and
    pagination. Inventory is not shown here for performance;
    click into a product to see the inventory summary.
    """

    model = Product
    template_name = "catalog/management/product_list.html"
    context_object_name = "products"
    paginate_by = 50

    def get_queryset(self):
        qs = (
            Product.objects.all()
            .select_related("category", "artisan", "material", "hue")
            .prefetch_related("tags", "variants", "ethical_standards")
            .order_by("-created_at")
        )

        # Retrieve Search Query
        q = (self.request.GET.get("q", "") or "").strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(sku__icontains=q)
                | Q(barcode__icontains=q)
            )

        # Status Filtering (whitelisted)
        status = self.request.GET.get("status", "")
        valid_statuses = {choice[0] for choice in Product.ProductStatus.choices}
        if status in valid_statuses:
            qs = qs.filter(status=status)

        # Activity Filtering
        is_active = self.request.GET.get("is_active", "")
        if is_active in ("1", "0"):
            qs = qs.filter(is_active=(is_active == "1"))

        return qs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "search_query": self.request.GET.get("q", ""),
                "status_filter": self.request.GET.get("status", ""),
                "active_filter": self.request.GET.get("is_active", ""),
                "status_choices": Product.ProductStatus.choices,
                "total_count": self.get_queryset().count(),
            }
        )
        return context

class ProductManageCreateView(ProductManagementMixin, CreateView):
    """
    Secure product creation view with transaction safety.
    """

    model = Product
    form_class = ProductForm
    template_name = "catalog/management/product_form.html"

    def get_success_url(self):
        try:
            messages.success(
                self.request,
                f"Product '{self.object.title}' created successfully. "
                "You can now configure variants and SEO.",
            )
        except Exception:
            pass
        return reverse(
            "catalog:product_manage_update",
            kwargs={"pk": self.object.pk},
        )

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["is_create"] = True
        context["title"] = "Create New Product Masterpiece"
        return context

    @transaction.atomic
    def form_valid(self, form: ProductForm):
        # Force initial status to Draft upon creation unless explicitly
        # published by an authorized admin.
        try:
            if not form.cleaned_data.get("status"):
                form.instance.status = Product.ProductStatus.DRAFT
        except Exception:
            form.instance.status = Product.ProductStatus.DRAFT
        return super().form_valid(form)

class ProductManageUpdateView(ProductManagementMixin, UpdateView):
    """
    Comprehensive product update view handling core data, SEO
    configuration, structured schema data, and publication state
    changes. Inventory is shown as a read-only summary sourced
    from the Inventory app.
    """

    model = Product
    form_class = ProductForm
    template_name = "catalog/management/product_form.html"

    def get_queryset(self):
        return _optimize_product_queryset_with_inventory(super().get_queryset())

    def get_success_url(self):
        return reverse(
            "catalog:product_manage_update",
            kwargs={"pk": self.object.pk},
        )

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        product = self.object

        # Build read-only inventory summary for the management view
        inventory = _build_inventory_context_for_product(product)

        # Initialize supplementary forms
        try:
            seo_instance = getattr(product, "seo_config", None)
        except Exception:
            seo_instance = None
        try:
            schema_instance = getattr(product, "schema_config", None)
        except Exception:
            schema_instance = None

        if self.request.method == "POST":
            context["seo_form"] = ProductSEOForm(
                self.request.POST,
                self.request.FILES,
                instance=seo_instance,
                prefix="seo",
            )
            context["schema_form"] = ProductSchemaForm(
                self.request.POST,
                instance=schema_instance,
                prefix="schema",
            )
            context["publish_form"] = PublishingWorkflowForm(
                self.request.POST,
                instance=product,
                prefix="publish",
            )
        else:
            context["seo_form"] = ProductSEOForm(
                instance=seo_instance,
                prefix="seo",
            )
            context["schema_form"] = ProductSchemaForm(
                instance=schema_instance,
                prefix="schema",
            )
            context["publish_form"] = PublishingWorkflowForm(
                instance=product,
                prefix="publish",
            )

        context["is_create"] = False
        context["title"] = f"Edit Product: {product.title}"
        try:
            context["variants"] = list(product.variants.all())
        except Exception:
            context["variants"] = []
        try:
            context["gallery"] = list(product.gallery_images.all())
        except Exception:
            context["gallery"] = []

        # Read-only inventory context for the management UI
        context["inventory"] = inventory.get("inventory")
        context["inventory_summary"] = inventory.get("inventory_summary")
        context["inventory_status"] = inventory.get("inventory_status")
        context["available_quantity"] = inventory.get("available_quantity")
        context["reserved_quantity"] = inventory.get("reserved_quantity")
        context["free_stock"] = inventory.get("free_stock")
        context["warehouse_summary"] = inventory.get("warehouse_summary")
        context["stock_message"] = inventory.get("stock_message")
        context["is_in_stock"] = inventory.get("is_in_stock")
        context["is_out_of_stock"] = inventory.get("is_out_of_stock")
        context["is_low_stock"] = inventory.get("is_low_stock")
        context["stock_status"] = inventory.get("stock_status")
        context["stock_text"] = inventory.get("stock_text")
        context["product_inventory"] = inventory
        return context

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        """
        Intercept POST to validate and save all unified module
        forms concurrently.
        """
        self.object = self.get_object()
        form_class = self.get_form_class()
        form = self.get_form(form_class)

        try:
            seo_instance = getattr(self.object, "seo_config", None)
        except Exception:
            seo_instance = None
        try:
            schema_instance = getattr(self.object, "schema_config", None)
        except Exception:
            schema_instance = None

        seo_form = ProductSEOForm(
            self.request.POST,
            self.request.FILES,
            instance=seo_instance,
            prefix="seo",
        )
        schema_form = ProductSchemaForm(
            self.request.POST,
            instance=schema_instance,
            prefix="schema",
        )
        publish_form = PublishingWorkflowForm(
            self.request.POST,
            instance=self.object,
            prefix="publish",
        )

        if (
            form.is_valid()
            and seo_form.is_valid()
            and schema_form.is_valid()
            and publish_form.is_valid()
        ):
            return self.form_valid(form, seo_form, schema_form, publish_form)
        else:
            return self.form_invalid(form, seo_form, schema_form, publish_form)

    def form_valid(self, form, seo_form, schema_form, publish_form):
        # Save Core Product Data
        self.object = form.save()

        # Save Publishing State Data safely over instance
        try:
            publish_data = publish_form.save(commit=False)
            publish_data.pk = self.object.pk
            publish_data.save()
        except Exception as exc:
            logger.debug("Publish form save failed: %s", exc)

        # Save SEO Profile Configuration
        try:
            seo_instance = seo_form.save(commit=False)
            seo_instance.product = self.object
            seo_instance.save()
        except Exception as exc:
            logger.debug("SEO form save failed: %s", exc)

        # Save Schema Configuration
        try:
            schema_instance = schema_form.save(commit=False)
            schema_instance.product = self.object
            schema_instance.save()
        except Exception as exc:
            logger.debug("Schema form save failed: %s", exc)

        try:
            messages.success(
                self.request,
                f"Product '{self.object.title}' and its configurations "
                "updated successfully.",
            )
        except Exception:
            pass
        return HttpResponseRedirect(self.get_success_url())

    def form_invalid(self, form, seo_form, schema_form, publish_form):
        try:
            messages.error(
                self.request,
                "There were errors updating the product. Please correct "
                "the fields below.",
            )
        except Exception:
            pass
        return self.render_to_response(
            self.get_context_data(
                form=form,
                seo_form=seo_form,
                schema_form=schema_form,
                publish_form=publish_form,
            )
        )

class ProductManageDeleteView(ProductManagementMixin, DeleteView):
    """
    Secure product deletion with confirmation and graceful
    handling of protected inventory references.

    The Inventory app uses ``on_delete=PROTECT`` for its product
    and product_variant foreign keys, so a product with active
    inventory cannot be deleted. The view surfaces a friendly
    error in that case.
    """

    model = Product
    template_name = "catalog/management/product_confirm_delete.html"
    success_url = reverse_lazy("catalog:product_manage_list")

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        product = self.object
        context["title"] = f"Delete Product: {product.title}"

        # Surface read-only inventory context so staff can see
        # what stock will be affected before confirming deletion.
        inventory = _build_inventory_context_for_product(product)
        context["inventory"] = inventory.get("inventory")
        context["inventory_summary"] = inventory.get("inventory_summary")
        context["inventory_status"] = inventory.get("inventory_status")
        context["available_quantity"] = inventory.get("available_quantity")
        context["is_in_stock"] = inventory.get("is_in_stock")
        context["product_inventory"] = inventory
        return context

    @transaction.atomic
    def form_valid(self, form):
        product = self.object
        product_title = product.title
        sku = product.sku
        username = getattr(self.request.user, "username", "unknown")
        try:
            response = super().form_valid(form)
            logger.info(
                "Product deleted by %s: %s (SKU: %s)",
                username,
                product_title,
                sku,
            )
            try:
                messages.success(
                    self.request,
                    f"Product '{product_title}' was permanently deleted.",
                )
            except Exception:
                pass
            return response
        except Exception as exc:
            # Inventory uses PROTECT on the FK; surface a friendly error
            # and let the transaction roll back cleanly.
            error_message = str(exc)
            logger.warning(
                "Product deletion failed for '%s' (SKU: %s): %s",
                product_title,
                sku,
                error_message,
            )
            try:
                messages.error(
                    self.request,
                    "This product cannot be deleted because it has active "
                    "inventory records in the Inventory module. Please "
                    "remove or transfer the inventory first.",
                )
            except Exception:
                pass
            return redirect("catalog:product_manage_list")

class ProductPublishActionView(ProductManagementMixin, View):
    """
    Standalone RPC-style view for quick status toggle operations
    (Draft, Publish, Archive) from the list view or external
    integrations.
    """

    http_method_names = ["post", "options"]

    def post(self, request, pk: int, action: str):
        product = get_object_or_404(Product, pk=pk)
        title = product.title

        # Whitelist actions to prevent unsafe state transitions
        valid_actions = {"publish", "unpublish", "archive"}
        if action not in valid_actions:
            try:
                messages.error(request, "Invalid publishing action requested.")
            except Exception:
                pass
            return self._safe_redirect(request, "catalog:product_manage_list")

        try:
            with transaction.atomic():
                if action == "publish":
                    product.status = Product.ProductStatus.PUBLISHED
                    product.is_active = True
                    if not product.published_at:
                        product.published_at = timezone.now()
                    try:
                        messages.success(
                            request,
                            f"'{title}' has been officially published.",
                        )
                    except Exception:
                        pass

                elif action == "unpublish":
                    product.status = Product.ProductStatus.DRAFT
                    product.is_active = False
                    try:
                        messages.warning(
                            request,
                            f"'{title}' was unpublished and reverted to Draft state.",
                        )
                    except Exception:
                        pass

                elif action == "archive":
                    product.status = Product.ProductStatus.ARCHIVED
                    product.is_active = False
                    try:
                        messages.info(
                            request,
                            f"'{title}' is now securely archived.",
                        )
                    except Exception:
                        pass

                product.save(
                    update_fields=["status", "is_active", "published_at", "updated_at"]
                )

        except Exception as exc:
            logger.error(
                "Publishing action failed for Product %s: %s", pk, exc
            )
            try:
                messages.error(
                    request,
                    "An error occurred while attempting to change the product status.",
                )
            except Exception:
                pass

        return self._safe_redirect(request, "catalog:product_manage_list")

    def _safe_redirect(self, request, default_url_name: str):
        """
        Redirect to a user-supplied 'next' URL if it is a safe
        relative path, otherwise fall back to a known-good URL.
        """
        next_url = request.POST.get("next", "")
        if next_url and self._is_safe_redirect(next_url):
            try:
                return redirect(next_url)
            except Exception:
                pass
        try:
            return redirect(default_url_name)
        except Exception:
            return redirect("/")

    @staticmethod
    def _is_safe_redirect(url: str) -> bool:
        """
        Prevent open-redirect vulnerabilities by ensuring the
        redirect target is a relative path on the current host.
        """
        if not url:
            return False
        if url.startswith("//"):
            return False
        if url.startswith("/") and not url.startswith("//"):
            return True
        return False