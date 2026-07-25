from django.urls import path
from .views import CalculateShippingRatesAPIView, PrintableShippingLabelView, ShipmentTrackingView

app_name = "shipping"

urlpatterns = [
    path("api/rates/", CalculateShippingRatesAPIView.as_view(), name="rates"),
    path("tracking/", ShipmentTrackingView.as_view(), name="tracking"),
    path("tracking/<str:tracking_number>/", ShipmentTrackingView.as_view(), name="tracking_detail"),
    path("label/<int:shipment_id>/", PrintableShippingLabelView.as_view(), name="label"),
]