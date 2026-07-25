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
    # 1. Django Admin Panel
    path("admin/", admin.site.urls),

    # 2. Django Allauth Google OAuth & Social Authentication Pipeline
    path("accounts/", include("allauth.urls")),

    # 3. Core Foundation App (Global utilities, auth fallbacks, store locator, track order)
    path("foundation/", include("apps.foundation.urls")),

    # 4. Cart & Checkout Pipeline
    path("cart/", include("apps.cart.urls")),

    # 5. Promotions & Coupons Engine
    path("coupons/", include("apps.coupons.urls")),

    # 6. Global Tax & Compliance Engine
    path("tax/", include("apps.tax.urls")),

    # 7. Inventory & Multi-Warehouse Management
    path("inventory/", include("apps.inventory.urls")),

    # 8. Shipping, Carrier & Fulfillment Engine
    path("shipping/", include("apps.shipping.urls")),

    # 9. Payment Gateways (eSewa, Khalti, Stripe, COD, Bank Transfer)
    path("payments/", include("apps.payments.urls")),

    # 10. Multi-Channel Notifications & Communication Center
    path("notifications/", include("apps.notifications.urls")),

    # 11. System Audit Logs & Compliance Audit Engine
    path("audit/", include("apps.audit.urls")),

    # 12. Order Management System (Single Canonical Namespace Source)
    path("orders/", include("apps.orders.urls")),

    # 13. Customer Management (Authentication, Dashboard, Profiles, Addresses, Wishlists, Saved Carts, B2B)
    path("", include("apps.customers.urls")),

    # 14. Homepage App (CMS-driven dynamic modules on the root URL)
    path("", include("apps.homepage.urls")),

    # 15. Catalog App (Masterpieces, artisans, categories, PLP/PDP, filters)
    path("", include("apps.catalog.urls")),

    # 16. Foundation Root Pages (Policies, care guides, bespoke request forms, contact)
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