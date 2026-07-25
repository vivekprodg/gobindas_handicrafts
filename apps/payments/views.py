import json
from decimal import Decimal
from typing import Any, Dict, Optional

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.orders.models import Order
from . import constants as c
from .forms import CheckoutPaymentSelectForm
from .selectors import get_available_payment_gateways, get_transaction_by_id
from .services import EsewaService, KhaltiService, PaymentService

class CheckoutPaymentView(View):
    """
    Renders payment gateway options for an unconfirmed checkout order.
    """
    template_name = "payments/checkout_payment.html"

    def get(self, request: HttpRequest, order_id: str, *args: Any, **kwargs: Any) -> HttpResponse:
        order = get_object_or_404(Order, pk=order_id)
        gateways = get_available_payment_gateways()

        return render(request, self.template_name, {
            "order": order,
            "gateways": gateways,
        })

    def post(self, request: HttpRequest, order_id: str, *args: Any, **kwargs: Any) -> HttpResponse:
        order = get_object_or_404(Order, pk=order_id)
        gateway = request.POST.get("gateway", "").lower()

        ip = request.META.get("REMOTE_ADDR", "")
        txn = PaymentService.create_transaction(order=order, gateway=gateway, ip_address=ip)

        if gateway == c.PaymentGatewayCode.ESEWA:
            payload = EsewaService.prepare_payment_payload(txn, request)
            return render(request, "payments/esewa_redirect.html", {"payload": payload})

        if gateway == c.PaymentGatewayCode.KHALTI:
            res = KhaltiService.initiate_payment(txn, request)
            if res.get("payment_url"):
                return redirect(res["payment_url"])

        if gateway == c.PaymentGatewayCode.COD:
            PaymentService.process_payment_success(txn, gateway_ref="COD-COLLECT")
            return redirect("payments:success", transaction_id=txn.transaction_id)

        if gateway == c.PaymentGatewayCode.BANK_TRANSFER:
            return redirect("payments:success", transaction_id=txn.transaction_id)

        messages.error(request, _("Selected payment method is currently unavailable."))
        return redirect("payments:checkout", order_id=str(order.pk))

class PaymentCallbackView(View):
    """
    Callback landing endpoint for eSewa and Khalti payment confirmations.
    """

    def get(self, request: HttpRequest, gateway: str, *args: Any, **kwargs: Any) -> HttpResponse:
        if gateway == "esewa":
            encoded_data = request.GET.get("data", "")
            if not encoded_data:
                messages.error(request, _("Invalid response from eSewa."))
                return redirect("cart:cart_detail")

            is_valid, txn_id, payload = EsewaService.verify_response(encoded_data)
            txn = get_transaction_by_id(txn_id)

            if is_valid and txn:
                PaymentService.process_payment_success(txn, gateway_ref=payload.get("transaction_code", ""), raw_payload=payload)
                return redirect("payments:success", transaction_id=txn.transaction_id)
            elif txn:
                txn.mark_failed(payload=payload)
                return redirect("payments:failed", transaction_id=txn.transaction_id)

        return redirect("cart:cart_detail")

class PaymentSuccessView(View):
    template_name = "payments/payment_success.html"

    def get(self, request: HttpRequest, transaction_id: str, *args: Any, **kwargs: Any) -> HttpResponse:
        txn = get_transaction_by_id(transaction_id)
        if not txn:
            raise Http404(_("Transaction not found."))

        return render(request, self.template_name, {
            "transaction": txn,
            "order": txn.order,
        })

class PaymentFailedView(View):
    template_name = "payments/payment_failed.html"

    def get(self, request: HttpRequest, transaction_id: str, *args: Any, **kwargs: Any) -> HttpResponse:
        txn = get_transaction_by_id(transaction_id)
        if not txn:
            raise Http404(_("Transaction not found."))

        return render(request, self.template_name, {
            "transaction": txn,
            "order": txn.order,
        })