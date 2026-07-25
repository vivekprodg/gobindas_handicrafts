"""
URL configurations for the Cart application.
Contains full mappings for HTML page routes and REST JSON endpoints.
"""

from django.urls import path

from .views.api import (
    CartApplyCouponView,
    CartEstimateView,
    CartMergeView,
    CartRemoveCouponView,
    CartReorderView,
    CartSyncView,
    CartValidateView,
)
from .views.pages import (
    CartAddItemView,
    CartClearView,
    CartDetailView,
    CartMoveToCartView,
    CartRemoveItemView,
    CartSaveForLaterView,
    CartSummaryView,
    CartUpdateItemView,
    MiniCartView,
)

app_name = "cart"

urlpatterns = [
    # Cart page views (HTML)
    path("", CartDetailView.as_view(), name="cart_detail"),
    path("summary/", CartSummaryView.as_view(), name="cart_summary"),
    path("mini/", MiniCartView.as_view(), name="mini_cart"),

    # Cart item operations (HTML form handlers)
    path("add/<int:product_id>/", CartAddItemView.as_view(), name="cart_add"),
    path("add/", CartAddItemView.as_view(), name="cart_add_base"),
    path("items/<int:item_id>/update/", CartUpdateItemView.as_view(), name="cart_update"),
    path("items/<int:item_id>/delete/", CartRemoveItemView.as_view(), name="cart_remove"),
    path("items/<int:item_id>/save/", CartSaveForLaterView.as_view(), name="cart_save_for_later"),
    path("items/<int:item_id>/move-to-cart/", CartMoveToCartView.as_view(), name="cart_move_to_cart"),
    path("clear/", CartClearView.as_view(), name="cart_clear"),

    # Cart API endpoints (JSON responses)
    path("sync/", CartSyncView.as_view(), name="cart_sync"),
    path("estimate/", CartEstimateView.as_view(), name="cart_estimate"),
    path("validate/", CartValidateView.as_view(), name="cart_validate"),
    path("coupon/apply/", CartApplyCouponView.as_view(), name="cart_apply_coupon"),
    path("coupon/remove/", CartRemoveCouponView.as_view(), name="cart_remove_coupon"),
    path("merge/", CartMergeView.as_view(), name="cart_merge"),
    path("reorder/", CartReorderView.as_view(), name="cart_reorder"),
]