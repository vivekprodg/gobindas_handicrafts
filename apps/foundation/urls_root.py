from django.urls import path

from .views import (
    CareGuidesPlaceholderView,
    ContactPageView,
    CustomOrdersPlaceholderView,
    PoliciesShippingPlaceholderView,
    TraceabilityPlaceholderView,
)

urlpatterns = [
    path("care-guides/", CareGuidesPlaceholderView.as_view(), name="care_guides"),
    path("traceability/", TraceabilityPlaceholderView.as_view(), name="traceability"),
    path("policies/shipping/", PoliciesShippingPlaceholderView.as_view(), name="policies_shipping"),
    path("custom-orders/", CustomOrdersPlaceholderView.as_view(), name="custom_orders"),
    path("contact/", ContactPageView.as_view(), name="contact"),
]