"""
URL routes for the Coupons application.
"""
from __future__ import annotations

from django.urls import path
from . import views

app_name = "coupons"

urlpatterns = [
    # Cart & Checkout Coupon Mutations
    path("apply/", views.ApplyCouponView.as_view(), name="apply_coupon"),
    path("remove/", views.RemoveCouponView.as_view(), name="remove_coupon"),

    # Public Coupons & REST APIs
    path("public/", views.PublicCouponsListView.as_view(), name="public_coupons"),
    path("api/validate/", views.ValidateCouponAPIView.as_view(), name="api_validate_coupon"),
]