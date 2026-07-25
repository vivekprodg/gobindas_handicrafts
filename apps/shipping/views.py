import json
from decimal import Decimal
from typing import Any, Dict, Optional

from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.cart.services.cart_core import CartService
from .forms import TrackingLookupForm
from .selectors import get_tracking_by_number
from .serializers import ShippingSerializer
from .services import ShippingCalculationService

class CalculateShippingRatesAPIView(View):
    """
    REST API endpoint querying dynamic shipping rate options for current cart and destination.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        cart = CartService.get_active_cart(request)
        if not cart:
            return JsonResponse({"success": False, "message": str(_("Cart not found."))}, status=400)

        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        except Exception:
            payload = {}

        country_code = str(payload.get("country_code", "NP")).strip().upper()
        state_or_province = str(payload.get("state_or_province", "")).strip()
        selected_method_code = str(payload.get("method_code", "")).strip()

        options = ShippingCalculationService.calculate_shipping_options(
            cart=cart,
            country_code=country_code,
            state_or_province=state_or_province,
            selected_method_code=selected_method_code,
        )

        return JsonResponse(options)

class ShipmentTrackingView(View):
    """
    Public order tracking lookup view.
    """
    template_name = "shipping/tracking.html"

    def get(self, request: HttpRequest, tracking_number: Optional[str] = None, *args: Any, **kwargs: Any) -> Any:
        form = TrackingLookupForm(initial={"tracking_number": tracking_number or ""})
        record = get_tracking_by_number(tracking_number or "") if tracking_number else None

        return render(request, self.template_name, {
            "form": form,
            "tracking_record": record,
            "tracking_number": tracking_number,
        })

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        form = TrackingLookupForm(request.POST)
        record = None
        tracking_number = ""

        if form.is_valid():
            tracking_number = form.cleaned_data["tracking_number"]
            record = get_tracking_by_number(tracking_number)

        return render(request, self.template_name, {
            "form": form,
            "tracking_record": record,
            "tracking_number": tracking_number,
        })

class PrintableShippingLabelView(View):
    """
    Renders printable dispatch shipping labels for warehouse operators.
    """
    template_name = "shipping/label.html"

    def get(self, request: HttpRequest, shipment_id: int, *args: Any, **kwargs: Any) -> Any:
        from apps.orders.models import Shipment
        shipment = get_object_or_404(Shipment.objects.select_related("order", "warehouse"), pk=shipment_id)

        return render(request, self.template_name, {
            "shipment": shipment,
            "order": shipment.order,
        })