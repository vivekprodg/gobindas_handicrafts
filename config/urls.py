from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

# Custom System-Wide Error Handlers for Enterprise Robustness
handler400 = "apps.foundation.views.bad_request_view"
handler403 = "apps.foundation.views.permission_denied_view"
handler404 = "apps.foundation.views.page_not_found_view"
handler500 = "apps.foundation.views.server_error_view"

urlpatterns = [
    # Django Admin Panel
    path("admin/", admin.site.urls),

    # Foundation app (Global utilities, auth fallbacks, and specific endpoints)
    path("foundation/", include("apps.foundation.urls")),

    # Orders app (Explicitly mapped to match account navigation references)
    path("account/orders/", include("apps.orders.urls")),

    # Customers app (Authentication, Dashboard, Profiles, Addresses, Wishlist, Carts, Orders)
    path("", include("apps.customers.urls")),

    # Homepage app (CMS Driven Dynamic Homepage mapped to the root)
    path("", include("apps.homepage.urls")),

    # Catalog app (Makers, categories, products)
    path("", include("apps.catalog.urls")),

    # CART APP - NEW ENTERPRISE-GRADE INTEGRATION
    path("cart/", include("apps.cart.urls")),

    # Foundation root-level general pages (Policies, care guides, etc.)
    path("", include("apps.foundation.urls_root")),
]

# Serve media and static files gracefully during local development
if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )

    urlpatterns += static(
        settings.STATIC_URL,
        document_root=settings.STATIC_ROOT,
    )