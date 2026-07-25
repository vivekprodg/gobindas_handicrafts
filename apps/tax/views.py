import json
from decimal import Decimal
from typing import Any, Dict

from django.http import HttpRequest, JsonResponse
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.cart.services.cart_core import CartService
from .selectors import get_tax_settings
from .services import TaxCalculationService

class CalculateTaxAPIView(View):
    """
    REST API endpoint querying dynamic tax estimation for active cart subtotal.
    """

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        cart = CartService.get_active_cart(request)
        if not cart:
            return JsonResponse({"success": False, "message": str(_("Active cart not found."))}, status=400)

        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        except Exception:
            payload = {}

        country_code = str(payload.get("country_code", "NP")).strip().upper()
        state_or_province = str(payload.get("state_or_province", "")).strip()
        postal_code = str(payload.get("postal_code", "")).strip()

        tax_result = TaxCalculationService.calculate_cart_tax(
            cart=cart,
            country_code=country_code,
            state_or_province=state_or_province,
            postal_code=postal_code,
            user=request.user if request.user.is_authenticated else None,
        )

        return JsonResponse({
            "success": True,
            "tax_total": str(tax_result["tax_total"]),
            "calculation_mode": tax_result.get("calculation_mode", "exclusive"),
            "is_exempt": tax_result.get("is_exempt", False),
            "tax_lines": tax_result.get("tax_lines", []),
            "cart_subtotal": str(cart.subtotal),
            "grand_total": str(cart.subtotal + tax_result["tax_total"] + cart.estimated_shipping - (cart.coupon_discount_amount or Decimal("0.00"))),
        })