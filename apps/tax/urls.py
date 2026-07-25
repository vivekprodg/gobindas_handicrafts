from django.urls import path
from .views import CalculateTaxAPIView

app_name = "tax"

urlpatterns = [
    path("api/estimate/", CalculateTaxAPIView.as_view(), name="estimate"),
]