"""
URL configurations for the Catalog application.
Maps public storefront PLP/PDP endpoints and staff catalog management views.
"""

from django.urls import path

from .views import (
    ArtisanDetailView,
    ArtisansListView,
    CategoryListingView,
    CollectionView,
    MaterialView,
    ProductDetailView,
    ProductManageCreateView,
    ProductManageDeleteView,
    ProductManageListView,
    ProductManageUpdateView,
    ProductPublishActionView,
    ProductQuickViewView,
    ProductSearchView,
)

app_name = "catalog"

urlpatterns = [
    # Public Storefront Catalog Routes
    path(
        "category/<slug:slug>/",
        CategoryListingView.as_view(),
        name="category_detail",
    ),
    path(
        "product/<slug:slug>/",
        ProductDetailView.as_view(),
        name="product_detail",
    ),
    path(
        "artisans/",
        ArtisansListView.as_view(),
        name="artisans",
    ),
    path(
        "artisan/",
        ArtisansListView.as_view(),
        name="artisan_list_alias",
    ),
    path(
        "artisans/<slug:slug>/",
        ArtisanDetailView.as_view(),
        name="artisan_detail",
    ),
    path(
        "artisan/<slug:slug>/",
        ArtisanDetailView.as_view(),
        name="artisan_detail_singular",
    ),
    path(
        "quick-view/<slug:slug>/",
        ProductQuickViewView.as_view(),
        name="product_quick_view",
    ),
    path(
        "search/",
        ProductSearchView.as_view(),
        name="product_search",
    ),
    path(
        "collection/<slug:slug>/",
        CollectionView.as_view(),
        name="collection_detail",
    ),
    path(
        "material/<slug:slug>/",
        MaterialView.as_view(),
        name="material_detail",
    ),

    # Staff Catalog Management Routes
    path(
        "manage/products/",
        ProductManageListView.as_view(),
        name="product_manage_list",
    ),
    path(
        "manage/products/create/",
        ProductManageCreateView.as_view(),
        name="product_manage_create",
    ),
    path(
        "manage/products/<int:pk>/edit/",
        ProductManageUpdateView.as_view(),
        name="product_manage_update",
    ),
    path(
        "manage/products/<int:pk>/delete/",
        ProductManageDeleteView.as_view(),
        name="product_manage_delete",
    ),
    path(
        "manage/products/<int:pk>/action/<str:action>/",
        ProductPublishActionView.as_view(),
        name="product_publish_action",
    ),
]