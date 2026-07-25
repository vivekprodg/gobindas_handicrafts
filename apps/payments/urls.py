from django.urls import path
from .views import (
    CheckoutPaymentView,
    PaymentCallbackView,
    PaymentFailedView,
    PaymentSuccessView,
)

app_name = "payments"

urlpatterns = [
    path("checkout/<str:order_id>/", CheckoutPaymentView.as_view(), name="checkout"),
    path("callback/<str:gateway>/", PaymentCallbackView.as_view(), name="callback"),
    path("success/<str:transaction_id>/", PaymentSuccessView.as_view(), name="success"),
    path("failed/<str:transaction_id>/", PaymentFailedView.as_view(), name="failed"),
]