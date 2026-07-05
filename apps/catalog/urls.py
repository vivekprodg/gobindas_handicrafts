from django.urls import path
from .views import (
    CategoryListingView,
    ArtisansListView,
    ArtisanDetailView,
    ProductDetailView,
    ProductManageListView,
    ProductManageCreateView,
    ProductManageUpdateView,
    ProductManageDeleteView,
    ProductPublishActionView,
    ProductQuickViewView,
    ProductSearchView,
    CollectionView,
    MaterialView,
)

app_name = "catalog"

urlpatterns = [
    # ==============================================================================
    # PUBLIC CATALOG ROUTES
    # ==============================================================================
    
    # Generic parameterised catalog views
    # Handles all category requests dynamically via database lookup
    path(
        "category/<slug:slug>/", 
        CategoryListingView.as_view(), 
        name="category_detail"
    ),
    
    # Product detail view
    path(
        "product/<slug:slug>/", 
        ProductDetailView.as_view(), 
        name="product_detail"
    ),
    
    # Artisans / makers listing
    path(
        "artisans/", 
        ArtisansListView.as_view(), 
        name="artisans"
    ),
    path(
        "artisan/", 
        ArtisansListView.as_view(), 
        name="artisan_list_alias"
    ),
    
    # Artisan detail / profile view
    path(
        "artisans/<slug:slug>/", 
        ArtisanDetailView.as_view(), 
        name="artisan_detail"
    ),
    path(
        "artisan/<slug:slug>/", 
        ArtisanDetailView.as_view(), 
        name="artisan_detail_singular"
    ),

    # New Discovery & Merchandising Routes
    path(
        "quick-view/<slug:slug>/",
        ProductQuickViewView.as_view(),
        name="product_quick_view"
    ),
    path(
        "search/",
        ProductSearchView.as_view(),
        name="product_search"
    ),
    path(
        "collection/<slug:slug>/",
        CollectionView.as_view(),
        name="collection_detail"
    ),
    path(
        "material/<slug:slug>/",
        MaterialView.as_view(),
        name="material_detail"
    ),

    # ==============================================================================
    # ENTERPRISE PRODUCT MANAGEMENT ROUTES (CMS / Staff Dashboard)
    # ==============================================================================
    
    # Product Management Dashboard / List
    path(
        "manage/products/",
        ProductManageListView.as_view(),
        name="product_manage_list"
    ),
    
    # Product Creation Workflow
    path(
        "manage/products/create/",
        ProductManageCreateView.as_view(),
        name="product_manage_create"
    ),
    
    # Product Update & Configuration Workflow (Core, SEO, Schema, Publish state)
    path(
        "manage/products/<int:pk>/edit/",
        ProductManageUpdateView.as_view(),
        name="product_manage_update"
    ),
    
    # Product Deletion Workflow
    path(
        "manage/products/<int:pk>/delete/",
        ProductManageDeleteView.as_view(),
        name="product_manage_delete"
    ),
    
    # Product Quick Publishing Actions (RPC-style status toggles)
    path(
        "manage/products/<int:pk>/action/<str:action>/",
        ProductPublishActionView.as_view(),
        name="product_publish_action"
    ),
]