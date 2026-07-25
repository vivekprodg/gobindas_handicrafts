"""
Enterprise-grade views for the Catalog application.
Provides storefront views (Category, Product Detail, Quick View, Search, Collections, Materials, Artisans)
and staff catalog management workflows.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponseRedirect, JsonResponse
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

from apps.catalog.forms import (
    ProductFilterForm,
    ProductForm,
    ProductSchemaForm,
    ProductSEOForm,
    PublishingWorkflowForm,
)
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
from apps.catalog.selectors import (
    get_facet_counts_for_queryset,
    get_filtered_products_queryset,
    get_price_bounds_for_queryset,
)
from apps.catalog.services import (
    CATALOG_CACHE_TIMEOUT,
    BreadcrumbService,
    CategoryService,
    CollectionService,
    ProductService,
    RecentlyViewedService,
    RecommendationService,
    SearchService,
    get_catalog_settings,
)

logger = logging.getLogger(__name__)

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
        logger.warning("Inventory models could not be imported.")
        return ()

def _get_inventory_selectors():
    try:
        from apps.inventory import selectors
        return selectors
    except ImportError:
        logger.warning("Inventory selectors could not be imported.")
        return None

def _get_inventory_services():
    try:
        from apps.inventory import services
        return services
    except ImportError:
        logger.warning("Inventory services could not be imported.")
        return None

def _empty_inventory_context() -> Dict[str, Any]:
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
    if is_out_of_stock:
        return "Out of stock"
    if is_low_stock:
        try:
            return f"Only {int(free_stock)} left in stock"
        except (TypeError, ValueError, InvalidOperation):
            return "Low stock - order soon"
    return "In stock"

def _generate_warehouse_summary(warehouse_ids: set, warehouse_name: Optional[str] = None) -> str:
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

def _summarize_variant_inventory_records(inventory_records: Iterable) -> Dict[str, Any]:
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
    if product is None or getattr(product, "pk", None) is None:
        return _empty_inventory_context()

    variants_qs = product.variants.all() if use_prefetch else product.variants.filter(is_active=True)
    variants = list(variants_qs)

    if variants:
        return _summarize_variants_aggregated_inventory(variants)

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
    total_available = Decimal("0.00")
    total_reserved = Decimal("0.00")
    warehouse_ids = set()
    variant_count = 0
    in_stock_variants = 0
    low_stock_variants = 0
    out_of_stock_variants = 0

    for variant in variants:
        records_qs = variant.inventory_records.all() if hasattr(
            variant, "inventory_records"
        ) else None

        if records_qs is not None:
            records = list(records_qs)
        else:
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
        elif any(getattr(r, "is_low_stock", False) for r in records):
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
    if variant is None or getattr(variant, "pk", None) is None:
        return _empty_inventory_context()

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
    if product is None:
        return _empty_inventory_context()
    return _summarize_product_inventory(product, use_prefetch=True)

def _optimize_product_queryset_with_inventory(qs):
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
    optimized_qs = _optimize_product_queryset_with_inventory(qs)
    products = list(optimized_qs)
    return _attach_inventory_summaries(products)

def _extract_filter_parameters(request: HttpRequest) -> Dict[str, Any]:
    """Helper method to parse and clean incoming GET request filter arguments."""
    def _parse_decimal(val_str: Optional[str]) -> Optional[Decimal]:
        if not val_str:
            return None
        try:
            d = Decimal(val_str)
            return d if d >= Decimal("0.00") else None
        except (InvalidOperation, ValueError, TypeError):
            return None

    min_p = _parse_decimal(request.GET.get("min_price"))
    max_p = _parse_decimal(request.GET.get("price_max") or request.GET.get("max_price"))
    in_stock = request.GET.get("in_stock_only") == "true" or request.GET.get("in_stock") == "1"
    min_rating = request.GET.get("min_rating") or request.GET.get("rating")
    on_sale = request.GET.get("on_sale") == "true" or request.GET.get("on_sale") == "1"
    
    min_discount = request.GET.get("min_discount_pct")
    min_discount_val = None
    if min_discount:
        try:
            min_discount_val = int(min_discount)
        except (ValueError, TypeError):
            pass

    return {
        "sort_by": request.GET.get("sort", "featured") or "featured",
        "min_price": min_p,
        "max_price": max_p,
        "in_stock_only": in_stock,
        "min_rating": min_rating,
        "selected_materials": request.GET.getlist("material"),
        "selected_artisans": request.GET.getlist("artisan"),
        "selected_origins": request.GET.getlist("origin"),
        "selected_hues": request.GET.getlist("hue"),
        "selected_ethical_standards": request.GET.getlist("ethical"),
        "selected_tags": request.GET.getlist("tag"),
        "selected_collections": request.GET.getlist("collection"),
        "on_sale_only": on_sale,
        "min_discount_pct": min_discount_val,
        "search_query": request.GET.get("q") or request.GET.get("search"),
    }

def _build_active_filter_chips(params: Dict[str, Any]) -> List[Dict[str, str]]:
    """Builds a list of active filter dictionary badges for template chip rendering."""
    chips: List[Dict[str, str]] = []
    
    if params.get("min_price") is not None or params.get("max_price") is not None:
        label = f"Price: NPR {params.get('min_price', 0) or 0:,.0f} - {params.get('max_price', 'Max')}"
        chips.append({"param": "price", "key": "price", "label": label})
        
    if params.get("in_stock_only"):
        chips.append({"param": "in_stock_only", "key": "in_stock_only", "label": "In-Stock Only"})

    if params.get("min_rating"):
        chips.append({"param": "min_rating", "key": "min_rating", "label": f"{params['min_rating']}★ & above"})

    for m in params.get("selected_materials", []):
        chips.append({"param": "material", "key": m, "label": f"Material: {m}"})

    for a in params.get("selected_artisans", []):
        chips.append({"param": "artisan", "key": a, "label": f"Craftsman: {a}"})

    for o in params.get("selected_origins", []):
        chips.append({"param": "origin", "key": o, "label": f"Origin: {o}"})

    for h in params.get("selected_hues", []):
        chips.append({"param": "hue", "key": h, "label": f"Color: {h}"})

    for e in params.get("selected_ethical_standards", []):
        chips.append({"param": "ethical", "key": e, "label": f"Standard: {e}"})

    for t in params.get("selected_tags", []):
        chips.append({"param": "tag", "key": t, "label": f"Tag: #{t}"})

    for c in params.get("selected_collections", []):
        chips.append({"param": "collection", "key": c, "label": f"Collection: {c}"})

    if params.get("on_sale_only"):
        chips.append({"param": "on_sale", "key": "on_sale", "label": "On Sale"})

    if params.get("min_discount_pct"):
        chips.append({"param": "min_discount_pct", "key": "min_discount_pct", "label": f"{params['min_discount_pct']}% OFF or more"})

    return chips

class ProductManagementMixin(UserPassesTestMixin):
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

class CategoryListingView(TemplateView):
    template_name = "catalog/product-list.html"
    slug: Optional[str] = None

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        slug = self.slug or self.kwargs.get("slug", "")

        category = cache.get_or_set(
            f"catalog:cat:slug:{slug}",
            lambda: CategoryService.get_category_by_slug(slug),
            CATALOG_CACHE_TIMEOUT,
        )

        if not category:
            raise Http404("Category not found")

        params = _extract_filter_parameters(self.request)

        # Build base category queryset
        base_qs = Product.objects.published().in_category(category)

        # Apply multi-criteria filtering selector
        products_qs = get_filtered_products_queryset(
            qs=base_qs,
            min_price=params["min_price"],
            max_price=params["max_price"],
            in_stock_only=params["in_stock_only"],
            min_rating=params["min_rating"],
            selected_materials=params["selected_materials"],
            selected_artisans=params["selected_artisans"],
            selected_origins=params["selected_origins"],
            selected_hues=params["selected_hues"],
            selected_ethical_standards=params["selected_ethical_standards"],
            selected_tags=params["selected_tags"],
            selected_collections=params["selected_collections"],
            on_sale_only=params["on_sale_only"],
            min_discount_pct=params["min_discount_pct"],
            sort_by=params["sort_by"],
        )

        products_qs = _optimize_product_queryset_with_inventory(products_qs)

        catalog_settings = cache.get_or_set(
            "catalog:settings",
            get_catalog_settings,
            CATALOG_CACHE_TIMEOUT,
        )
        page_number = self.request.GET.get("page", 1)

        paginated_products = SearchService.paginate_products(
            products_qs,
            page_number,
            catalog_settings.default_items_per_page,
        )

        page_products = list(getattr(paginated_products, "object_list", paginated_products))
        _attach_inventory_summaries(page_products)

        current_selections = {
            "materials": params["selected_materials"],
            "artisans": params["selected_artisans"],
            "origins": params["selected_origins"],
            "hues": params["selected_hues"],
            "ethical": params["selected_ethical_standards"],
            "tags": params["selected_tags"],
            "collections": params["selected_collections"],
        }
        
        facet_metadata = get_facet_counts_for_queryset(
            qs=base_qs,
            category=category,
            current_selections=current_selections,
        )

        min_bound = facet_metadata["price_bounds"]["min"] or catalog_settings.price_filter_min
        max_bound = facet_metadata["price_bounds"]["max"] or catalog_settings.price_filter_max
        current_val = int(params["max_price"]) if params["max_price"] is not None else max_bound

        breadcrumbs = BreadcrumbService.build_for_category(category)
        active_chips = _build_active_filter_chips(params)

        context.update(
            {
                "category": category,
                "title": category.seo_title or category.name,
                "description": category.seo_description or category.description,
                "products": paginated_products,
                "total_products": products_qs.count(),
                "breadcrumbs": breadcrumbs,
                "facets": facet_metadata,
                "active_filter_chips": active_chips,
                "filter_categories": facet_metadata["categories"],
                "filter_materials": facet_metadata["materials"],
                "filter_craftsmen": facet_metadata["artisans"],
                "filter_provenance": facet_metadata["origins"],
                "filter_hues": facet_metadata["hues"],
                "filter_ethical": facet_metadata["ethical"],
                "filter_tags": facet_metadata["tags"],
                "filter_collections": facet_metadata["collections"],
                "filter_price": {
                    "min": min_bound,
                    "max": max_bound,
                    "current": current_val,
                    "min_formatted": f"{min_bound:,.0f}",
                    "current_formatted": f"{current_val:,.0f}",
                },
                "current_sort": params["sort_by"],
            }
        )
        return context

class ArtisansListView(ListView):
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
                    "We partner directly with master craftsmen, ensuring fair wages, "
                    "safe workshops, and the survival of ancestral lineages."
                ),
            }
        )
        return context

class ArtisanDetailView(DetailView):
    model = Artisan
    template_name = "catalog/artisan-detail.html"
    context_object_name = "artisan"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return Artisan.objects.filter(is_active=True)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        artisan = self.get_object()

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
                    or f"Explore the exclusive collection and craft lineage of Master {artisan.name}."
                ),
                "products": products,
            }
        )
        return context

class ProductDetailView(DetailView):
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
        qs = super().get_queryset()
        return _optimize_product_queryset_with_inventory(qs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        product = self.object

        inventory = _build_inventory_context_for_product(product)
        breadcrumbs = BreadcrumbService.build_for_product(product)

        try:
            related_products_qs = RecommendationService.get_related_products(
                product, limit=4
            )
            related_products = _process_products_with_inventory(related_products_qs)
        except Exception as exc:
            logger.debug("Related products fetch failed: %s", exc)
            related_products = []

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

        try:
            product.increment_view_count(commit=True)
        except Exception as exc:
            logger.debug("View count increment failed: %s", exc)

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

class ProductQuickViewView(DetailView):
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
    model = Product
    template_name = "catalog/product-search.html"
    context_object_name = "products"
    paginate_by = 12

    def get_queryset(self):
        params = _extract_filter_parameters(self.request)
        return get_filtered_products_queryset(
            search_query=params["search_query"],
            min_price=params["min_price"],
            max_price=params["max_price"],
            in_stock_only=params["in_stock_only"],
            min_rating=params["min_rating"],
            selected_materials=params["selected_materials"],
            selected_artisans=params["selected_artisans"],
            selected_origins=params["selected_origins"],
            selected_hues=params["selected_hues"],
            selected_ethical_standards=params["selected_ethical_standards"],
            selected_tags=params["selected_tags"],
            selected_collections=params["selected_collections"],
            on_sale_only=params["on_sale_only"],
            min_discount_pct=params["min_discount_pct"],
            sort_by=params["sort_by"],
        )

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        params = _extract_filter_parameters(self.request)
        query = params["search_query"] or ""

        base_search_qs = Product.objects.published().search(query) if query else Product.objects.published()
        facet_metadata = get_facet_counts_for_queryset(qs=base_search_qs)

        products_qs = self.get_queryset()
        total_count = products_qs.count()

        page_obj = context.get("page_obj")
        if page_obj is not None:
            page_products = list(page_obj.object_list)
            _attach_inventory_summaries(page_products)
            page_obj.object_list = page_products

        active_chips = _build_active_filter_chips(params)

        context.update(
            {
                "search_query": query,
                "title": f"Search: {query}" if query else "Search Catalog Masterpieces",
                "description": (
                    f"Search results for '{query}' across artisans, materials, and categories." if query
                    else "Browse our handcrafted products."
                ),
                "total_products": total_count,
                "facets": facet_metadata,
                "active_filter_chips": active_chips,
                "current_sort": params["sort_by"],
            }
        )
        return context

class CollectionView(DetailView):
    model = ProductCollection
    template_name = "catalog/collection-detail.html"
    context_object_name = "collection"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return ProductCollection.objects.filter(is_active=True)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        collection = self.object

        params = _extract_filter_parameters(self.request)
        base_qs = collection.products.published()

        products_qs = get_filtered_products_queryset(
            qs=base_qs,
            min_price=params["min_price"],
            max_price=params["max_price"],
            in_stock_only=params["in_stock_only"],
            min_rating=params["min_rating"],
            selected_materials=params["selected_materials"],
            selected_artisans=params["selected_artisans"],
            selected_origins=params["selected_origins"],
            selected_hues=params["selected_hues"],
            selected_ethical_standards=params["selected_ethical_standards"],
            selected_tags=params["selected_tags"],
            on_sale_only=params["on_sale_only"],
            min_discount_pct=params["min_discount_pct"],
            sort_by=params["sort_by"],
        )

        products = _process_products_with_inventory(products_qs)
        facet_metadata = get_facet_counts_for_queryset(qs=base_qs)

        context.update(
            {
                "products": products,
                "title": collection.name,
                "description": collection.description,
                "facets": facet_metadata,
                "active_filter_chips": _build_active_filter_chips(params),
                "current_sort": params["sort_by"],
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
    model = Material
    template_name = "catalog/material-detail.html"
    context_object_name = "material"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return Material.objects.all()

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        material = self.object

        params = _extract_filter_parameters(self.request)
        base_qs = Product.objects.published().filter(material=material)

        products_qs = get_filtered_products_queryset(
            qs=base_qs,
            min_price=params["min_price"],
            max_price=params["max_price"],
            in_stock_only=params["in_stock_only"],
            min_rating=params["min_rating"],
            selected_artisans=params["selected_artisans"],
            selected_origins=params["selected_origins"],
            selected_hues=params["selected_hues"],
            selected_ethical_standards=params["selected_ethical_standards"],
            selected_tags=params["selected_tags"],
            selected_collections=params["selected_collections"],
            on_sale_only=params["on_sale_only"],
            min_discount_pct=params["min_discount_pct"],
            sort_by=params["sort_by"],
        )

        products = _process_products_with_inventory(products_qs)
        facet_metadata = get_facet_counts_for_queryset(qs=base_qs)

        context.update(
            {
                "products": products,
                "title": material.name,
                "description": f"Masterpieces crafted from {material.name}.",
                "facets": facet_metadata,
                "active_filter_chips": _build_active_filter_chips(params),
                "current_sort": params["sort_by"],
                "breadcrumbs": [
                    ("Materials", ""),
                    (material.name, ""),
                ],
            }
        )
        return context

class ProductManageListView(ProductManagementMixin, ListView):
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

        q = (self.request.GET.get("q", "") or "").strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(sku__icontains=q)
                | Q(barcode__icontains=q)
            )

        status = self.request.GET.get("status", "")
        valid_statuses = {choice[0] for choice in Product.ProductStatus.choices}
        if status in valid_statuses:
            qs = qs.filter(status=status)

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
        try:
            if not form.cleaned_data.get("status"):
                form.instance.status = Product.ProductStatus.DRAFT
        except Exception:
            form.instance.status = Product.ProductStatus.DRAFT
        return super().form_valid(form)

class ProductManageUpdateView(ProductManagementMixin, UpdateView):
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

        inventory = _build_inventory_context_for_product(product)

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
        self.object = form.save()

        try:
            publish_data = publish_form.save(commit=False)
            publish_data.pk = self.object.pk
            publish_data.save()
        except Exception as exc:
            logger.debug("Publish form save failed: %s", exc)

        try:
            seo_instance = seo_form.save(commit=False)
            seo_instance.product = self.object
            seo_instance.save()
        except Exception as exc:
            logger.debug("SEO form save failed: %s", exc)

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
                "There were errors updating the product. Please correct the fields below.",
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
    model = Product
    template_name = "catalog/management/product_confirm_delete.html"
    success_url = reverse_lazy("catalog:product_manage_list")

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        product = self.object
        context["title"] = f"Delete Product: {product.title}"

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
    http_method_names = ["post", "options"]

    def post(self, request, pk: int, action: str):
        product = get_object_or_404(Product, pk=pk)
        title = product.title

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
            logger.error("Publishing action failed for Product %s: %s", pk, exc)
            try:
                messages.error(
                    request,
                    "An error occurred while attempting to change the product status.",
                )
            except Exception:
                pass

        return self._safe_redirect(request, "catalog:product_manage_list")

    def _safe_redirect(self, request, default_url_name: str):
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
        if not url:
            return False
        if url.startswith("//"):
            return False
        if url.startswith("/") and not url.startswith("//"):
            return True
        return False

__all__ = [
    "CategoryListingView",
    "ArtisansListView",
    "ArtisanDetailView",
    "ProductDetailView",
    "ProductQuickViewView",
    "ProductSearchView",
    "CollectionView",
    "MaterialView",
    "ProductManageListView",
    "ProductManageCreateView",
    "ProductManageUpdateView",
    "ProductManageDeleteView",
    "ProductPublishActionView",
]