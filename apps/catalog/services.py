from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.cache import cache
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db import models
from django.db.models import QuerySet, Min, Max, Q, Prefetch, Count

from .models import (
    CatalogSettings,
    Category,
    Product,
    Artisan,
    Material,
    Hue,
    EthicalStandard,
    ProductCollection,
    ProductSpecification,
    ProductFAQ,
    ProductVideo,
    RecentlyViewedProduct,
    ProductVariant,
)

# ==============================================================================
# CACHING & SETTINGS CONFIGURATIONS
# ==============================================================================
CATALOG_CACHE_VERSION = 1
CATALOG_CACHE_TIMEOUT = 60 * 30  # 30 minutes
CATALOG_LIST_CACHE_PREFIX = "catalog:list:"

def invalidate_catalog_cache() -> None:
    """
    Clears all catalog-related caches.
    This should be called from Signals on Product/Category/Artisan save/delete.
    """
    cache.clear()

def get_catalog_settings() -> CatalogSettings:
    """
    Retrieves the singleton CatalogSettings instance.
    Kept for backward compatibility with views and external components.
    """
    return ProductService.get_catalog_settings()

def get_category_by_slug(slug: str) -> Category | None:
    """
    Retrieves an active Category instance by its slug.
    Kept for backward compatibility with views.
    """
    return CategoryService.get_category_by_slug(slug)

def get_active_categories_hierarchy() -> list[dict[str, Any]]:
    """
    Compiles category hierarchy tree dynamically for sidebar filtering.
    Kept for backward compatibility.
    """
    return CategoryService.get_active_categories_hierarchy()

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
    Kept for backward compatibility.
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

def get_sidebar_filter_metadata(category: Category, current_selections: dict[str, Any]) -> dict[str, Any]:
    """
    Compiles distinct filter values from all active products in this category tree
    to populate sidebar options dynamically.
    Kept for backward compatibility.
    """
    return SearchService.get_sidebar_filter_metadata(category, current_selections)


def paginate_products(products_qs: QuerySet[Product], page_number: str | int, items_per_page: int) -> Any:
    """
    Paginates the given product queryset.
    Kept for backward compatibility.
    """
    return SearchService.paginate_products(products_qs, page_number, items_per_page)

# ==============================================================================
# ENTERPRISE CATALOG SERVICE CLASSES
# ==============================================================================
class ProductService:
    """
    Dedicated Service class managing core Product queries, loading metadata,
    and handling business logical requirements for single and bulk views.
    """

    @staticmethod
    def get_catalog_settings() -> CatalogSettings:
        """
        Retrieves the singleton CatalogSettings instance, creating a default one if none exists.
        """
        settings = CatalogSettings.objects.first()
        if not settings:
            settings = CatalogSettings.objects.create(
                default_items_per_page=9,
                price_filter_min=500,
                price_filter_max=100000,
                show_stock_warning_threshold=5,
            )
        return settings

    @staticmethod
    def get_optimized_product_queryset() -> QuerySet[Product]:
        """
        Builds a standard optimized QuerySet utilizing select_related and prefetch_related
        to eliminate N+1 performance bottlenecks on complex templates.
        """
        return Product.objects.filter(is_active=True).select_related(
            "category",
            "category__parent",
            "artisan",
            "material",
            "hue",
            "seo_config",
            "schema_config",
        ).prefetch_related(
            "ethical_standards",
            "variants",
            "gallery_images",
            "tags",
            "in_collections",
            "highlights",
            "trust_badges",
            "labels",
            "icons",
        )

    @classmethod
    def get_product_by_id(cls, product_id: int) -> Product | None:
        """
        Retrieves an optimized product by its primary key.
        """
        try:
            return cls.get_optimized_product_queryset().get(id=product_id)
        except Product.DoesNotExist:
            return None

    @classmethod
    def get_product_by_slug(cls, slug: str) -> Product | None:
        """
        Retrieves an optimized product by its unique slug identifier.
        """
        try:
            return cls.get_optimized_product_queryset().get(slug=slug)
        except Product.DoesNotExist:
            return None

    @staticmethod
    def is_product_available(product: Product) -> bool:
        """
        Evaluates the current operational availability state of a given Product masterpiece.
        """
        return product.stock_status != Product.StockChoices.OUT_OF_STOCK

    @staticmethod
    def get_product_specifications(product: Product) -> QuerySet[ProductSpecification]:
        """
        Returns display-ordered specifications for a product.
        """
        return product.specifications.all().order_by("display_order", "label")

    @staticmethod
    def get_product_faqs(product: Product) -> QuerySet[ProductFAQ]:
        """
        Returns active display-ordered FAQs linked to a product.
        """
        return product.faqs.filter(is_active=True).order_by("display_order", "id")

    @staticmethod
    def get_product_videos(product: Product) -> QuerySet[ProductVideo]:
        """
        Returns display-ordered multimedia/videos linked to a product.
        """
        return product.videos.all().order_by("display_order", "id")

    @staticmethod
    def get_product_variants(product: Product) -> QuerySet[ProductVariant]:
        """
        Returns active, sorted variant inventory options linked to a product.
        """
        return product.variants.filter(is_active=True).order_by("sort_order", "id")

class CategoryService:
    """
    Dedicated Service class handling Category tree traversal, breadcrumb mapping,
    and optimal database indexing structures.
    """

    @staticmethod
    def get_category_by_slug(slug: str) -> Category | None:
        """
        Retrieves an active category from the database using its slug.
        """
        try:
            return Category.objects.filter(slug=slug, is_active=True).select_related("parent").first()
        except Category.DoesNotExist:
            return None

    @staticmethod
    def get_active_categories_hierarchy() -> list[dict[str, Any]]:
        """
        Retrieves and structures the dynamic Category sidebar hierarchy list.
        Uses Django Prefetch to perform minimal SQL hits.
        """
        top_categories = Category.objects.filter(parent=None, is_active=True).prefetch_related(
            Prefetch("subcategories", queryset=Category.objects.filter(is_active=True))
        )
        hierarchy = []
        for cat in top_categories:
            subcats = [{"name": sub.name, "slug": sub.slug} for sub in cat.subcategories.all()]
            hierarchy.append(
                {
                    "id": cat.id,
                    "name": cat.name,
                    "slug": cat.slug,
                    "subcategories": subcats,
                }
            )
        return hierarchy

    @staticmethod
    def get_category_product_counts() -> dict[int, int]:
        """
        Retrieves a aggregated mapping of Category IDs to active product counts.
        """
        counts = Category.objects.filter(is_active=True).annotate(
            active_product_count=Count(
                "products",
                filter=Q(
                    products__is_active=True,
                    products__status=Product.ProductStatus.PUBLISHED,
                ),
            )
        )
        return {c.id: c.active_product_count for c in counts}

class CollectionService:
    """
    Dedicated Service class managing curated static, seasonal, or custom product collections.
    """

    @staticmethod
    def get_collection_by_slug(slug: str) -> ProductCollection | None:
        """
        Retrieves a collection object using its slug with optimized product fetches.
        """
        try:
            return ProductCollection.objects.filter(slug=slug, is_active=True).prefetch_related(
                Prefetch(
                    "products",
                    queryset=Product.objects.filter(
                        is_active=True, status=Product.ProductStatus.PUBLISHED
                    ),
                )
            ).first()
        except ProductCollection.DoesNotExist:
            return None

    @staticmethod
    def get_active_collections() -> QuerySet[ProductCollection]:
        """
        Returns all active collections ordered by their display priority.
        """
        return ProductCollection.objects.filter(is_active=True).order_by("sort_order", "name")

    @staticmethod
    def get_products_in_collection(slug: str, limit: int | None = None) -> QuerySet[Product]:
        """
        Retrieves optimized products mapping to a designated product collection.
        """
        collection = ProductCollection.objects.filter(slug=slug, is_active=True).first()
        if not collection:
            return Product.objects.none()

        qs = collection.products.filter(
            is_active=True, status=Product.ProductStatus.PUBLISHED
        ).select_related("category", "artisan", "material", "hue").prefetch_related("ethical_standards")

        if limit:
            return qs[:limit]
        return qs

class RecommendationService:
    """
    Flexible Recommendation engine resolving similar craft lines, complementary artisan pieces,
    upsell/cross-sell configurations, or contextual fallbacks.
    """

    @staticmethod
    def get_related_products(product: Product, limit: int = 4) -> QuerySet[Product]:
        """
        Fetches explicitly marked related products. Fallbacks gracefully onto the
        same Artisan or Category to safeguard design representation.
        """
        related = product.related_products.filter(
            is_active=True, status=Product.ProductStatus.PUBLISHED
        ).select_related("category", "artisan", "material", "hue").prefetch_related("ethical_standards")

        if related.exists():
            return related[:limit]

        # Fallback 1: Same Artisan Lineage
        if product.artisan:
            artisan_products = Product.objects.filter(
                artisan=product.artisan,
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            ).exclude(id=product.id).select_related(
                "category", "artisan", "material", "hue"
            ).prefetch_related("ethical_standards")[:limit]

            if artisan_products.count() >= limit:
                return artisan_products

        # Fallback 2: Same Category
        if product.category:
            return Product.objects.filter(
                category=product.category,
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            ).exclude(id=product.id).select_related(
                "category", "artisan", "material", "hue"
            ).prefetch_related("ethical_standards").order_by(
                "-wishlist_count", "-view_count"
            )[:limit]

        return Product.objects.none()

    @staticmethod
    def get_upsell_products(product: Product, limit: int = 4) -> QuerySet[Product]:
        """
        Retrieves upsell recommendations. Recommends higher-priced active items in same category.
        """
        upsell = product.upsell_products.filter(
            is_active=True, status=Product.ProductStatus.PUBLISHED
        ).select_related("category", "artisan", "material", "hue").prefetch_related("ethical_standards")

        if upsell.exists():
            return upsell[:limit]

        if product.category and product.price:
            return Product.objects.filter(
                category=product.category,
                price__gt=product.price,
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            ).exclude(id=product.id).select_related(
                "category", "artisan", "material", "hue"
            ).prefetch_related("ethical_standards").order_by("price")[:limit]

        return Product.objects.none()

    @staticmethod
    def get_cross_sell_products(product: Product, limit: int = 4) -> QuerySet[Product]:
        """
        Retrieves cross-sell recommendations. Recommends matching material handicrafts.
        """
        cross_sell = product.cross_sell_products.filter(
            is_active=True, status=Product.ProductStatus.PUBLISHED
        ).select_related("category", "artisan", "material", "hue").prefetch_related("ethical_standards")

        if cross_sell.exists():
            return cross_sell[:limit]

        if product.material:
            return Product.objects.filter(
                material=product.material,
                is_active=True,
                status=Product.ProductStatus.PUBLISHED,
            ).exclude(id=product.id).select_related(
                "category", "artisan", "material", "hue"
            ).prefetch_related("ethical_standards").order_by("position")[:limit]

        return Product.objects.none()

class RecentlyViewedService:
    """
    Maintains and queries the contextual browsing history for authenticated users or anonymous sessions.
    """

    @staticmethod
    def add_to_recently_viewed(
        product: Product, user: Any = None, session_key: str | None = None
    ) -> RecentlyViewedProduct | None:
        """
        Adds a product entry to historical browser queue. Prevents duplicate values
        and trims historical data to prevent unnecessary database bloat.
        """
        if not user and not session_key:
            return None

        user_id = user.id if (user and user.is_authenticated) else None

        lookup_kwargs: dict[str, Any] = {"product": product}
        if user_id:
            lookup_kwargs["user_id"] = user_id
        else:
            lookup_kwargs["session_key"] = session_key

        # update_or_create forces watched timestamp to auto-increment on save
        rv, _ = RecentlyViewedProduct.objects.update_or_create(
            **lookup_kwargs, defaults={}
        )

        # Retain browsing queue size limits (max 20 records)
        trim_qs = (
            RecentlyViewedProduct.objects.filter(user_id=user_id)
            if user_id
            else RecentlyViewedProduct.objects.filter(session_key=session_key)
        )
        excess_ids = list(trim_qs.order_by("-viewed_at").values_list("id", flat=True)[20:])
        if excess_ids:
            RecentlyViewedProduct.objects.filter(id__in=excess_ids).delete()

        return rv

    @staticmethod
    def get_recently_viewed(
        user: Any = None, session_key: str | None = None, limit: int = 6
    ) -> QuerySet[Product]:
        """
        Retrieves the ordered browser history items mapped to user/session parameters.
        """
        if not user and not session_key:
            return Product.objects.none()

        user_id = user.id if (user and user.is_authenticated) else None

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

        product_ids = list(qs.order_by("-viewed_at").values_list("product_id", flat=True)[:limit])
        if not product_ids:
            return Product.objects.none()

        # Preserves initial dynamic list position ranking inside Django SQL compilation
        preserved_order = models.Case(
            *[models.When(pk=pk, then=pos) for pos, pk in enumerate(product_ids)]
        )
        return Product.objects.filter(id__in=product_ids, is_active=True).order_by(preserved_order)

class BreadcrumbService:
    """
    Builds consistent structural link matrices suitable for search engine bots
    and nested navigation links.
    """

    @staticmethod
    def build_for_home() -> list[dict[str, str]]:
        return [{"label": "Home", "url": "/"}]

    @classmethod
    def build_for_category(cls, category: Category) -> list[dict[str, str]]:
        breadcrumbs = cls.build_for_home()
        breadcrumbs.append({"label": "Handicrafts", "url": "#"})
        if category.parent:
            breadcrumbs.append({"label": category.parent.name, "url": f"/category/{category.parent.slug}/"})
        breadcrumbs.append({"label": category.name, "url": f"/category/{category.slug}/"})
        return breadcrumbs

    @classmethod
    def build_for_product(cls, product: Product) -> list[dict[str, str]]:
        if product.category:
            breadcrumbs = cls.build_for_category(product.category)
        else:
            breadcrumbs = cls.build_for_home()
            breadcrumbs.append({"label": "Handicrafts", "url": "#"})
        breadcrumbs.append({"label": product.title, "url": "#"})
        return breadcrumbs

    @classmethod
    def build_for_collection(cls, collection: ProductCollection) -> list[dict[str, str]]:
        breadcrumbs = cls.build_for_home()
        breadcrumbs.append({"label": "Collections", "url": "#"})
        breadcrumbs.append({"label": collection.name, "url": f"/collection/{collection.slug}/"})
        return breadcrumbs

class SearchService:
    """
    Aggregated search, dynamic facets, multi-attribute indexing, sorting rules,
    and fallback navigation pipelines.
    """

    @staticmethod
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
            sub_ids = list(category.subcategories.filter(is_active=True).values_list("id", flat=True))
            category_ids.extend(sub_ids)

        # 2. Base Active QuerySet
        qs = Product.objects.filter(category_id__in=category_ids, is_active=True).select_related(
            "category", "artisan", "material", "hue"
        ).prefetch_related("ethical_standards")

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
        else:  # Default is "featured" (position then created_at)
            qs = qs.order_by("position", "-created_at")

        return qs.distinct()

    @staticmethod
    def get_sidebar_filter_metadata(category: Category, current_selections: dict[str, Any]) -> dict[str, Any]:
        """
        Compiles distinct filter values from all active products in this category tree
        to populate sidebar options dynamically.
        """
        category_ids = [category.id]
        if not category.parent:
            category_ids.extend(list(category.subcategories.filter(is_active=True).values_list("id", flat=True)))

        base_qs = Product.objects.filter(category_id__in=category_ids, is_active=True)

        # Fetch distinct criteria from matching products
        materials = list(
            Material.objects.filter(products__in=base_qs).distinct().values_list("name", flat=True)
        )
        artisans = list(
            Artisan.objects.filter(products__in=base_qs, is_active=True).distinct().values("name", "slug")
        )
        origins = list(
            base_qs.exclude(artisan__region="").values_list("artisan__region", flat=True).distinct()
        )
        hues = list(
            Hue.objects.filter(products__in=base_qs).distinct().values("name", "color_code")
        )
        ethical_standards = list(
            EthicalStandard.objects.filter(products__in=base_qs).distinct().values_list("name", flat=True)
        )

        # Aggregate min/max prices
        price_stats = base_qs.aggregate(min_p=Min("price"), max_p=Max("price"))
        min_price_found = int(price_stats["min_p"] or 0)
        max_price_found = int(price_stats["max_p"] or 0)

        # Resolve active/checked statuses
        return {
            "categories": CategoryService.get_active_categories_hierarchy(),
            "materials": [
                {"name": mat, "checked": mat in current_selections.get("materials", [])}
                for mat in materials
            ],
            "artisans": [
                {
                    "name": art["name"],
                    "slug": art["slug"],
                    "checked": art["slug"] in current_selections.get("artisans", []),
                }
                for art in artisans
            ],
            "origins": [
                {"name": orig, "checked": orig in current_selections.get("origins", [])}
                for orig in origins
            ],
            "hues": [
                {
                    "name": hue["name"],
                    "color": hue["color_code"],
                    "checked": hue["name"] in current_selections.get("hues", []),
                }
                for hue in hues
            ],
            "ethical": [
                {"name": std, "checked": std in current_selections.get("ethical", [])}
                for std in ethical_standards
            ],
            "price_bounds": {
                "min": min_price_found,
                "max": max_price_found,
            },
        }

    @staticmethod
    def paginate_products(products_qs: QuerySet[Product], page_number: str | int, items_per_page: int) -> Any:
        """
        Applies pagination structures with fallback safety mechanisms.
        """
        paginator = Paginator(products_qs, items_per_page)
        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)
        return page_obj