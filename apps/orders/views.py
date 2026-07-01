import logging
from typing import Any, Dict, Optional

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError, PermissionDenied
from django.http import HttpRequest, HttpResponse, Http404
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, DetailView, FormView, View

from . import selectors
from . import services
from .forms import OrderCancelForm, ReturnRequestForm, OrderRefundRequestForm
from .models import Payment

logger = logging.getLogger(__name__)


class OrderListView(LoginRequiredMixin, ListView):
    """
    Enterprise-grade List View for customer orders.
    Handles pagination, filtering, sorting, and searching via selectors.
    Ensures users can only see their own orders unless authorized otherwise.
    """
    template_name = 'orders/order_list.html'
    context_object_name = 'orders'
    paginate_by = 12

    def get_queryset(self) -> Any:
        """
        Delegates the database query to the selector layer to enforce thin views
        and domain-driven design principles.
        """
        try:
            return selectors.get_order_list_for_user(
                user=self.request.user,
                filters=self.request.GET
            )
        except Exception as e:
            logger.error(f"Error fetching order list for user {self.request.user.id}: {str(e)}")
            return []

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        """
        Injects active filter states into the context to maintain UI synchronization.
        """
        context = super().get_context_data(**kwargs)
        context['current_filters'] = self.request.GET.dict()
        return context


class OrderHistoryView(LoginRequiredMixin, ListView):
    """
    Maintains complete customer order history.
    Properly segregated to the orders application adhering to DDD principles.
    """
    template_name = 'customers/order_history.html'
    context_object_name = 'orders'
    paginate_by = 10

    def get_queryset(self) -> Any:
        try:
            return selectors.get_customer_orders(user=self.request.user)
        except Exception as e:
            logger.error(f"Error fetching order history for user {self.request.user.id}: {str(e)}")
            return []


class OrderDetailView(LoginRequiredMixin, DetailView):
    """
    Comprehensive Detail View displaying order information, items, billing, 
    shipping, payment details, and timeline history.
    """
    template_name = 'orders/order_detail.html'
    context_object_name = 'order'

    def get_object(self, queryset: Optional[Any] = None) -> Any:
        """
        Retrieves the order instance via selector, ensuring strict authorization checks.
        """
        order_id = self.kwargs.get('id')
        order = selectors.get_order_detail(order_id=order_id, user=self.request.user)
        
        if not order:
            logger.warning(f"Unauthorized or invalid order access attempt by user {self.request.user.id} for order {order_id}")
            raise Http404(_("Order not found or you do not have permission to view it."))
        
        return order


class InvoiceView(LoginRequiredMixin, DetailView):
    """
    Displays the printable HTML invoice for a specific order.
    Reuses order detail context but formats specifically for printing via template.
    """
    template_name = 'orders/invoice.html'
    context_object_name = 'order'

    def get_object(self, queryset: Optional[Any] = None) -> Any:
        order_id = self.kwargs.get('id')
        order = selectors.get_order_detail(order_id=order_id, user=self.request.user)
        
        if not order:
            raise Http404(_("Invoice not found or access denied."))
            
        return order


class DownloadInvoiceView(LoginRequiredMixin, View):
    """
    Handles the generation and download of an invoice in the system's preferred format (e.g., PDF).
    Delegates generation logic entirely to the service layer.
    """
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order_id = self.kwargs.get('id')
        order = selectors.get_order_detail(order_id=order_id, user=request.user)
        
        if not order:
            raise Http404(_("Order not found or access denied."))
            
        try:
            # Delegate to service layer for PDF/File generation
            file_response, filename = services.generate_invoice_document(order=order)
            
            response = HttpResponse(file_response, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
            
        except ValidationError as e:
            for msg in getattr(e, 'messages', [str(e)]):
                messages.error(request, msg)
            return redirect('orders:order_detail', id=order_id)
        except Exception as e:
            logger.error(f"Invoice generation failed for order {order_id}: {str(e)}", exc_info=True)
            messages.error(request, _("An unexpected error occurred while generating your invoice."))
            return redirect('orders:order_detail', id=order_id)


class TrackOrderView(LoginRequiredMixin, DetailView):
    """
    Displays shipment timeline, carrier status, and estimated delivery frames.
    """
    template_name = 'orders/track_order.html'
    context_object_name = 'tracking_info'

    def get_object(self, queryset: Optional[Any] = None) -> Any:
        order_id = self.kwargs.get('id')
        
        # Selectors retrieve aggregated tracking and shipment details
        tracking_info = selectors.get_order_tracking_info(order_id=order_id, user=self.request.user)
        
        if not tracking_info:
            raise Http404(_("Tracking information is currently unavailable for this order."))
            
        return tracking_info


class ShipmentDetailsView(LoginRequiredMixin, DetailView):
    """
    Isolates and displays fulfillment, shipping, and courier data.
    Ensures clear separation from the primary financial detail view.
    """
    template_name = 'orders/shipment_details.html'
    context_object_name = 'order'

    def get_object(self, queryset: Optional[Any] = None) -> Any:
        order_id = self.kwargs.get('id')
        order = selectors.get_order_detail(order_id=order_id, user=self.request.user)
        
        if not order:
            raise Http404(_("Order not found or access denied."))
            
        return order

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        # Pull explicitly related shipment snapshots via the service selector
        context['shipments'] = selectors.get_order_shipments(self.object)
        return context


class CancelOrderView(LoginRequiredMixin, FormView):
    """
    Manages customer-initiated order cancellation requests.
    Validates eligibility through form and delegates state mutation to services.
    """
    template_name = 'orders/cancel_order.html'
    form_class = OrderCancelForm

    def get_form_kwargs(self) -> Dict[str, Any]:
        """
        Injects the target order into the form for eligibility validation.
        """
        kwargs = super().get_form_kwargs()
        order_id = self.kwargs.get('id')
        
        self.order = selectors.get_order_detail(order_id=order_id, user=self.request.user)
        if not self.order:
            raise Http404(_("Order not found or access denied."))
            
        kwargs['order'] = self.order
        return kwargs

    def form_valid(self, form: Any) -> HttpResponse:
        """
        Executes cancellation business logic via the service layer upon validation.
        """
        try:
            services.cancel_order(
                order=self.order,
                user=self.request.user,
                remarks=form.cleaned_data.get('remarks', '')
            )
            messages.success(self.request, _("Your order has been successfully cancelled."))
            
        except ValidationError as e:
            for msg in getattr(e, 'messages', [str(e)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except PermissionDenied as e:
            messages.error(self.request, str(e))
            return self.form_invalid(form)
        except Exception as e:
            logger.error(f"System error cancelling order {self.order.pk}: {str(e)}", exc_info=True)
            messages.error(self.request, _("An error occurred while processing your cancellation request."))
            return self.form_invalid(form)
            
        return redirect('orders:order_detail', id=self.order.pk)


class ReturnRequestView(LoginRequiredMixin, FormView):
    """
    Processes customer return requests for delivered orders.
    Delegates validation window checks and RMA generation to the service layer.
    """
    template_name = 'orders/return_request.html'
    form_class = ReturnRequestForm

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        order_id = self.kwargs.get('id')
        
        self.order = selectors.get_order_detail(order_id=order_id, user=self.request.user)
        if not self.order:
            raise Http404(_("Order not found or access denied."))
            
        kwargs['order'] = self.order
        return kwargs

    def form_valid(self, form: Any) -> HttpResponse:
        try:
            services.process_return_request(
                order=self.order,
                user=self.request.user,
                return_items_data=form.cleaned_data.get('return_items'),
                reason=form.cleaned_data.get('reason'),
                additional_details=form.cleaned_data.get('comments')
            )
            messages.success(self.request, _("Your return request has been submitted successfully."))
            
        except ValidationError as e:
            for msg in getattr(e, 'messages', [str(e)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as e:
            logger.error(f"Failed to process return request for order {self.order.pk}: {str(e)}", exc_info=True)
            messages.error(self.request, _("We could not process your return request at this time. Please contact support."))
            return self.form_invalid(form)
            
        return redirect('orders:order_detail', id=self.order.pk)


class RefundRequestView(LoginRequiredMixin, FormView):
    """
    Allows authenticated customers to formally request payment refunds
    based on eligible captures inside their orders.
    """
    template_name = 'orders/refund_request.html'
    form_class = OrderRefundRequestForm

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        order_id = self.kwargs.get('id')
        
        self.order = selectors.get_order_detail(order_id=order_id, user=self.request.user)
        if not self.order:
            raise Http404(_("Order not found or access denied."))
            
        kwargs['order'] = self.order
        return kwargs

    def form_valid(self, form: Any) -> HttpResponse:
        try:
            payment_id = form.cleaned_data.get('payment_id')
            amount = form.cleaned_data.get('amount')
            reason = form.cleaned_data.get('reason')

            # Look up payment mapping safely preventing lateral escalations
            payment = self.order.payments.get(id=payment_id)

            services.request_refund(
                order=self.order,
                payment=payment,
                amount=amount,
                reason=reason
            )
            messages.success(self.request, _("Your refund request has been successfully submitted and is under review."))

        except Payment.DoesNotExist:
            messages.error(self.request, _("Invalid or unauthorized payment transaction selected."))
            return self.form_invalid(form)
        except ValidationError as e:
            for msg in getattr(e, 'messages', [str(e)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as e:
            logger.error(f"Failed to process refund request for order {self.order.pk}: {str(e)}", exc_info=True)
            messages.error(self.request, _("We could not process your refund request at this time. Please contact support."))
            return self.form_invalid(form)

        return redirect('orders:order_detail', id=self.order.pk)


class ReorderView(LoginRequiredMixin, View):
    """
    Allows customers to duplicate a previous order's items directly into their active cart.
    Business logic and stock validation is securely managed by the service layer.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order_id = self.kwargs.get('id')
        order = selectors.get_order_detail(order_id=order_id, user=request.user)
        
        if not order:
            raise Http404(_("Order not found or access denied."))
            
        try:
            services.reorder_items_into_cart(
                order=order, 
                user=request.user,
                session_key=request.session.session_key
            )
            messages.success(request, _("Items from this order have been successfully added to your cart."))
            return redirect('cart:cart_detail')
            
        except ValidationError as e:
            for msg in getattr(e, 'messages', [str(e)]):
                messages.warning(request, msg)
            return redirect('orders:order_detail', id=order_id)
        except Exception as e:
            logger.error(f"Reorder failed for order {order_id}: {str(e)}", exc_info=True)
            messages.error(request, _("An error occurred while attempting to reorder these items."))
            return redirect('orders:order_detail', id=order_id)