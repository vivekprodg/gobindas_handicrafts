from django.urls import path

from .views import (
    CareGuidesPlaceholderView,
    ContactPageView,
    CustomOrdersPlaceholderView,
    DigitalBusinessCardView,
    ExportVCardView,
    PoliciesShippingPlaceholderView,
    TraceabilityPlaceholderView,
)

urlpatterns = [
    path("care-guides/", CareGuidesPlaceholderView.as_view(), name="care_guides"),
    path("traceability/", TraceabilityPlaceholderView.as_view(), name="traceability"),
    path("policies/shipping/", PoliciesShippingPlaceholderView.as_view(), name="policies_shipping"),
    path("custom-orders/", CustomOrdersPlaceholderView.as_view(), name="custom_orders"),
    path("contact/", ContactPageView.as_view(), name="contact"),
    path("card/<slug:slug>/", DigitalBusinessCardView.as_view(), name="digital_card"),
    path("card/<slug:slug>/vcf/", ExportVCardView.as_view(), name="export_vcard"),
]