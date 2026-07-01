from django.urls import path
from django.contrib.auth import views as auth_views
from .views import (
    HomePageView,
    StoreLocatorView,
    TrackOrderView,
    CustomLoginView,
    CustomLogoutView,
    RegisterView,
    CustomPasswordResetView,
    CustomPasswordResetConfirmView,
)

app_name = "foundation"

urlpatterns = [
    path("", HomePageView.as_view(), name="home"),
    path("store-locator/", StoreLocatorView.as_view(), name="store_locator"),
    path("track-order/", TrackOrderView.as_view(), name="track_order"),
    path("login/", CustomLoginView.as_view(), name="login"),
    path("logout/", CustomLogoutView.as_view(), name="logout"),
    path("register/", RegisterView.as_view(), name="register"),
    
    # Password Reset
    path("password-reset/", CustomPasswordResetView.as_view(), name="password_reset"),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "password-reset/confirm/<uidb64>/<token>/",
        CustomPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        auth_views.PasswordResetCompleteView.as_view(template_name="registration/password_reset_complete.html"),
        name="password_reset_complete",
    ),
]