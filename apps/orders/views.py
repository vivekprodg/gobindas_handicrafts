from __future__ import annotations

import csv
import json
import logging
import mimetypes
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.generic import DeleteView, DetailView, FormView, ListView, TemplateView, UpdateView, View

from apps.cart.services.cart_core import CartService
from apps.cart.services.cart_inventory import CartInventoryService
from apps.cart.services.cart_items import CartItemService
from apps.orders import constants as c
from apps.orders import forms as order_forms
from apps.orders import selectors, services, tasks
from apps.orders import utils as u
from apps.orders.models import (
    Order,
    OrderAddressSnapshot,
    OrderAttachment,
    OrderItem,
    OrderNote,
    Payment,
    Refund,
    ReturnImage,
    ReturnRequest,
    Shipment,
)

logger = logging.getLogger(c.LOGGER_NAME)

def _coerce_uuid(value: Any) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except Exception:
        return None

def _order_or_404(order_id: Any, user: Optional[Any] = None) -> Order:
    uid = _coerce_uuid(order_id) or order_id
    order = selectors.get_order_detail(
        order_id=uid,
        user=user,
        scoped_to_user=not (user and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))),
    )
    if not order:
        raise Http404(_("Order not found or access denied."))
    return order

def _user_owns_order(user: Any, order: Order) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    return order.customer_id == getattr(user, "pk", None)

class CheckoutPrepareView(View):
    """
    Renders the checkout preparation interface and processes order creation from active cart items.
    Converts inventory reservations, records coupon redemptions, and clears the cart on success.
    """
    template_name = "orders/checkout.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.cart = CartService.get_active_cart(request)
        if not self.cart or self.cart.total_items_count == 0:
            messages.warning(request, _("Your shopping bag is empty."))
            return redirect("cart:cart_detail")

        validation = CartInventoryService.validate_for_checkout(cart=self.cart)
        if not validation.get("ready_for_checkout", False):
            issues = validation.get("issues", [])
            msg = issues[0].get("message") if issues else _("Some items in your cart are no longer available.")
            messages.error(request, msg)
            return redirect("cart:cart_detail")

        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = order_forms.CheckoutAddressForm(user=request.user)
        totals = CartService.compute_totals(self.cart)
        active_items = self.cart.items.filter(status=OrderItem.ItemStatus.ACTIVE).select_related("product", "variant")

        context = {
            "cart": self.cart,
            "cart_items": active_items,
            "totals": totals,
            "form": form,
            "page_title": _("Checkout Preparation"),
        }
        return render(request, self.template_name, context)

    @transaction.atomic
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        form = order_forms.CheckoutAddressForm(request.POST, user=request.user)
        if not form.is_valid():
            messages.error(request, _("Please fix errors in the address details."))
            return render(request, self.template_name, {
                "cart": self.cart,
                "cart_items": self.cart.items.filter(status=OrderItem.ItemStatus.ACTIVE),
                "totals": CartService.compute_totals(self.cart),
                "form": form,
            })

        data = form.cleaned_data
        user = request.user if request.user.is_authenticated else None

        # 1. Address Resolution
        saved_addr_id = data.get("saved_shipping_address")
        if saved_addr_id and saved_addr_id != "new" and user:
            from apps.customers.models import CustomerAddress
            cust_addr = get_object_or_404(CustomerAddress, pk=saved_addr_id, customer__user=user)
            shipping_snapshot = services.create_address_snapshot(
                full_name=cust_addr.full_name,
                phone_number=cust_addr.phone_number,
                address_line_1=cust_addr.address_line_1,
                address_line_2=cust_addr.address_line_2 or "",
                city=cust_addr.city,
                state_or_province=cust_addr.state_or_province,
                postal_code=cust_addr.postal_code,
                country=cust_addr.country,
            )
            email_address = user.email or data.get("email") or "guest@store.com"
        else:
            shipping_snapshot = services.create_address_snapshot(
                full_name=data.get("full_name") or (user.get_full_name() if user else "Customer"),
                phone_number=data.get("phone_number") or "",
                address_line_1=data.get("address_line_1") or "Standard Address",
                address_line_2=data.get("address_line_2") or "",
                city=data.get("city") or "Kathmandu",
                state_or_province=data.get("state_or_province") or "Bagmati",
                postal_code=data.get("postal_code") or "44600",
                country=data.get("country") or "Nepal",
            )
            email_address = data.get("email") or (user.email if user else "guest@store.com")

        totals = CartService.compute_totals(self.cart)

        # 2. Order Creation
        order = services.create_order(
            email=email_address,
            shipping_snapshot=shipping_snapshot,
            customer=user,
            currency=self.cart.currency or "USD",
            shipping_cost=totals.get("shipping", Decimal("0.00")),
            tax_total=totals.get("tax", Decimal("0.00")),
            discount_total=totals.get("discount", Decimal("0.00")),
            customer_note=data.get("customer_note", ""),
            source=Order.Source.WEB,
        )

        # 3. Transfer Cart Items -> Order Items
        active_items = self.cart.items.filter(status=OrderItem.ItemStatus.ACTIVE).select_related("product", "variant")
        for item in active_items:
            services.add_order_item(
                order=order,
                product=item.product,
                variant=item.variant,
                product_name=item.product_name_snapshot or (item.product.title if item.product else ""),
                product_sku=item.product_sku_snapshot or (item.product.sku if item.product else ""),
                variant_name=item.variant_name_snapshot or "",
                unit_price=item.unit_price_snapshot,
                quantity=item.quantity,
            )
            if item.reservation_id:
                CartInventoryService.convert_for_cart(
                    cart=self.cart,
                    cart_item=item,
                    order_reference=order.order_number,
                    user=user,
                )

        # 4. Coupon Redemption
        if self.cart.coupon_code:
            try:
                from apps.coupons.services import CouponApplicationService
                CouponApplicationService.record_coupon_redemption_for_order(
                    order=order,
                    user=user,
                )
            except Exception as exc:
                logger.warning("Coupon redemption logging skipped: %s", exc)

        # 5. Clear Cart & Redirect to Confirmation
        CartItemService.clear_cart(cart=self.cart)
        messages.success(request, _("Order #%(num)s has been placed successfully.") % {"num": order.order_number})
        return redirect("orders:checkout_success", id=str(order.pk))

class CheckoutSuccessView(LoginRequiredMixin, DetailView):
    template_name = "orders/checkout_success.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class OrderListView(LoginRequiredMixin, ListView):
    template_name = "orders/list.html"
    context_object_name = "orders"
    paginate_by = 12

    def get_queryset(self) -> QuerySet[Order]:
        return selectors.get_order_list_for_user(user=self.request.user, filters=self.request.GET)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["current_filters"] = self.request.GET.dict()
        ctx["filter_form"] = order_forms.OrderFilterForm(self.request.GET or None)
        ctx["search_form"] = order_forms.OrderSearchForm(self.request.GET or None)
        ctx["kpi_summary"] = selectors.get_kpi_summary()
        return ctx

class MyOrdersView(LoginRequiredMixin, ListView):
    template_name = "customers/order-history.html"
    context_object_name = "orders"
    paginate_by = 10

    def get_queryset(self) -> QuerySet[Order]:
        return selectors.get_customer_orders(user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["open_orders"] = selectors.get_customer_open_orders(user=user)
        ctx["completed_orders"] = selectors.get_customer_completed_orders(user=user)
        ctx["cancelled_orders"] = selectors.get_customer_cancelled_orders(user=user)
        ctx["order_count"] = selectors.get_customer_order_count(user=user)
        return ctx

class OrderHistoryView(MyOrdersView):
    pass

class OrderDetailView(LoginRequiredMixin, DetailView):
    template_name = "orders/detail.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id") or self.kwargs.get("pk"), user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        order = self.object
        ctx["items"] = selectors.get_order_items(order=order)
        ctx["payments"] = selectors.get_order_payments(order=order)
        ctx["refunds"] = selectors.get_order_refunds(order=order)
        ctx["returns"] = selectors.get_order_returns(order=order)
        ctx["timeline"] = selectors.get_order_timeline(order=order)
        ctx["shipments"] = selectors.get_order_shipments(order=order)
        ctx["attachments"] = selectors.get_order_attachments(order=order)
        ctx["notes"] = selectors.get_order_notes(order=order)
        ctx["can_cancel"] = order.can_be_cancelled
        ctx["can_refund"] = order.can_be_refunded
        return ctx

class MyOrderDetailView(OrderDetailView):
    pass

class OrderCreateView(LoginRequiredMixin, FormView):
    template_name = "orders/order_form.html"
    form_class = order_forms.OrderCreateForm

    def form_valid(self, form: order_forms.OrderCreateForm) -> HttpResponse:
        c = form.cleaned_data
        try:
            with transaction.atomic():
                order = services.create_order(
                    email=c["email"],
                    shipping_snapshot=c.get("shipping_snapshot"),
                    customer=c.get("customer"),
                    billing_snapshot=c.get("billing_snapshot"),
                    currency=c.get("currency", "USD"),
                    customer_note=c.get("customer_note", ""),
                    source=c.get("source", Order.Source.WEB),
                    shipping_cost=c.get("shipping_cost", Decimal("0.00")),
                    tax_total=c.get("tax_total", Decimal("0.00")),
                    discount_total=c.get("discount_total", Decimal("0.00")),
                    is_gift=c.get("is_gift", False),
                    gift_message=c.get("gift_message", ""),
                    gift_wrapping=c.get("gift_wrapping", ""),
                    notes=c.get("notes", ""),
                )
            messages.success(self.request, _("Order created successfully."))
            return redirect("orders:order_detail", id=str(order.pk))
        except Exception as exc:
            logger.exception("OrderCreateView failed: %s", exc)
            messages.error(self.request, _("Failed to create order."))
            return self.form_invalid(form)

class OrderUpdateView(LoginRequiredMixin, UpdateView):
    model = Order
    form_class = order_forms.OrderUpdateForm
    template_name = "orders/order_form.html"
    pk_url_kwarg = "id"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class OrderEditView(LoginRequiredMixin, UpdateView):
    model = Order
    form_class = order_forms.OrderEditForm
    template_name = "orders/order_edit.html"
    pk_url_kwarg = "id"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class OrderDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = Order
    template_name = "orders/order_confirm_delete.html"
    pk_url_kwarg = "id"
    success_url = reverse_lazy("orders:list")

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class OrderCancelView(LoginRequiredMixin, FormView):
    template_name = "orders/cancel-order.html"
    form_class = order_forms.OrderCancelForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["order"] = self.order
        return kwargs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["order"] = self.order
        ctx["can_cancel"] = self.order.can_be_cancelled
        return ctx

    def form_valid(self, form: order_forms.OrderCancelForm) -> HttpResponse:
        remarks = form.cleaned_data.get("remarks", "")
        try:
            services.cancel_order(order=self.order, user=self.request.user, remarks=remarks)
            messages.success(self.request, _("Order cancelled successfully."))
        except Exception as exc:
            logger.exception("OrderCancelView failed: %s", exc)
            messages.error(self.request, _("Failed to cancel order."))
        return redirect("orders:order_detail", id=str(self.order.pk))

class OrderConfirmView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        services.update_order_status(order=order, new_status=Order.OrderStatus.PROCESSING, user=request.user)
        messages.success(request, _("Order confirmed."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderCompleteView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        services.complete_order(order=order, user=request.user)
        messages.success(request, _("Order completed."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderHoldView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        services.hold_order(order=order, user=request.user)
        messages.success(request, _("Order placed on hold."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderResumeView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        services.resume_order(order=order, user=request.user)
        messages.success(request, _("Order resumed."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderArchiveView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        order.is_active = False
        order.save(update_fields=["is_active", "updated_at"])
        messages.success(request, _("Order archived."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderRestoreView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        order.is_active = True
        order.save(update_fields=["is_active", "updated_at"])
        messages.success(request, _("Order restored."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderSearchView(LoginRequiredMixin, ListView):
    template_name = "orders/order_search.html"
    context_object_name = "orders"

    def get_queryset(self) -> QuerySet[Order]:
        q = self.request.GET.get("q", "").strip()
        return selectors.search_orders(q) if q else Order.objects.none()

class OrderFilterView(LoginRequiredMixin, ListView):
    template_name = "orders/order_filter.html"
    context_object_name = "orders"

    def get_queryset(self) -> QuerySet[Order]:
        return selectors.get_orders(**self.request.GET.dict())

class OrderDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    template_name = "orders/order_dashboard.html"

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["kpi_summary"] = selectors.get_kpi_summary()
        ctx["sales_summary"] = selectors.get_sales_summary()
        ctx["status_distribution"] = selectors.get_status_distribution()
        return ctx

class OrderTimelineView(LoginRequiredMixin, DetailView):
    template_name = "orders/order_timeline.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class InvoiceView(LoginRequiredMixin, DetailView):
    template_name = "orders/invoice.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["items"] = selectors.get_order_items(order=self.object)
        return ctx

class DownloadInvoiceView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        payload, filename = services.generate_invoice_document(order=order)
        response = HttpResponse(payload, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

class TrackOrderView(LoginRequiredMixin, DetailView):
    template_name = "orders/track-order.html"
    context_object_name = "tracking_info"

    def get_object(self, queryset: Optional[Any] = None) -> Dict[str, Any]:
        order = _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)
        tracking = selectors.get_order_tracking_info(order_id=order.pk, user=self.request.user)
        if not tracking:
            raise Http404(_("Tracking unavailable."))
        tracking["order"] = order
        return tracking

class ShipmentDetailView(LoginRequiredMixin, DetailView):
    template_name = "orders/shipment_details.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class TrackingDetailView(LoginRequiredMixin, DetailView):
    template_name = "orders/tracking_detail.html"
    context_object_name = "shipment"
    pk_url_kwarg = "shipment_id"

    def get_object(self, queryset: Optional[QuerySet[Shipment]] = None) -> Shipment:
        shipment = get_object_or_404(Shipment.objects.select_related("order"), pk=self.kwargs.get("shipment_id"))
        if not _user_owns_order(self.request.user, shipment.order):
            raise PermissionDenied()
        return shipment

class ShipmentHistoryView(LoginRequiredMixin, DetailView):
    template_name = "orders/shipment_history.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class TrackingLookupView(LoginRequiredMixin, FormView):
    template_name = "orders/tracking_lookup.html"
    form_class = order_forms.TrackingForm

    def form_valid(self, form: order_forms.TrackingForm) -> HttpResponse:
        tracking_number = form.cleaned_data.get("tracking_number", "")
        shipment = selectors.get_shipment_by_tracking_number(tracking_number)
        if not shipment or not _user_owns_order(self.request.user, shipment.order):
            messages.error(self.request, _("Shipment not found."))
            return self.form_invalid(form)
        return redirect("orders:tracking_detail", shipment_id=shipment.pk)

class ReturnRequestView(LoginRequiredMixin, FormView):
    template_name = "orders/return_request.html"
    form_class = order_forms.ReturnRequestForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["order"] = self.order
        return kwargs

    def form_valid(self, form: order_forms.ReturnRequestForm) -> HttpResponse:
        c = form.cleaned_data
        services.create_return_request(
            order=self.order,
            return_type=c.get("return_type", ReturnRequest.ReturnType.REFUND),
            reason_category=c.get("reason_category", ReturnRequest.ReturnReasonCategory.OTHER),
            reason_text=c.get("reason_text", ""),
            requested_by=self.request.user,
        )
        messages.success(self.request, _("Return request submitted."))
        return redirect("orders:order_detail", id=str(self.order.pk))

class ReturnDetailView(LoginRequiredMixin, DetailView):
    template_name = "orders/return_detail.html"
    context_object_name = "return_request"
    pk_url_kwarg = "return_id"

    def get_object(self, queryset: Optional[QuerySet[ReturnRequest]] = None) -> ReturnRequest:
        res = get_object_or_404(ReturnRequest.objects.select_related("order"), pk=self.kwargs.get("return_id"))
        if not _user_owns_order(self.request.user, res.order):
            raise PermissionDenied()
        return res

class ReturnStatusView(ReturnDetailView):
    template_name = "orders/return_status.html"

class ReturnApprovalView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "orders/return_approval.html"
    form_class = order_forms.ReturnApprovalForm

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def form_valid(self, form: order_forms.ReturnApprovalForm) -> HttpResponse:
        ret = get_object_or_404(ReturnRequest, pk=self.kwargs.get("return_id"))
        services.approve_return(return_request=ret, approved_by=self.request.user)
        messages.success(self.request, _("Return approved."))
        return redirect("orders:return_detail", return_id=str(ret.pk))

class ReturnCompletionView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "orders/return_completion.html"
    form_class = order_forms.ReturnCompletionForm

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def form_valid(self, form: order_forms.ReturnCompletionForm) -> HttpResponse:
        ret = get_object_or_404(ReturnRequest, pk=self.kwargs.get("return_id"))
        services.complete_return(return_request=ret)
        messages.success(self.request, _("Return completed."))
        return redirect("orders:return_detail", return_id=str(ret.pk))

class PaymentDetailView(LoginRequiredMixin, DetailView):
    template_name = "orders/payment_detail.html"
    context_object_name = "payment"
    pk_url_kwarg = "payment_id"

    def get_object(self, queryset: Optional[QuerySet[Payment]] = None) -> Payment:
        payment = get_object_or_404(Payment.objects.select_related("order"), pk=self.kwargs.get("payment_id"))
        if not _user_owns_order(self.request.user, payment.order):
            raise PermissionDenied()
        return payment

class PaymentStatusView(PaymentDetailView):
    template_name = "orders/payment_status.html"

class RetryPaymentView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment = get_object_or_404(Payment.objects.select_related("order"), pk=kwargs.get("payment_id"))
        if not _user_owns_order(request.user, payment.order):
            return HttpResponseForbidden()
        messages.info(request, _("Payment retry initiated."))
        return redirect("orders:payment_detail", payment_id=payment.pk)

class RefundRequestView(LoginRequiredMixin, FormView):
    template_name = "orders/refund_request.html"
    form_class = order_forms.RefundRequestForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["order"] = self.order
        return kwargs

    def form_valid(self, form: order_forms.RefundRequestForm) -> HttpResponse:
        c = form.cleaned_data
        payment = get_object_or_404(Payment, id=c["payment_id"], order=self.order)
        services.create_refund(
            order=self.order,
            payment=payment,
            amount=c.get("amount") or payment.amount,
            reason=c["reason"],
        )
        messages.success(self.request, _("Refund request submitted."))
        return redirect("orders:order_detail", id=str(self.order.pk))

class RefundDetailView(LoginRequiredMixin, DetailView):
    template_name = "orders/refund_detail.html"
    context_object_name = "refund"
    pk_url_kwarg = "refund_id"

    def get_object(self, queryset: Optional[QuerySet[Refund]] = None) -> Refund:
        refund = get_object_or_404(Refund.objects.select_related("order"), pk=self.kwargs.get("refund_id"))
        if not _user_owns_order(self.request.user, refund.order):
            raise PermissionDenied()
        return refund

class AttachmentUploadView(LoginRequiredMixin, FormView):
    template_name = "orders/attachment_upload.html"
    form_class = order_forms.AttachmentUploadForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: order_forms.AttachmentUploadForm) -> HttpResponse:
        attachment = form.save(commit=False)
        attachment.order = self.order
        attachment.uploaded_by = self.request.user
        attachment.save()
        messages.success(self.request, _("Attachment uploaded."))
        return redirect("orders:order_detail", id=str(self.order.pk))

class AttachmentDeleteView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        attachment = get_object_or_404(OrderAttachment.objects.select_related("order"), pk=kwargs.get("attachment_id"))
        if not _user_owns_order(request.user, attachment.order):
            return HttpResponseForbidden()
        attachment.is_active = False
        attachment.save(update_fields=["is_active", "updated_at"])
        messages.success(request, _("Attachment deleted."))
        return redirect("orders:order_detail", id=str(attachment.order_id))

class AttachmentDownloadView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        attachment = get_object_or_404(OrderAttachment.objects.select_related("order"), pk=kwargs.get("attachment_id"), is_active=True)
        if not _user_owns_order(request.user, attachment.order):
            return HttpResponseForbidden()
        response = FileResponse(attachment.file.open("rb"), content_type=attachment.mime_type or "application/octet-stream")
        response["Content-Disposition"] = f'attachment; filename="{attachment.original_filename or attachment.file.name}"'
        return response

class AttachmentPreviewView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        attachment = get_object_or_404(OrderAttachment.objects.select_related("order"), pk=kwargs.get("attachment_id"), is_active=True)
        if not _user_owns_order(request.user, attachment.order):
            return HttpResponseForbidden()
        response = FileResponse(attachment.file.open("rb"), content_type=attachment.mime_type or "application/octet-stream")
        response["Content-Disposition"] = f'inline; filename="{attachment.original_filename or attachment.file.name}"'
        return response

class OrderExportView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "orders/order_export.html"
    form_class = order_forms.OrderExportForm

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def form_valid(self, form: order_forms.OrderExportForm) -> HttpResponse:
        return order_csv_export_view(self.request)

class OrderImportView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "orders/order_import.html"
    form_class = order_forms.OrderImportForm

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def form_valid(self, form: order_forms.OrderImportForm) -> HttpResponse:
        messages.info(self.request, _("Order import queued."))
        return redirect("orders:order_dashboard")

class OrderStatusRefreshView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return JsonResponse({"order_id": str(order.pk), "status": order.status, "payment_status": order.payment_status})

class OrderTimelineRefreshView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        events = selectors.get_order_timeline(order=order, customer_visible_only=True)[:10]
        return JsonResponse({"order_id": str(order.pk), "events": [{"title": e.title, "event_type": e.event_type} for e in events]})

class OrderTrackingRefreshView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return JsonResponse({"order_id": str(order.pk), "carrier": order.carrier, "tracking_number": order.tracking_number})

class OrderSearchEndpointView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        q = request.GET.get("q", "").strip()
        results = selectors.search_orders(q)[:10] if q else []
        return JsonResponse({"results": [{"id": str(o.pk), "order_number": o.order_number, "email": o.email} for o in results]})

class OrderAutocompleteView(OrderSearchEndpointView):
    pass

class ReorderView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        try:
            services.reorder_items_into_cart(order=order, user=request.user, session_key=request.session.session_key)
            messages.success(request, _("Items reordered into your active cart."))
        except Exception as exc:
            logger.exception("ReorderView failed: %s", exc)
            messages.error(request, _("Failed to reorder items."))
        return redirect("cart:cart_detail")

class OrderItemCreateView(LoginRequiredMixin, FormView):
    template_name = "orders/order_item_create.html"
    form_class = order_forms.OrderItemCreateForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: order_forms.OrderItemCreateForm) -> HttpResponse:
        c = form.cleaned_data
        services.add_order_item(
            order=self.order,
            product_name=c["product_name"],
            unit_price=c["unit_price"],
            quantity=c["quantity"],
        )
        messages.success(self.request, _("Item added to order."))
        return redirect("orders:order_detail", id=str(self.order.pk))

class OrderItemUpdateView(LoginRequiredMixin, UpdateView):
    model = OrderItem
    form_class = order_forms.OrderItemUpdateForm
    template_name = "orders/order_item_update.html"
    pk_url_kwarg = "item_id"

    def get_object(self, queryset: Optional[QuerySet[OrderItem]] = None) -> OrderItem:
        item = get_object_or_404(OrderItem.objects.select_related("order"), pk=self.kwargs.get("item_id"))
        if not _user_owns_order(self.request.user, item.order):
            raise PermissionDenied()
        return item

class OrderItemDeleteView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        item = get_object_or_404(OrderItem.objects.select_related("order"), pk=kwargs.get("item_id"))
        if not _user_owns_order(request.user, item.order):
            return HttpResponseForbidden()
        order_id = str(item.order_id)
        services.delete_order_item(item=item)
        messages.success(request, _("Line item deleted."))
        return redirect("orders:order_detail", id=order_id)

class OrderItemQuantityUpdateView(LoginRequiredMixin, FormView):
    template_name = "orders/order_item_quantity.html"
    form_class = order_forms.QuantityUpdateForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.item = get_object_or_404(OrderItem.objects.select_related("order"), pk=kwargs.get("item_id"))
        if not _user_owns_order(request.user, self.item.order):
            raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: order_forms.QuantityUpdateForm) -> HttpResponse:
        services.update_order_item_quantity(item=self.item, new_quantity=form.cleaned_data["quantity"])
        messages.success(self.request, _("Quantity updated."))
        return redirect("orders:order_detail", id=str(self.item.order_id))

class OrderItemStatusUpdateView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        item = get_object_or_404(OrderItem.objects.select_related("order"), pk=kwargs.get("item_id"))
        if not _user_owns_order(request.user, item.order):
            return HttpResponseForbidden()
        services.update_order_item_status(item=item, new_status=request.POST.get("status", ""))
        messages.success(request, _("Item status updated."))
        return redirect("orders:order_detail", id=str(item.order_id))

class ShipmentCreateView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "orders/shipment_create.html"
    form_class = order_forms.ShipmentForm

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: order_forms.ShipmentForm) -> HttpResponse:
        c = form.cleaned_data
        services.create_shipment(order=self.order, carrier=c.get("carrier", "Unknown"), tracking_number=c.get("tracking_number", ""))
        messages.success(self.request, _("Shipment created."))
        return redirect("orders:shipment_detail", id=str(self.order.pk))

class ShipmentStatusUpdateView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        shipment = get_object_or_404(Shipment, pk=kwargs.get("shipment_id"))
        shipment.status = request.POST.get("status", shipment.status)
        shipment.save(update_fields=["status", "updated_at"])
        messages.success(request, _("Shipment status updated."))
        return redirect("orders:tracking_detail", shipment_id=shipment.pk)

class OrderNoteCreateView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        text = request.POST.get("text", "").strip()
        if text:
            OrderNote.objects.create(order=order, author=request.user, text=text, note_type=OrderNote.NoteType.CUSTOMER)
            messages.success(request, _("Note added."))
        return redirect("orders:order_detail", id=str(order.pk))

class PaymentStatusUpdateView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment = get_object_or_404(Payment, pk=kwargs.get("payment_id"))
        services.update_payment_status(payment=payment, new_status=request.POST.get("status", ""))
        messages.success(request, _("Payment status updated."))
        return redirect("orders:payment_detail", payment_id=payment.pk)

class RefundApprovalView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "orders/refund_approval.html"
    form_class = order_forms.RefundApprovalForm

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def form_valid(self, form: order_forms.RefundApprovalForm) -> HttpResponse:
        refund = get_object_or_404(Refund, pk=self.kwargs.get("refund_id"))
        services.approve_refund(refund=refund, approved_by=self.request.user)
        messages.success(self.request, _("Refund approved."))
        return redirect("orders:refund_detail", refund_id=refund.pk)

class RefundRejectionView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        refund = get_object_or_404(Refund, pk=kwargs.get("refund_id"))
        services.reject_refund(refund=refund, approved_by=request.user, rejection_reason=request.POST.get("reason", ""))
        messages.success(request, _("Refund rejected."))
        return redirect("orders:refund_detail", refund_id=refund.pk)

class RefundCompletionView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    template_name = "orders/refund_completion.html"
    form_class = order_forms.RefundCompletionForm

    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def form_valid(self, form: order_forms.RefundCompletionForm) -> HttpResponse:
        refund = get_object_or_404(Refund, pk=self.kwargs.get("refund_id"))
        services.complete_refund(refund=refund)
        messages.success(self.request, _("Refund completed."))
        return redirect("orders:refund_detail", refund_id=refund.pk)

class OrderQuickSearchView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        q = request.GET.get("q", "").strip()
        qs = selectors.search_orders(q)[:10] if q else Order.objects.none()
        return render(request, "orders/_quick_search_results.html", {"results": qs, "query": q})

class OrderHealthView(View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        return JsonResponse({"status": "ok", "app": "orders", "timestamp": u.format_iso(timezone.now())})

@login_required
@require_http_methods(["GET"])
def order_csv_export_view(request: HttpRequest) -> HttpResponse:
    qs = selectors.get_orders_for_csv_export()
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="orders_export_{u.format_export_timestamp()}.csv"'
    response.write(c.CSV_BOM)
    writer = csv.writer(response)
    writer.writerow(c.CSV_EXPORT_FIELDS)
    for o in qs.iterator():
        writer.writerow([getattr(o, field, "") for field in c.CSV_EXPORT_FIELDS])
    return response

class OrderAddressFormView(LoginRequiredMixin, FormView):
    template_name = "orders/address_form.html"
    form_class = order_forms.OrderAddressForm

    def form_valid(self, form: order_forms.OrderAddressForm) -> HttpResponse:
        c = form.cleaned_data
        snapshot = services.create_address_snapshot(
            full_name=c.get("full_name", ""),
            phone_number=c.get("phone_number", ""),
            address_line_1=c.get("address_line_1", ""),
            city=c.get("city", ""),
            state_or_province=c.get("state_or_province", ""),
            postal_code=c.get("postal_code", ""),
            country=c.get("country", ""),
        )
        messages.success(self.request, _("Address saved."))
        return redirect(f"{reverse('orders:order_create')}?shipping_snapshot={snapshot.pk}")

@method_decorator(csrf_exempt, name="dispatch")
class OrderStatusAPIView(View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        return JsonResponse({
            "order_statuses": [{"value": v, "label": l} for v, l in Order.OrderStatus.choices],
            "payment_statuses": [{"value": v, "label": l} for v, l in Order.PaymentStatus.choices],
        })

class OrderKPIsAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    def test_func(self) -> bool:
        return getattr(self.request.user, "is_staff", False)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        return JsonResponse(selectors.get_kpi_summary())

__all__ = [
    "CheckoutPrepareView", "CheckoutSuccessView", "OrderListView", "MyOrdersView", "OrderHistoryView", "OrderDetailView",
    "MyOrderDetailView", "OrderCreateView", "OrderUpdateView", "OrderEditView",
    "OrderDeleteView", "OrderCancelView", "OrderConfirmView", "OrderCompleteView",
    "OrderHoldView", "OrderResumeView", "OrderArchiveView", "OrderRestoreView",
    "OrderSearchView", "OrderFilterView", "OrderDashboardView", "OrderTimelineView",
    "InvoiceView", "DownloadInvoiceView", "TrackOrderView", "ShipmentDetailView",
    "TrackingDetailView", "ShipmentHistoryView", "TrackingLookupView", "ReturnRequestView",
    "ReturnDetailView", "ReturnStatusView", "ReturnApprovalView", "ReturnCompletionView",
    "PaymentDetailView", "PaymentStatusView", "RetryPaymentView", "RefundRequestView",
    "RefundDetailView", "AttachmentUploadView", "AttachmentDeleteView", "AttachmentDownloadView",
    "AttachmentPreviewView", "OrderExportView", "OrderImportView", "OrderStatusRefreshView",
    "OrderTimelineRefreshView", "OrderTrackingRefreshView", "OrderSearchEndpointView",
    "OrderAutocompleteView", "ReorderView", "OrderItemCreateView", "OrderItemUpdateView",
    "OrderItemDeleteView", "OrderItemQuantityUpdateView", "OrderItemStatusUpdateView",
    "ShipmentCreateView", "ShipmentStatusUpdateView", "OrderNoteCreateView",
    "PaymentStatusUpdateView", "RefundApprovalView", "RefundRejectionView",
    "RefundCompletionView", "OrderQuickSearchView", "OrderHealthView", "order_csv_export_view",
    "OrderAddressFormView", "OrderStatusAPIView", "OrderKPIsAPIView",
]