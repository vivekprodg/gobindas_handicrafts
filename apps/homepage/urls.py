from __future__ import annotations

from django.urls import path

from .views import HomepageView

# Namespace for the homepage application routing
app_name = "homepage"

urlpatterns = [
    # Fully dynamic, CMS-driven homepage entry point
    path("", HomepageView.as_view(), name="homepage"),
]