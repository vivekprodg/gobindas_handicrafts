from __future__ import annotations

from decimal import Decimal
import logging
from typing import Any

from django.db import transaction
from django.db.models import Q, Count, Prefetch
from django.views.generic import TemplateView, DetailView, ListView, CreateView, UpdateView, DeleteView, View
from django.shortcuts import get_object_or_404, redirect
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.cache import cache

# Core catalog models
from apps.catalog.models import (
    Category, 
    Product, 
    Artisan, 
    Material, 
    Hue, 
    EthicalStandard,
    ProductVariant,
    ProductImage,
    ProductGalleryImage,
    ProductTag,
    ProductCollection,
    ProductSEO,
    ProductSchema
)

# Services
from apps.catalog.services import (
    ProductService,
    CategoryService,
    CollectionService,
    RecommendationService,
    RecentlyViewedService,
    BreadcrumbService,
    SearchService,
    get_catalog_settings,
    CATALOG_CACHE_TIMEOUT,
)

# Forms for Management Module
from apps.catalog.forms import (
    ProductForm,
    ProductSEOForm,
    ProductSchemaForm,
    PublishingWorkflowForm
)

logger = logging.getLogger(__name__)


# ==============================================================================
# PUBLIC-FACING CATALOG VIEWS (Service-Layer Refactored)
# ==============================================================================

class CategoryListingView(TemplateView):
    """
    Public product listing and merchandising discovery view for categories.
    Delegates dynamic faceted filtering, sorting, pagination, and structural
    breadcrumbs mapping directly to modular services.
    """
    template_name = "catalog/product-list.html"
    slug = None  # Set via URL conf for legacy routing

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = self.slug or self.kwargs.get("slug", "")
        
        # Safe cache extraction of Category details
        category = cache.get_or_set(
            f"catalog:cat:slug:{slug}",
            lambda: CategoryService.get_category_by_slug(slug),
            CATALOG_CACHE_TIMEOUT
        )
        
        if not category:
            raise Http404("Category not found")

        # Extract parameters for faceted searches
        sort_by = self.request.GET.get("sort", "featured")
        
        price_max_raw = self.request.GET.get("price_max")
        price_max = None
        if price_max_raw:
            try:
                price_max = Decimal(price_max_raw)
            except (ValueError, ArithmeticError):
                pass
                
        selected_materials = self.request.GET.getlist("material")
        selected_artisans = self.request.GET.getlist("artisan")
        selected_origins = self.request.GET.getlist("origin")
        selected_hues = self.request.GET.getlist("hue")
        selected_ethical = self.request.GET.getlist("ethical")

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
            selected_ethical_standards=selected_ethical
        )

        # Pagination resolution
        catalog_settings = cache.get_or_set(
            "catalog:settings",
            ProductService.get_catalog_settings,
            CATALOG_CACHE_TIMEOUT
        )
        page_number = self.request.GET.get("page", 1)
        paginated_products = SearchService.paginate_products(
            products_qs,
            page_number,
            catalog_settings.default_items_per_page
        )

        # Facets and UI criteria details
        current_selections = {
            "materials": selected_materials,
            "artisans": selected_artisans,
            "origins": selected_origins,
            "hues": selected_hues,
            "ethical": selected_ethical
        }
        sidebar_filters = SearchService.get_sidebar_filter_metadata(category, current_selections)

        min_bound = sidebar_filters["price_bounds"]["min"] or catalog_settings.price_filter_min
        max_bound = sidebar_filters["price_bounds"]["max"] or catalog_settings.price_filter_max
        current_val = int(price_max) if price_max is not None else max_bound

        # Breadcrumbs built by dedicated builder service
        breadcrumbs = BreadcrumbService.build_for_category(category)

        context.update({
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
                "current_formatted": f"{current_val:,.0f}"
            },
            "current_sort": sort_by,
        })
        return context


class ArtisansListView(ListView):
    """
    Public listing of all active Master Craftsmen.
    """
    model = Artisan
    template_name = "catalog/artisans-list.html"
    context_object_name = "artisans"

    def get_queryset(self):
        return Artisan.objects.filter(is_active=True).order_by("position", "name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "title": "Meet the Makers",
            "description": "We partner directly with over 150 master craftsmen, ensuring fair wages, safe workshops, and the survival of ancestral lineages."
        })
        return context


class ArtisanDetailView(DetailView):
    """
    Public profile detail view for a single Artisan, including their masterpieces.
    """
    model = Artisan
    template_name = "catalog/artisan-detail.html"
    context_object_name = "artisan"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return Artisan.objects.filter(is_active=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        artisan = self.get_object()
        
        # Retrieve optimized artisan masterpieces
        products = Product.objects.filter(artisan=artisan, is_active=True).select_related(
            'category', 'material', 'hue'
        ).prefetch_related('ethical_standards')
        
        context.update({
            "title": f"Master {artisan.name}",
            "description": artisan.bio or f"Explore the exclusive collection and craft lineage of Master {artisan.name}.",
            "products": products
        })
        return context


class ProductDetailView(DetailView):
    """
    Public product detail view. Enriches rendering with structured microdata schemas, SEO configuration profiles,
    and records client-side browsing context safely using the Recently Viewed Service layer.
    """
    model = Product
    template_name = "catalog/product-detail.html"
    context_object_name = "product"
    slug_url_kwarg = "slug"

    def get_object(self, queryset=None):
        slug = self.kwargs.get(self.slug_url_kwarg)
        product = ProductService.get_product_by_slug(slug)
        if not product:
            raise Http404("Product not found")
        return product

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        product = self.object
        
        # Build breadcrumbs using the breadcrumb builder service
        breadcrumbs = BreadcrumbService.build_for_product(product)

        # Pull matching product recommendations safely through the recommendation service
        related_products = RecommendationService.get_related_products(product, limit=4)

        # Get variants
        variants = ProductService.get_product_variants(product)

        # Resolve SEO fields cleanly
        seo_title = product.title
        seo_desc = product.short_description
        if hasattr(product, 'seo_config') and product.seo_config:
            seo_title = product.seo_config.meta_title or product.seo_title or product.title
            seo_desc = product.seo_config.meta_description or product.seo_description or product.short_description
        else:
            seo_title = product.seo_title or product.title
            seo_desc = product.seo_description or product.short_description

        # Safely track browser browsing context
        if not self.request.session.session_key:
            self.request.session.create()
        session_key = self.request.session.session_key
        RecentlyViewedService.add_to_recently_viewed(
            product=product,
            user=self.request.user,
            session_key=session_key
        )

        # Increment analytical display metric counts
        product.increment_view_count(commit=True)

        context.update({
            "title": seo_title,
            "description": seo_desc,
            "breadcrumbs": breadcrumbs,
            "related_products": related_products,
            "variants": variants,
            "active_tags": product.tags.filter(is_active=True),
            "active_collections": product.in_collections.filter(is_active=True)
        })
        return context
    
# ==============================================================================
# DISCOVERY & MERCHANDISING VIEWS
# ==============================================================================

class ProductQuickViewView(DetailView):
    """
    Lightweight product detail view intended for AJAX quick-view modals.
    """
    model = Product
    template_name = "catalog/product-quick-view.html"
    context_object_name = "product"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return (
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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        product = self.object

        context.update({
            "variants": product.variants.filter(is_active=True),
            "gallery": product.gallery_images.all(),
            "related_products": RecommendationService.get_related_products(
                product,
                limit=4,
            ),
        })
        return context


class ProductSearchView(ListView):
    """
    Product search results.
    """
    model = Product
    template_name = "catalog/product-search.html"
    context_object_name = "products"
    paginate_by = 12

    def get_queryset(self):
        query = self.request.GET.get("q", "").strip()

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

        return qs.order_by("position", "-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        query = self.request.GET.get("q", "").strip()

        context.update({
            "search_query": query,
            "title": f"Search: {query}" if query else "Search Products",
            "description": f"Search results for '{query}'." if query else "Browse our handcrafted products.",
            "total_products": self.get_queryset().count(),
        })

        return context


class CollectionView(DetailView):
    """
    Displays all products belonging to a ProductCollection.
    """
    model = ProductCollection
    template_name = "catalog/collection-detail.html"
    context_object_name = "collection"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return ProductCollection.objects.filter(is_active=True)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        collection = self.object

        products = (
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

        context.update({
            "products": products,
            "title": collection.name,
            "description": collection.description,
            "breadcrumbs": [
                ("Collections", reverse("catalog:collection_detail", kwargs={"slug": collection.slug})),
                (collection.name, ""),
            ],
        })

        return context


class MaterialView(DetailView):
    """
    Displays all products using a particular material.
    """
    model = Material
    template_name = "catalog/material-detail.html"
    context_object_name = "material"
    slug_url_kwarg = "slug"

    def get_queryset(self):
        return Material.objects.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        material = self.object

        products = (
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

        context.update({
            "products": products,
            "title": material.name,
            "description": f"Products crafted from {material.name}.",
            "breadcrumbs": [
                ("Materials", ""),
                (material.name, ""),
            ],
        })

        return context

# ==============================================================================
# ENTERPRISE PRODUCT MANAGEMENT MODULE (Staff / CMS Facing)
# ==============================================================================

class ProductManagementMixin(UserPassesTestMixin):
    """
    Base access mixin for the Product Management module. 
    Restricts access to staff or authorized product managers.
    """
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_staff

    def handle_no_permission(self):
        messages.error(self.request, "You do not have permission to access the Product Management Module.")
        return super().handle_no_permission()


class ProductManageListView(ProductManagementMixin, ListView):
    """
    Enterprise product listing for staff/admin dashboard.
    Supports deep searching, filtering by status/collection/tags, and pagination.
    """
    model = Product
    template_name = "catalog/management/product_list.html"
    context_object_name = "products"
    paginate_by = 50

    def get_queryset(self):
        qs = Product.objects.all().select_related(
            'category', 'artisan'
        ).prefetch_related(
            'tags', 'variants'
        ).order_by('-created_at')

        # Retrieve Search Query
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(title__icontains=q) | 
                Q(sku__icontains=q) | 
                Q(barcode__icontains=q)
            )

        # Status Filtering
        status = self.request.GET.get('status')
        if status in dict(Product.ProductStatus.choices):
            qs = qs.filter(status=status)

        # Activity Filtering
        is_active = self.request.GET.get('is_active')
        if is_active in ['1', '0']:
            qs = qs.filter(is_active=(is_active == '1'))
            
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            "search_query": self.request.GET.get('q', ''),
            "status_filter": self.request.GET.get('status', ''),
            "active_filter": self.request.GET.get('is_active', ''),
            "status_choices": Product.ProductStatus.choices,
            "total_count": self.get_queryset().count()
        })
        return context


class ProductManageCreateView(ProductManagementMixin, CreateView):
    """
    Secure product creation view with transaction safety.
    """
    model = Product
    form_class = ProductForm
    template_name = "catalog/management/product_form.html"
    
    def get_success_url(self):
        messages.success(self.request, f"Product '{self.object.title}' created successfully. You can now configure variants and SEO.")
        # After creation, redirect to the update view to allow adding images/SEO/variants
        return reverse('catalog:product_manage_update', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_create'] = True
        context['title'] = "Create New Product Masterpiece"
        return context

    @transaction.atomic
    def form_valid(self, form):
        # Force initial status to Draft upon creation unless explicitly published
        if not form.cleaned_data.get('status'):
            form.instance.status = Product.ProductStatus.DRAFT
        return super().form_valid(form)


class ProductManageUpdateView(ProductManagementMixin, UpdateView):
    """
    Comprehensive product update view handling core data, SEO configuration, 
    structured schema data, and publication state changes.
    """
    model = Product
    form_class = ProductForm
    template_name = "catalog/management/product_form.html"

    def get_success_url(self):
        return reverse('catalog:product_manage_update', kwargs={'pk': self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        product = self.object
        
        # Initialize supplementary forms
        if self.request.method == 'POST':
            context['seo_form'] = ProductSEOForm(self.request.POST, self.request.FILES, instance=getattr(product, 'seo_config', None), prefix='seo')
            context['schema_form'] = ProductSchemaForm(self.request.POST, instance=getattr(product, 'schema_config', None), prefix='schema')
            context['publish_form'] = PublishingWorkflowForm(self.request.POST, instance=product, prefix='publish')
        else:
            context['seo_form'] = ProductSEOForm(instance=getattr(product, 'seo_config', None), prefix='seo')
            context['schema_form'] = ProductSchemaForm(instance=getattr(product, 'schema_config', None), prefix='schema')
            context['publish_form'] = PublishingWorkflowForm(instance=product, prefix='publish')
            
        context['is_create'] = False
        context['title'] = f"Edit Product: {product.title}"
        context['variants'] = product.variants.all()
        context['gallery'] = product.gallery_images.all()
        return context

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        """
        Intercept POST to validate and save all unified module forms concurrently.
        """
        self.object = self.get_object()
        form_class = self.get_form_class()
        form = self.get_form(form_class)
        
        seo_form = ProductSEOForm(self.request.POST, self.request.FILES, instance=getattr(self.object, 'seo_config', None), prefix='seo')
        schema_form = ProductSchemaForm(self.request.POST, instance=getattr(self.object, 'schema_config', None), prefix='schema')
        publish_form = PublishingWorkflowForm(self.request.POST, instance=self.object, prefix='publish')

        if form.is_valid() and seo_form.is_valid() and schema_form.is_valid() and publish_form.is_valid():
            return self.form_valid(form, seo_form, schema_form, publish_form)
        else:
            return self.form_invalid(form, seo_form, schema_form, publish_form)

    def form_valid(self, form, seo_form, schema_form, publish_form):
        # Save Core Product Data
        self.object = form.save()
        
        # Save Publishing State Data safely over instance
        publish_data = publish_form.save(commit=False)
        publish_data.pk = self.object.pk
        publish_data.save()
        
        # Save SEO Profile Configuration
        seo_instance = seo_form.save(commit=False)
        seo_instance.product = self.object
        seo_instance.save()
        
        # Save Schema Configuration
        schema_instance = schema_form.save(commit=False)
        schema_instance.product = self.object
        schema_instance.save()

        messages.success(self.request, f"Product '{self.object.title}' and its configurations updated successfully.")
        return HttpResponseRedirect(self.get_success_url())

    def form_invalid(self, form, seo_form, schema_form, publish_form):
        messages.error(self.request, "There were errors updating the product. Please correct the fields below.")
        return self.render_to_response(self.get_context_data(
            form=form,
            seo_form=seo_form,
            schema_form=schema_form,
            publish_form=publish_form
        ))


class ProductManageDeleteView(ProductManagementMixin, DeleteView):
    """
    Secure product deletion with confirmation and cleanup handling.
    """
    model = Product
    template_name = "catalog/management/product_confirm_delete.html"
    success_url = reverse_lazy("catalog:product_manage_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['title'] = f"Delete Product: {self.object.title}"
        return context

    @transaction.atomic
    def form_valid(self, form):
        product_title = self.object.title
        sku = self.object.sku
        # Deletion logic processed safely inside atomic block
        response = super().form_valid(form)
        logger.info(f"Product deleted by {self.request.user.username}: {product_title} (SKU: {sku})")
        messages.success(self.request, f"Product '{product_title}' was permanently deleted.")
        return response


class ProductPublishActionView(ProductManagementMixin, View):
    """
    Standalone RPC-style view for quick status toggle operations 
    (Draft, Publish, Archive) from the list view or external integrations.
    """
    def post(self, request, pk, action):
        product = get_object_or_404(Product, pk=pk)
        title = product.title
        
        try:
            with transaction.atomic():
                if action == 'publish':
                    product.status = Product.ProductStatus.PUBLISHED
                    product.is_active = True
                    if not product.published_at:
                        product.published_at = timezone.now()
                    messages.success(request, f"'{title}' has been officially published.")
                    
                elif action == 'unpublish':
                    product.status = Product.ProductStatus.DRAFT
                    product.is_active = False
                    messages.warning(request, f"'{title}' was unpublished and reverted to Draft state.")
                    
                elif action == 'archive':
                    product.status = Product.ProductStatus.ARCHIVED
                    product.is_active = False
                    messages.info(request, f"'{title}' is now securely archived.")
                    
                else:
                    messages.error(request, "Invalid publishing action requested.")
                    return redirect('catalog:product_manage_list')

                product.save(update_fields=['status', 'is_active', 'published_at', 'updated_at'])
                
        except Exception as e:
            logger.error(f"Publishing action failed for Product {pk}: {e}")
            messages.error(request, "An error occurred while attempting to change the product status.")
            
        # Determine fallback redirect
        next_url = request.POST.get('next', 'catalog:product_manage_list')
        try:
            return redirect(next_url)
        except Exception:
            return redirect('catalog:product_manage_list')