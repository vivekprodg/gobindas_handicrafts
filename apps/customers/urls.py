from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import TemplateView

from apps.customers import views

# Safely import order class views
try:
    from apps.orders.views import OrderDetailView, OrderHistoryView, ReorderView
except ImportError:
    class OrderHistoryView(TemplateView):
        template_name = "customers/order-history.html"

    class OrderDetailView(TemplateView):
        template_name = "customers/order-detail.html"

    class ReorderView(TemplateView):
        template_name = "customers/placeholder.html"

app_name = "customers"

urlpatterns = [
    # Dashboard & Profile Management
    path("account/", views.CustomerDashboardView.as_view(), name="dashboard"),
    path("account/profile/", views.CustomerProfileUpdateView.as_view(), name="profile"),

    # Authentication Workflows
    path("account/login/", views.CustomerLoginView.as_view(), name="login"),
    path("account/logout/", views.CustomerLogoutView.as_view(), name="logout"),
    path("account/register/", views.CustomerRegistrationView.as_view(), name="register"),

    # Password Management
    path("account/password/change/", views.CustomerPasswordChangeView.as_view(), name="password_change"),
    path(
        "account/password/change/done/",
        auth_views.PasswordChangeDoneView.as_view(template_name="registration/password_change_done.html"),
        name="password_change_done",
    ),
    path("account/password/reset/", views.CustomerPasswordResetView.as_view(), name="password_reset"),
    path(
        "account/password/reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "account/password/reset/<uidb64>/<token>/",
        views.CustomerPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "account/password/reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(template_name="registration/password_reset_complete.html"),
        name="password_reset_complete",
    ),

    # Address Book Management
    path("account/addresses/", views.AddressListView.as_view(), name="address_list"),
    path("account/addresses/add/", views.AddressCreateView.as_view(), name="address_create"),
    path("account/addresses/add/legacy/", views.AddressCreateView.as_view(), name="address_add"),
    path("account/addresses/<int:pk>/edit/", views.AddressUpdateView.as_view(), name="address_update"),
    path("account/addresses/<int:pk>/edit/legacy/", views.AddressUpdateView.as_view(), name="address_edit"),
    path("account/addresses/<int:pk>/delete/", views.AddressDeleteView.as_view(), name="address_delete"),
    path(
        "account/addresses/<int:pk>/default-shipping/",
        views.AddressSetDefaultShippingView.as_view(),
        name="address_set_default_shipping",
    ),
    path(
        "account/addresses/<int:pk>/default-billing/",
        views.AddressSetDefaultBillingView.as_view(),
        name="address_set_default_billing",
    ),

    # Wishlist Routes
    path("account/wishlist/", views.WishlistView.as_view(), name="wishlist"),
    path("account/wishlist/add/", views.WishlistAddView.as_view(), name="wishlist_add_base"),
    path("account/wishlist/add/<int:product_id>/", views.WishlistAddView.as_view(), name="wishlist_add"),
    path("account/wishlist/remove/<int:pk>/", views.WishlistRemoveView.as_view(), name="wishlist_remove"),

    # Saved Cart Routes
    path("account/saved-carts/", views.SavedCartListView.as_view(), name="saved_cart_list"),
    path("account/saved-carts/<int:pk>/delete/", views.SavedCartDeleteView.as_view(), name="saved_cart_delete"),
    path("account/saved-carts/save/", TemplateView.as_view(template_name="customers/placeholder.html"), name="saved_cart_save"),
    path("account/saved-carts/<int:pk>/load/", views.SavedCartLoadView.as_view(), name="saved_cart_load"),

    # Order History Routes
    path("account/orders/", OrderHistoryView.as_view(), name="order_history"),
    path("account/orders/<str:id>/", OrderDetailView.as_view(), name="order_detail"),
    path("account/orders/<str:id>/reorder/", ReorderView.as_view(), name="order_reorder"),
]