from django.urls import path
from .views import AuditLogDetailView, AuditLogListView

app_name = "audit"

urlpatterns = [
    path("logs/", AuditLogListView.as_view(), name="list"),
    path("logs/<int:pk>/", AuditLogDetailView.as_view(), name="detail"),
]