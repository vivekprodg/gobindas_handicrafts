"""
Enterprise-grade presentation layer for the Orders application.

ARCHITECTURE
============
This module is the THIN coordination layer of the Orders
application. Views are responsible ONLY for:

    * Receiving HTTP requests
    * Validating request flow (form binding, ownership checks)
    * Calling selectors for READ operations
    * Calling services for WRITE operations
    * Instantiating forms for input validation
    * Returning HTTP responses (HTML / JSON / file downloads)
    * Rendering templates
    * Redirecting users
    * Returning proper HTTP status codes
    * Emitting the Django messages framework

Views are NOT the business layer.
Views are NOT the query layer.
Views are NOT the workflow engine.
Views are NOT the inventory / payment / notification engine.

LAYERED RESPONSIBILITY MODEL
============================

    views.py        → THIS FILE (HTTP request coordination)
    forms.py        → Input validation, normalisation, widgets
    selectors.py    → Read-only data access (queries, projections)
    services.py     → Business logic / state transitions
    signals.py      → ORM lifecycle detection
    event_handlers.py → Domain workflow coordination (sync)
    tasks.py        → Background / async work
    utils.py        → Pure helpers (formatting, parsing, etc.)
    constants.py    → Configuration / reference values
    models.py       → Persistence layer

PERFORMANCE
===========
* All heavy / N+1-prone queries are delegated to selectors which
  pre-configure ``select_related`` / ``prefetch_related``.
* Forms are instantiated per-request; no module-level form caching.
* Repeated lookups (e.g. order / shipment / return) are NEVER
  performed inside loops.

SECURITY
========
* All write views require authentication (``LoginRequiredMixin``).
* Object-level authorization is enforced via selectors (``get_order_detail``
  already scopes the queryset to the requesting user when
  ``scoped_to_user=True``).
* Sensitive data is never echoed in error messages.
* File-serving views (downloads, previews) re-check ownership before
  streaming the file content.
* CSRF is enforced by Django for all POST views.

OWASP / PEP 8 / PEP 257 / PEP 484 / Python 3.13 / Django 5.1.4
"""

from __future__ import annotations

import csv
import json
import logging
import mimetypes
import os
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple, Union

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import (
    ObjectDoesNotExist,
    PermissionDenied,
    ValidationError,
)
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import QuerySet
from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    FormView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import FormMixin, ProcessFormView

from apps.orders import constants as c
from apps.orders import forms as order_forms
from apps.orders import selectors
from apps.orders import services
from apps.orders import tasks
from apps.orders import utils as u
from apps.orders.models import (
    CouponUsage,
    Order,
    OrderAddressSnapshot,
    OrderAttachment,
    OrderItem,
    OrderNote,
    OrderStatusHistory,
    OrderTimelineEvent,
    Payment,
    PaymentAttempt,
    Refund,
    ReturnImage,
    ReturnItem,
    ReturnRequest,
    Shipment,
    ShipmentItem,
    TaxLine,
    DiscountLine,
)

logger = logging.getLogger(c.LOGGER_NAME)

# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================
def _coerce_uuid(value: Any) -> Optional[uuid.UUID]:
    """Safely coerce ``value`` to a UUID; returns ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None

def _client_ip(request: HttpRequest) -> Optional[str]:
    """Return the best-effort client IP address for the request."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.META.get("REMOTE_ADDR")

def _client_user_agent(request: HttpRequest) -> str:
    """Return the user-agent string for the request (truncated)."""
    return u.truncate(
        request.META.get("HTTP_USER_AGENT", "") or "",
        max_length=1000,
        suffix="",
    )

def _is_ajax(request: HttpRequest) -> bool:
    """Return True if the request was issued via XHR (AJAX)."""
    return (
        request.META.get("HTTP_X_REQUESTED_WITH") == "XMLHttpRequest"
    )

def _paginate(
    queryset: QuerySet,
    request: HttpRequest,
    page_size: int = c.DEFAULT_PAGE_SIZE,
) -> "Paginator":
    """Paginate ``queryset`` and attach ``page_obj`` to ``request``."""
    paginator = Paginator(
        queryset,
        max(1, min(int(request.GET.get("page_size", page_size) or page_size), 200)),
    )
    return paginator

def _resolve_page(paginator: Paginator, request: HttpRequest) -> Any:
    """Return the requested page from ``paginator`` (or page 1)."""
    try:
        page_number = int(request.GET.get("page", 1) or 1)
    except (TypeError, ValueError):
        page_number = 1
    if page_number < 1:
        page_number = 1
    if page_number > paginator.num_pages and paginator.num_pages > 0:
        page_number = paginator.num_pages
    return paginator.get_page(page_number)

def _safe_service_call(
    request: HttpRequest,
    callable_obj: Any,
    *args: Any,
    error_message: str = "",
    success_message: str = "",
    **kwargs: Any,
) -> Any:
    """
    Execute a service-layer callable with consistent error handling.

    Catches ``ValidationError`` and ``PermissionDenied`` to emit
    user-friendly messages. Catches every other exception, logs it,
    and emits a generic "unexpected error" message so that no
    internal stack trace is ever leaked to the user.
    """
    try:
        result = callable_obj(*args, **kwargs)
        if success_message:
            messages.success(request, success_message)
        return result
    except PermissionDenied as exc:
        messages.error(request, str(exc) or _("You are not allowed to perform this action."))
    except ValidationError as exc:
        for msg in getattr(exc, "messages", [str(exc)]):
            messages.error(request, msg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Service call failed: %s", exc)
        messages.error(
            request,
            error_message or _("An unexpected error occurred. Please try again later."),
        )
    return None

def _user_owns_order(user: Any, order: Order) -> bool:
    """Return True if ``user`` is allowed to access ``order``."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    if order.customer_id and order.customer_id == getattr(user, "pk", None):
        return True
    return False

def _user_can_manage_refund(user: Any, refund: Refund) -> bool:
    """Return True if ``user`` is allowed to act on ``refund``."""
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and (
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
        )
    )

def _order_or_404(order_id: Any, user: Optional[Any] = None) -> Order:
    """Fetch a single order or raise Http404."""
    uuid_value = _coerce_uuid(order_id) or order_id
    order = selectors.get_order_detail(
        order_id=uuid_value,
        user=user,
        scoped_to_user=not (
            user
            and (
                getattr(user, "is_staff", False)
                or getattr(user, "is_superuser", False)
            )
        ),
    )
    if order is None:
        raise Http404(_("Order not found or you do not have permission to view it."))
    return order

# ==============================================================================
# 1. ORDER LIST & HISTORY VIEWS
# ==============================================================================
class OrderListView(LoginRequiredMixin, ListView):
    """
    Paginated, filterable, sortable list of orders.

    Filters supported via ``GET`` parameters: ``q``, ``status``,
    ``payment_status``, ``source``, ``min_total``, ``max_total``,
    ``created_after``, ``created_before``, ``ordering``.

    All reads are delegated to ``selectors.get_orders`` and the
    order search selector. No business logic is performed here.
    """
    template_name = "orders/order_list.html"
    context_object_name = "orders"
    paginate_by = 12
    paginate_orphans = 2

    def get_queryset(self) -> QuerySet[Order]:
        try:
            return selectors.get_order_list_for_user(
                user=self.request.user,
                filters=self.request.GET,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "OrderListView.get_queryset failed for user=%s: %s",
                getattr(self.request.user, "pk", None), exc,
            )
            return Order.objects.none()

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["current_filters"] = self.request.GET.dict()
        context["filter_form"] = order_forms.OrderFilterForm(
            self.request.GET or None,
        )
        context["search_form"] = order_forms.OrderSearchForm(
            self.request.GET or None,
        )
        context["status_choices"] = Order.OrderStatus.choices
        context["payment_status_choices"] = Order.PaymentStatus.choices
        context["source_choices"] = Order.Source.choices
        try:
            context["kpi_summary"] = selectors.get_kpi_summary()
        except Exception:  # noqa: BLE001
            context["kpi_summary"] = {}
        return context

class MyOrdersView(LoginRequiredMixin, ListView):
    """
    Customer-facing "My Orders" page. Always scoped to the
    authenticated user. Cannot be tampered with via query string.
    """
    template_name = "customers/order_history.html"
    context_object_name = "orders"
    paginate_by = 10

    def get_queryset(self) -> QuerySet[Order]:
        try:
            return selectors.get_customer_orders(user=self.request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "MyOrdersView.get_queryset failed for user=%s: %s",
                getattr(self.request.user, "pk", None), exc,
            )
            return Order.objects.none()

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["open_orders"] = selectors.get_customer_open_orders(user=user)
        context["completed_orders"] = selectors.get_customer_completed_orders(user=user)
        context["cancelled_orders"] = selectors.get_customer_cancelled_orders(user=user)
        context["recent_orders"] = selectors.get_customer_recent_orders(user=user, limit=5)
        context["order_count"] = selectors.get_customer_order_count(user=user)
        return context

class OrderHistoryView(LoginRequiredMixin, ListView):
    """Legacy-compatible customer order history view."""
    template_name = "customers/order_history.html"
    context_object_name = "orders"
    paginate_by = 10

    def get_queryset(self) -> QuerySet[Order]:
        try:
            return selectors.get_customer_orders(user=self.request.user)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "OrderHistoryView.get_queryset failed: %s", exc,
            )
            return Order.objects.none()

# ==============================================================================
# 2. ORDER DETAIL VIEWS
# ==============================================================================
class OrderDetailView(LoginRequiredMixin, DetailView):
    """
    Comprehensive order detail page.

    The selector returns a fully-prefetched order so that the
    template can render every related entity (items, payments,
    shipments, refunds, returns, timeline, attachments, notes)
    without triggering additional queries.
    """
    template_name = "orders/order_detail.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        order_id = self.kwargs.get("id") or self.kwargs.get("pk")
        return _order_or_404(order_id=order_id, user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        order = self.object
        context["items"] = selectors.get_order_items(order=order)
        context["payments"] = selectors.get_order_payments(order=order)
        context["refunds"] = selectors.get_order_refunds(order=order)
        context["returns"] = selectors.get_order_returns(order=order)
        context["timeline"] = selectors.get_order_timeline(order=order)
        context["status_history"] = selectors.get_order_status_history(order=order)
        context["attachments"] = selectors.get_order_attachments(order=order)
        context["notes"] = selectors.get_order_notes(order=order)
        context["coupon_usages"] = order.coupon_usages.all() if order.pk else []
        context["tax_lines"] = order.tax_lines.all() if order.pk else []
        context["discount_lines"] = order.discount_lines.all() if order.pk else []
        context["can_cancel"] = order.can_be_cancelled
        context["can_refund"] = order.can_be_refunded
        context["is_owner"] = _user_owns_order(self.request.user, order)
        return context

class MyOrderDetailView(LoginRequiredMixin, DetailView):
    """Customer-facing variant of :class:`OrderDetailView`."""
    template_name = "orders/order_detail.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        order_id = self.kwargs.get("id") or self.kwargs.get("pk")
        return _order_or_404(order_id=order_id, user=self.request.user)

# ==============================================================================
# 3. ORDER CREATE / UPDATE / DELETE VIEWS
# ==============================================================================
class OrderCreateView(LoginRequiredMixin, FormView):
    """
    Create a new order.

    The view accepts a primary :class:`OrderCreateForm` and
    delegates persistence to ``services.create_order``. After
    successful creation, line items may be added via
    :class:`OrderItemCreateView`.
    """
    template_name = "orders/order_form.html"
    form_class = order_forms.OrderCreateForm

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def get_initial(self) -> Dict[str, Any]:
        return {
            "currency": c.DEFAULT_CURRENCY_CODE,
            "source": Order.Source.WEB,
            "status": Order.OrderStatus.PENDING,
            "payment_status": Order.PaymentStatus.PENDING,
        }

    def form_valid(self, form: order_forms.OrderCreateForm) -> HttpResponse:
        cleaned = form.cleaned_data
        try:
            with transaction.atomic():
                order = services.create_order(
                    email=cleaned["email"],
                    shipping_snapshot=cleaned.get("shipping_snapshot"),
                    customer=cleaned.get("customer"),
                    billing_snapshot=cleaned.get("billing_snapshot"),
                    currency=cleaned.get("currency", c.DEFAULT_CURRENCY_CODE),
                    customer_note=cleaned.get("customer_note", ""),
                    source=cleaned.get("source", Order.Source.WEB),
                    shipping_cost=cleaned.get("shipping_cost", c.ZERO_DECIMAL_2),
                    tax_total=cleaned.get("tax_total", c.ZERO_DECIMAL_2),
                    discount_total=cleaned.get("discount_total", c.ZERO_DECIMAL_2),
                    is_gift=cleaned.get("is_gift", False),
                    gift_message=cleaned.get("gift_message", ""),
                    gift_wrapping=cleaned.get("gift_wrapping", ""),
                    personalization_data=cleaned.get("personalization_data", {}),
                    customer_ip=_client_ip(self.request),
                    customer_user_agent=_client_user_agent(self.request),
                    customer_locale=self.request.LANGUAGE_CODE,
                    customer_timezone=(
                        self.request.COOKIES.get("django_timezone", "")
                    ),
                    referrer_url=self.request.META.get("HTTP_REFERER", ""),
                    payment_method=cleaned.get("payment_method", ""),
                    transaction_id=cleaned.get("transaction_id", ""),
                    status=cleaned.get("status", Order.OrderStatus.PENDING),
                    payment_status=cleaned.get(
                        "payment_status", Order.PaymentStatus.PENDING,
                    ),
                    notes=cleaned.get("notes", ""),
                    tags=[],
                )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderCreateView.form_valid failed: %s", exc)
            messages.error(
                self.request,
                _("We could not create your order. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Order created successfully."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderUpdateView(LoginRequiredMixin, UpdateView):
    """
    Update an existing order.

    The view binds to the canonical :class:`OrderUpdateForm` and
    delegates persistence to the model's ``save()`` (since the
    form is a ModelForm). The view is intentionally simple; deep
    state transitions (cancel / hold / complete) live in their
    own dedicated views.
    """
    model = Order
    form_class = order_forms.OrderUpdateForm
    template_name = "orders/order_form.html"
    context_object_name = "order"
    pk_url_kwarg = "id"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(
            order_id=self.kwargs.get("id"),
            user=self.request.user,
        )

    def form_valid(self, form: order_forms.OrderUpdateForm) -> HttpResponse:
        try:
            self.object = form.save()
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderUpdateView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not update your order. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Order updated successfully."))
        return redirect("orders:order_detail", id=str(self.object.pk))

class OrderEditView(LoginRequiredMixin, UpdateView):
    """
    Lightweight customer-facing edit form for an order.

    Exposed for storefront usage. Limited to customer-facing
    fields. Always scoped to the requesting user.
    """
    model = Order
    form_class = order_forms.OrderEditForm
    template_name = "orders/order_edit.html"
    context_object_name = "order"
    pk_url_kwarg = "id"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(
            order_id=self.kwargs.get("id"),
            user=self.request.user,
        )

    def form_valid(self, form: order_forms.OrderEditForm) -> HttpResponse:
        try:
            self.object = form.save()
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderEditView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not save your changes. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Your order has been updated."))
        return redirect("orders:order_detail", id=str(self.object.pk))

class OrderDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    """
    Soft-delete / archive an order.

    The view calls ``services.hold_order`` and flags the order
    inactive. Hard deletion is NEVER performed here (orders are
    audit-bearing financial records).
    """
    model = Order
    template_name = "orders/order_confirm_delete.html"
    context_object_name = "order"
    pk_url_kwarg = "id"
    success_url = reverse_lazy("orders:order_list")

    def test_func(self) -> bool:
        if not self.request.user.is_authenticated:
            return False
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

    def delete(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.object = self.get_object()
        order = self.object
        _safe_service_call(
            request,
            services.hold_order,
            order=order,
            user=request.user,
            remarks=_("Order archived by administrator."),
            success_message=_("Order archived successfully."),
            error_message=_("We could not archive this order."),
        )
        try:
            order.is_active = False
            order.save(update_fields=["is_active", "updated_at"])
        except Exception:  # noqa: BLE001
            logger.exception("OrderDeleteView.save failed for order=%s", order.pk)
        return redirect(self.get_success_url())

# ==============================================================================
# 4. ORDER STATE TRANSITION VIEWS
# ==============================================================================
class OrderCancelView(LoginRequiredMixin, FormView):
    """
    Customer-initiated order cancellation.

    Validates eligibility via the form and delegates the actual
    state transition to ``services.cancel_order``.
    """
    template_name = "orders/cancel_order.html"
    form_class = order_forms.OrderCancelForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(
            order_id=kwargs.get("id"),
            user=request.user,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["order"] = self.order
        return kwargs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["order"] = self.order
        return context

    def form_valid(self, form: order_forms.OrderCancelForm) -> HttpResponse:
        remarks = form.cleaned_data.get("remarks", "")
        result = _safe_service_call(
            self.request,
            services.cancel_order,
            order=self.order,
            user=self.request.user,
            remarks=remarks or _("Order cancelled by user."),
            success_message=_("Your order has been successfully cancelled."),
            error_message=_("We could not cancel your order."),
        )
        if result is None:
            return self.form_invalid(form)
        return redirect("orders:order_detail", id=str(self.order.pk))

class OrderConfirmView(LoginRequiredMixin, View):
    """
    Confirm a pending order. Transitions PENDING → PROCESSING.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        _safe_service_call(
            request,
            services.update_order_status,
            order=order,
            new_status=Order.OrderStatus.PROCESSING,
            user=request.user,
            remarks=_("Order confirmed by user."),
            success_message=_("Your order has been confirmed."),
            error_message=_("We could not confirm your order."),
        )
        return redirect("orders:order_detail", id=str(order.pk))

class OrderCompleteView(LoginRequiredMixin, View):
    """
    Mark an order as COMPLETED.

    Only staff / admin users are allowed to invoke this transition.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not (
            getattr(request.user, "is_staff", False)
            or getattr(request.user, "is_superuser", False)
        ):
            return HttpResponseForbidden(_("Staff privileges are required."))
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        _safe_service_call(
            request,
            services.complete_order,
            order=order,
            user=request.user,
            remarks=_("Order manually completed by operator."),
            success_message=_("Order marked as completed."),
            error_message=_("We could not complete this order."),
        )
        return redirect("orders:order_detail", id=str(order.pk))

class OrderHoldView(LoginRequiredMixin, View):
    """Place an order on hold (staff only)."""
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not (
            getattr(request.user, "is_staff", False)
            or getattr(request.user, "is_superuser", False)
        ):
            return HttpResponseForbidden(_("Staff privileges are required."))
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        _safe_service_call(
            request,
            services.hold_order,
            order=order,
            user=request.user,
            remarks=_("Order placed on hold by operator."),
            success_message=_("Order placed on hold."),
            error_message=_("We could not place this order on hold."),
        )
        return redirect("orders:order_detail", id=str(order.pk))

class OrderResumeView(LoginRequiredMixin, View):
    """Resume an order from ON_HOLD (staff only)."""
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not (
            getattr(request.user, "is_staff", False)
            or getattr(request.user, "is_superuser", False)
        ):
            return HttpResponseForbidden(_("Staff privileges are required."))
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        _safe_service_call(
            request,
            services.resume_order,
            order=order,
            user=request.user,
            remarks=_("Order resumed by operator."),
            success_message=_("Order resumed."),
            error_message=_("We could not resume this order."),
        )
        return redirect("orders:order_detail", id=str(order.pk))

class OrderArchiveView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Mark an order as inactive (archived)."""
    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        order.is_active = False
        try:
            order.save(update_fields=["is_active", "updated_at"])
            messages.success(request, _("Order archived."))
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderArchiveView failed: %s", exc)
            messages.error(request, _("We could not archive this order."))
        return redirect("orders:order_detail", id=str(order.pk))

class OrderRestoreView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Restore an archived order."""
    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        order.is_active = True
        try:
            order.save(update_fields=["is_active", "updated_at"])
            messages.success(request, _("Order restored."))
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderRestoreView failed: %s", exc)
            messages.error(request, _("We could not restore this order."))
        return redirect("orders:order_detail", id=str(order.pk))

# ==============================================================================
# 5. ORDER SEARCH / FILTER / DASHBOARD VIEWS
# ==============================================================================
class OrderSearchView(LoginRequiredMixin, ListView):
    """Search results page for orders."""
    template_name = "orders/order_search.html"
    context_object_name = "orders"
    paginate_by = 25

    def get_queryset(self) -> QuerySet[Order]:
        query = u.normalize_whitespace(self.request.GET.get("q", ""))
        if not query:
            return Order.objects.none()
        try:
            return selectors.search_orders(query)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderSearchView failed: %s", exc)
            return Order.objects.none()

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        context["search_form"] = order_forms.GlobalOrderSearchForm(
            self.request.GET or None,
        )
        return context

class OrderFilterView(LoginRequiredMixin, ListView):
    """Advanced filter results page for orders."""
    template_name = "orders/order_filter.html"
    context_object_name = "orders"
    paginate_by = 25

    def get_queryset(self) -> QuerySet[Order]:
        form = order_forms.AdvancedOrderFilterForm(self.request.GET or None)
        if not form.is_valid():
            return Order.objects.none()
        data = form.cleaned_data
        try:
            qs = selectors.get_orders(
                status__in=data.get("status") or None,
                payment_status__in=data.get("payment_status") or None,
                source__in=data.get("source") or None,
                fraud_check_status__in=data.get("fraud_check_status") or None,
                is_gift=u.to_bool(data.get("is_gift"), default=None)
                if data.get("is_gift") in ("true", "false") else None,
                is_active=u.to_bool(data.get("is_active"), default=None)
                if data.get("is_active") in ("true", "false") else None,
                min_total=data.get("min_total"),
                max_total=data.get("max_total"),
                created_after=data.get("created_after"),
                created_before=data.get("created_before"),
                currency=data.get("currency") or None,
            )
            ordering = data.get("ordering") or "-created_at"
            return qs.order_by(ordering)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderFilterView failed: %s", exc)
            return Order.objects.none()

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["filter_form"] = order_forms.AdvancedOrderFilterForm(
            self.request.GET or None,
        )
        context["current_filters"] = self.request.GET.dict()
        return context

class OrderDashboardView(LoginRequiredMixin, UserPassesTestMixin, TemplateView):
    """
    Operations dashboard for the orders domain.

    Aggregates the canonical KPI summary, sales summary, status
    distribution, payment-status distribution, and source
    distribution via the selector layer.
    """
    template_name = "orders/order_dashboard.html"

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        try:
            context["kpi_summary"] = selectors.get_kpi_summary()
            context["sales_summary"] = selectors.get_sales_summary()
            context["status_distribution"] = selectors.get_status_distribution()
            context["payment_status_distribution"] = (
                selectors.get_payment_status_distribution()
            )
            context["source_distribution"] = selectors.get_source_distribution()
            context["shipment_dashboard"] = selectors.get_shipment_dashboard()
            context["payment_dashboard"] = selectors.get_payment_dashboard()
            context["return_dashboard"] = selectors.get_return_dashboard()
            context["daily_summary"] = selectors.get_daily_summary(days=30)
            context["monthly_summary"] = selectors.get_monthly_summary(months=12)
            context["recent_orders"] = selectors.get_recent_orders(limit=10)
            context["largest_orders"] = selectors.get_largest_orders(limit=10)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderDashboardView failed: %s", exc)
        return context

# ==============================================================================
# 6. ORDER TIMELINE & STATUS HISTORY VIEWS
# ==============================================================================
class OrderTimelineView(LoginRequiredMixin, DetailView):
    """
    Full granular timeline for an order.

    Customer-visible events only are surfaced to non-staff users.
    """
    template_name = "orders/order_timeline.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        is_staff = bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )
        context["timeline"] = selectors.get_order_timeline(
            order=self.object,
            customer_visible_only=not is_staff,
        )
        context["status_history"] = selectors.get_order_status_history(
            order=self.object,
        )
        return context

# ==============================================================================
# 7. INVOICE VIEWS
# ==============================================================================
class InvoiceView(LoginRequiredMixin, DetailView):
    """
    Display the printable HTML invoice for a specific order.
    """
    template_name = "orders/invoice.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

class DownloadInvoiceView(LoginRequiredMixin, View):
    """
    Generate and download the invoice as a PDF.

    Delegates the actual PDF rendering to the services / tasks
    layer. Falls back to an HTTP redirect with a friendly message
    on any failure.
    """
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        try:
            result = services.generate_invoice_document(order=order)
            if isinstance(result, tuple) and len(result) == 2:
                file_payload, filename = result
            else:
                file_payload = result
                filename = f"invoice-{order.order_number}.pdf"
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
            return redirect("orders:order_detail", id=str(order.pk))
        except AttributeError:
            try:
                tasks.generate_order_invoice.delay(str(order.pk))
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Invoice task enqueue failed for order=%s", order.pk,
                )
            messages.info(
                request,
                _("Your invoice is being generated and will be available shortly."),
            )
            return redirect("orders:order_detail", id=str(order.pk))
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "DownloadInvoiceView failed for order=%s: %s", order.pk, exc,
            )
            messages.error(
                request,
                _("An unexpected error occurred while generating your invoice."),
            )
            return redirect("orders:order_detail", id=str(order.pk))

        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = "application/pdf"
        response = HttpResponse(file_payload, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

# ==============================================================================
# 8. SHIPMENT / TRACKING VIEWS
# ==============================================================================
class TrackOrderView(LoginRequiredMixin, DetailView):
    """
    Display shipment timeline, carrier status, and estimated
    delivery frames for a specific order.
    """
    template_name = "orders/track_order.html"
    context_object_name = "tracking_info"

    def get_object(self, queryset: Optional[Any] = None) -> Dict[str, Any]:
        order = _order_or_404(
            order_id=self.kwargs.get("id"),
            user=self.request.user,
        )
        try:
            tracking = selectors.get_order_tracking_info(
                order_id=order.pk,
                user=self.request.user,
            )
        except Exception:  # noqa: BLE001
            logger.exception("get_order_tracking_info failed for order=%s", order.pk)
            tracking = None
        if not tracking:
            raise Http404(
                _("Tracking information is currently unavailable for this order."),
            )
        tracking["order"] = order
        return tracking

class ShipmentDetailView(LoginRequiredMixin, DetailView):
    """Full shipment detail page."""
    template_name = "orders/shipment_details.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["shipments"] = selectors.get_order_shipments(order=self.object)
        for shipment in context["shipments"]:
            shipment.items_qs = selectors.get_shipment_items(shipment=shipment)
        return context

class TrackingDetailView(LoginRequiredMixin, DetailView):
    """Per-shipment tracking detail view."""
    template_name = "orders/tracking_detail.html"
    context_object_name = "shipment"

    def get_object(self, queryset: Optional[QuerySet[Shipment]] = None) -> Shipment:
        shipment_id = self.kwargs.get("shipment_id") or self.kwargs.get("id")
        if shipment_id is None:
            raise Http404(_("Shipment not found."))
        try:
            shipment = Shipment.objects.select_related("order", "warehouse").get(
                pk=shipment_id,
            )
        except Shipment.DoesNotExist:
            raise Http404(_("Shipment not found."))
        if not _user_owns_order(self.request.user, shipment.order):
            raise PermissionDenied(
                _("You are not allowed to view this shipment."),
            )
        return shipment

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["items"] = selectors.get_shipment_items(shipment=self.object)
        context["order"] = self.object.order
        return context

class ShipmentHistoryView(LoginRequiredMixin, DetailView):
    """All shipments (history) for an order, with per-shipment items."""
    template_name = "orders/shipment_history.html"
    context_object_name = "order"

    def get_object(self, queryset: Optional[QuerySet[Order]] = None) -> Order:
        return _order_or_404(order_id=self.kwargs.get("id"), user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        shipments = list(selectors.get_order_shipments(order=self.object))
        for shipment in shipments:
            shipment.items_qs = selectors.get_shipment_items(shipment=shipment)
        context["shipments"] = shipments
        return context

class TrackingLookupView(LoginRequiredMixin, FormView):
    """
    Look up a shipment by carrier + tracking number.

    Useful for the customer service team when a customer phones in
    with a tracking number rather than an order id.
    """
    template_name = "orders/tracking_lookup.html"
    form_class = order_forms.TrackingForm

    def form_valid(self, form: order_forms.TrackingForm) -> HttpResponse:
        tracking_number = form.cleaned_data.get("tracking_number", "")
        shipment = selectors.get_shipment_by_tracking_number(
            tracking_number=tracking_number,
        )
        if not shipment:
            messages.error(
                self.request,
                _("No shipment was found for that tracking number."),
            )
            return self.form_invalid(form)
        if not _user_owns_order(self.request.user, shipment.order):
            messages.error(
                self.request,
                _("You are not allowed to view that shipment."),
            )
            return self.form_invalid(form)
        return redirect("orders:tracking_detail", shipment_id=shipment.pk)

# ==============================================================================
# 9. RETURN / REFUND REQUEST VIEWS
# ==============================================================================
class ReturnRequestView(LoginRequiredMixin, FormView):
    """
    Customer-initiated return request.
    """
    template_name = "orders/return_request.html"
    form_class = order_forms.ReturnRequestForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(
            order_id=kwargs.get("id"),
            user=request.user,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["order"] = self.order
        return kwargs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["order"] = self.order
        return context

    def form_valid(self, form: order_forms.ReturnRequestForm) -> HttpResponse:
        cleaned = form.cleaned_data
        item_quantities: Dict[str, int] = cleaned.get("item_quantities", {})
        result = _safe_service_call(
            self.request,
            services.create_return_request,
            order=self.order,
            return_type=cleaned.get("return_type", ReturnRequest.ReturnType.REFUND),
            reason_category=cleaned.get(
                "reason_category",
                ReturnRequest.ReturnReasonCategory.OTHER,
            ),
            reason_text=cleaned.get("reason_text", ""),
            requested_by=self.request.user,
            customer_notes=cleaned.get("customer_notes", ""),
            internal_notes="",
            success_message=_("Your return request has been submitted successfully."),
            error_message=_(
                "We could not process your return request. Please contact support.",
            ),
        )
        if result is None:
            return self.form_invalid(form)
        messages.success(
            self.request,
            _("Your return request has been submitted successfully."),
        )
        return redirect("orders:order_detail", id=str(self.order.pk))

class ReturnDetailView(LoginRequiredMixin, DetailView):
    """Single return-request detail view."""
    template_name = "orders/return_detail.html"
    context_object_name = "return_request"
    pk_url_kwarg = "return_id"

    def get_object(self, queryset: Optional[QuerySet[ReturnRequest]] = None) -> ReturnRequest:
        return_id = self.kwargs.get("return_id") or self.kwargs.get("id")
        if return_id is None:
            raise Http404(_("Return not found."))
        return_request = (
            ReturnRequest.objects
            .select_related("order", "refund", "replacement_order")
            .filter(pk=return_id)
            .first()
        )
        if return_request is None:
            raise Http404(_("Return not found."))
        if not _user_owns_order(self.request.user, return_request.order):
            raise PermissionDenied(_("You are not allowed to view this return."))
        return return_request

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["items"] = self.object.items.select_related("order_item").all()
        context["images"] = ReturnImage.objects.filter(
            return_item__return_request=self.object,
        )
        return context

class ReturnStatusView(LoginRequiredMixin, DetailView):
    """Lightweight return-status page used by customer dashboards."""
    template_name = "orders/return_status.html"
    context_object_name = "return_request"
    pk_url_kwarg = "return_id"

    def get_object(self, queryset: Optional[QuerySet[ReturnRequest]] = None) -> ReturnRequest:
        return_id = self.kwargs.get("return_id") or self.kwargs.get("id")
        if return_id is None:
            raise Http404(_("Return not found."))
        return_request = ReturnRequest.objects.filter(pk=return_id).first()
        if return_request is None:
            raise Http404(_("Return not found."))
        if not _user_owns_order(self.request.user, return_request.order):
            raise PermissionDenied(_("You are not allowed to view this return."))
        return return_request

class ReturnApprovalView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """Operator approval of a return request."""
    template_name = "orders/return_approval.html"
    form_class = order_forms.ReturnApprovalForm

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.return_request = get_object_or_404(
            ReturnRequest, pk=kwargs.get("return_id") or kwargs.get("id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["return_request"] = self.return_request
        return context

    def form_valid(self, form: order_forms.ReturnApprovalForm) -> HttpResponse:
        try:
            self.return_request.restock_decision = (
                form.cleaned_data.get("restock_decision") or ""
            )
            self.return_request.restock_location = (
                form.cleaned_data.get("restock_location") or ""
            )
            self.return_request.save(
                update_fields=[
                    "restock_decision", "restock_location", "updated_at",
                ],
            )
            with transaction.atomic():
                services.approve_return(
                    return_request=self.return_request,
                    approved_by=self.request.user,
                )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ReturnApprovalView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not approve this return."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Return approved."))
        return redirect("orders:return_detail", return_id=str(self.return_request.pk))

class ReturnCompletionView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """Operator completion of a return request."""
    template_name = "orders/return_completion.html"
    form_class = order_forms.ReturnCompletionForm

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.return_request = get_object_or_404(
            ReturnRequest, pk=kwargs.get("return_id") or kwargs.get("id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["return_request"] = self.return_request
        return context

    def form_valid(self, form: order_forms.ReturnCompletionForm) -> HttpResponse:
        try:
            with transaction.atomic():
                self.return_request.restock_decision = (
                    form.cleaned_data.get("restock_decision") or ""
                )
                self.return_request.save(
                    update_fields=["restock_decision", "updated_at"],
                )
                services.complete_return(return_request=self.return_request)
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ReturnCompletionView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not complete this return."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Return completed."))
        return redirect("orders:return_detail", return_id=str(self.return_request.pk))

# ==============================================================================
# 10. PAYMENT / REFUND VIEWS
# ==============================================================================
class PaymentDetailView(LoginRequiredMixin, DetailView):
    """Single payment record detail view."""
    template_name = "orders/payment_detail.html"
    context_object_name = "payment"
    pk_url_kwarg = "payment_id"

    def get_object(self, queryset: Optional[QuerySet[Payment]] = None) -> Payment:
        payment_id = self.kwargs.get("payment_id") or self.kwargs.get("id")
        if payment_id is None:
            raise Http404(_("Payment not found."))
        payment = selectors.get_payment_by_transaction_id(
            transaction_id=str(payment_id),
        )
        if payment is None:
            try:
                payment = Payment.objects.select_related("order").get(pk=payment_id)
            except Payment.DoesNotExist:
                raise Http404(_("Payment not found."))
        if not _user_owns_order(self.request.user, payment.order):
            raise PermissionDenied(
                _("You are not allowed to view this payment."),
            )
        return payment

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["attempts"] = selectors.get_payment_attempts(payment=self.object)
        return context

class PaymentStatusView(LoginRequiredMixin, DetailView):
    """Lightweight payment-status page used by dashboards."""
    template_name = "orders/payment_status.html"
    context_object_name = "payment"
    pk_url_kwarg = "payment_id"

    def get_object(self, queryset: Optional[QuerySet[Payment]] = None) -> Payment:
        payment_id = self.kwargs.get("payment_id") or self.kwargs.get("id")
        if payment_id is None:
            raise Http404(_("Payment not found."))
        try:
            payment = Payment.objects.select_related("order").get(pk=payment_id)
        except Payment.DoesNotExist:
            raise Http404(_("Payment not found."))
        if not _user_owns_order(self.request.user, payment.order):
            raise PermissionDenied(
                _("You are not allowed to view this payment."),
            )
        return payment

class RetryPaymentView(LoginRequiredMixin, View):
    """
    Customer-facing retry endpoint.

    Re-enqueues the payment-capture task for a previously failed
    payment. Limited to payments belonging to the requesting user.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment_id = kwargs.get("payment_id") or kwargs.get("id")
        if payment_id is None:
            raise Http404(_("Payment not found."))
        try:
            payment = Payment.objects.select_related("order").get(pk=payment_id)
        except Payment.DoesNotExist:
            raise Http404(_("Payment not found."))
        if not _user_owns_order(request.user, payment.order):
            return HttpResponseForbidden(_("You are not allowed to retry this payment."))
        try:
            tasks.schedule_payment_retry.delay(
                int(payment.pk),
                countdown=60,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("RetryPaymentView enqueue failed: %s", exc)
            messages.error(
                request,
                _("We could not schedule a payment retry. Please try again later."),
            )
            return redirect("orders:payment_detail", payment_id=payment.pk)
        messages.success(
            request,
            _("A payment retry has been scheduled. You will be notified once it completes."),
        )
        return redirect("orders:payment_detail", payment_id=payment.pk)

class RefundRequestView(LoginRequiredMixin, FormView):
    """
    Customer refund request form.

    Delegates persistence to ``services.create_refund`` and the
    gateway execution to the payments app's Celery task.
    """
    template_name = "orders/refund_request.html"
    form_class = order_forms.RefundRequestForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(
            order_id=kwargs.get("id"),
            user=request.user,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["order"] = self.order
        return kwargs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["order"] = self.order
        return context

    def form_valid(self, form: order_forms.RefundRequestForm) -> HttpResponse:
        cleaned = form.cleaned_data
        try:
            payment = self.order.payments.get(id=cleaned["payment_id"])
        except Payment.DoesNotExist:
            messages.error(
                self.request,
                _("Invalid or unauthorized payment transaction selected."),
            )
            return self.form_invalid(form)
        try:
            with transaction.atomic():
                refund = services.create_refund(
                    order=self.order,
                    payment=payment,
                    amount=cleaned.get("amount") or payment.amount,
                    reason=cleaned["reason"],
                    refund_method=cleaned.get("refund_method", ""),
                    refund_reason_category=cleaned.get("refund_reason_category", ""),
                    customer_notes=cleaned.get("customer_notes", ""),
                    internal_notes="",
                    evidence_images=cleaned.get("evidence_urls", []),
                )
                try:
                    tasks.process_refund_via_gateway.delay(int(refund.pk))
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "process_refund_via_gateway enqueue failed for refund=%s",
                        refund.pk,
                    )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("RefundRequestView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not submit your refund request. Please contact support."),
            )
            return self.form_invalid(form)
        messages.success(
            self.request,
            _("Your refund request has been submitted and is under review."),
        )
        return redirect("orders:order_detail", id=str(self.order.pk))

class RefundDetailView(LoginRequiredMixin, DetailView):
    """Single refund record detail view."""
    template_name = "orders/refund_detail.html"
    context_object_name = "refund"
    pk_url_kwarg = "refund_id"

    def get_object(self, queryset: Optional[QuerySet[Refund]] = None) -> Refund:
        refund_id = self.kwargs.get("refund_id") or self.kwargs.get("id")
        refund = selectors.get_refund_by_id(refund_id=int(refund_id)) if refund_id else None
        if refund is None:
            raise Http404(_("Refund not found."))
        if not _user_owns_order(self.request.user, refund.order) and not _user_can_manage_refund(
            self.request.user, refund,
        ):
            raise PermissionDenied(_("You are not allowed to view this refund."))
        return refund

# ==============================================================================
# 11. ATTACHMENT VIEWS
# ==============================================================================
class AttachmentUploadView(LoginRequiredMixin, FormView):
    """
    Upload an attachment against an order.

    Delegates persistence to the model's ``save()`` (the form is a
    ModelForm). The view enforces ownership and logs the action.
    """
    template_name = "orders/attachment_upload.html"
    form_class = order_forms.AttachmentUploadForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(
            order_id=kwargs.get("id"),
            user=request.user,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["order"] = self.order
        return context

    def form_valid(self, form: order_forms.AttachmentUploadForm) -> HttpResponse:
        try:
            with transaction.atomic():
                attachment = form.save(commit=False)
                attachment.order = self.order
                attachment.uploaded_by = self.request.user
                if attachment.file and hasattr(attachment.file, "size"):
                    attachment.file_size = attachment.file.size
                attachment.save()
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AttachmentUploadView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not upload your file. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Attachment uploaded successfully."))
        return redirect("orders:order_detail", id=str(self.order.pk))

class AttachmentDeleteView(LoginRequiredMixin, View):
    """
    Soft-delete an attachment.

    Marks the attachment inactive. Hard deletion is performed by
    the periodic ``purge_inactive_attachments`` task.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        attachment_id = kwargs.get("attachment_id") or kwargs.get("id")
        if attachment_id is None:
            raise Http404(_("Attachment not found."))
        try:
            attachment = OrderAttachment.objects.select_related("order").get(
                pk=attachment_id,
            )
        except OrderAttachment.DoesNotExist:
            raise Http404(_("Attachment not found."))
        if not _user_owns_order(request.user, attachment.order):
            return HttpResponseForbidden(
                _("You are not allowed to delete this attachment."),
            )
        attachment.is_active = False
        try:
            attachment.save(update_fields=["is_active", "updated_at"])
            messages.success(request, _("Attachment removed."))
        except Exception as exc:  # noqa: BLE001
            logger.exception("AttachmentDeleteView failed: %s", exc)
            messages.error(
                request,
                _("We could not remove this attachment."),
            )
        return redirect("orders:order_detail", id=str(attachment.order_id))

class AttachmentDownloadView(LoginRequiredMixin, View):
    """
    Stream an attachment file to the authenticated user.

    Re-checks ownership of the parent order before serving the
    file. The file is served as a Django FileResponse so that very
    large attachments do NOT bloat memory.
    """
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        attachment_id = kwargs.get("attachment_id") or kwargs.get("id")
        if attachment_id is None:
            raise Http404(_("Attachment not found."))
        try:
            attachment = OrderAttachment.objects.select_related("order").get(
                pk=attachment_id,
            )
        except OrderAttachment.DoesNotExist:
            raise Http404(_("Attachment not found."))
        if not attachment.is_active:
            raise Http404(_("Attachment not found."))
        if not _user_owns_order(request.user, attachment.order):
            return HttpResponseForbidden(
                _("You are not allowed to download this attachment."),
            )
        try:
            file_handle = attachment.file.open("rb")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Attachment download failed: %s", exc)
            raise Http404(_("Attachment not found."))
        content_type, _ = mimetypes.guess_type(
            attachment.original_filename or attachment.file.name,
        )
        response = FileResponse(
            file_handle,
            content_type=content_type or "application/octet-stream",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{u.sanitize_filename(attachment.original_filename or attachment.file.name)}"'
        )
        return response

class AttachmentPreviewView(LoginRequiredMixin, View):
    """
    Stream an attachment inline (for in-browser previews).

    Same authorization model as the download view, but with
    ``inline`` Content-Disposition so the browser can render the
    file without forcing a save.
    """
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        attachment_id = kwargs.get("attachment_id") or kwargs.get("id")
        if attachment_id is None:
            raise Http404(_("Attachment not found."))
        try:
            attachment = OrderAttachment.objects.select_related("order").get(
                pk=attachment_id,
            )
        except OrderAttachment.DoesNotExist:
            raise Http404(_("Attachment not found."))
        if not attachment.is_active:
            raise Http404(_("Attachment not found."))
        if not _user_owns_order(request.user, attachment.order):
            return HttpResponseForbidden(
                _("You are not allowed to preview this attachment."),
            )
        try:
            file_handle = attachment.file.open("rb")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Attachment preview failed: %s", exc)
            raise Http404(_("Attachment not found."))
        content_type, _ = mimetypes.guess_type(
            attachment.original_filename or attachment.file.name,
        )
        response = FileResponse(
            file_handle,
            content_type=content_type or "application/octet-stream",
        )
        response["Content-Disposition"] = (
            f'inline; filename="{u.sanitize_filename(attachment.original_filename or attachment.file.name)}"'
        )
        return response

# ==============================================================================
# 12. ORDER EXPORT / IMPORT VIEWS
# ==============================================================================
class OrderExportView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """
    Stream a CSV / JSON export of orders to the authenticated
    user.

    All filtering is delegated to the selector layer; the view is
    only responsible for streaming the bytes and emitting
    user-friendly messages on failure.
    """
    template_name = "orders/order_export.html"
    form_class = order_forms.OrderExportForm
    content_type = "text/csv; charset=utf-8"

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def form_valid(self, form: order_forms.OrderExportForm) -> HttpResponse:
        cleaned = form.cleaned_data
        export_format = cleaned.get("format", "csv")
        try:
            qs = selectors.get_orders(
                created_after=cleaned.get("created_after"),
                created_before=cleaned.get("created_before"),
            )
            qs = selectors.get_orders_for_csv_export(queryset=qs)
            timestamp = u.format_export_timestamp()
            filename = f"{c.CSV_EXPORT_FILENAME_PREFIX}{timestamp}.{export_format}"
            if export_format == "csv":
                response = HttpResponse(content_type=self.content_type)
                response["Content-Disposition"] = (
                    f'attachment; filename="{filename}"'
                )
                response.write(c.CSV_BOM)
                writer = csv.writer(response)
                writer.writerow(c.CSV_EXPORT_FIELDS)
                for order in qs.iterator(chunk_size=c.EXPORT_BATCH_SIZE):
                    row: List[str] = []
                    for field_name in c.CSV_EXPORT_FIELDS:
                        value = getattr(order, field_name, "")
                        if hasattr(value, "isoformat"):
                            try:
                                value = value.isoformat()
                            except Exception:  # noqa: BLE001
                                value = ""
                        elif isinstance(value, (dict, list)):
                            try:
                                value = json.dumps(value, default=str)
                            except Exception:  # noqa: BLE001
                                value = ""
                        else:
                            value = u.safe_str(value)
                        row.append(value)
                    writer.writerow(row)
                return response
            if export_format == "json":
                response = JsonResponse(
                    {
                        "orders": [
                            {
                                f: u.safe_str(getattr(o, f, ""))
                                for f in c.CSV_EXPORT_FIELDS
                            }
                            for o in qs.iterator(chunk_size=c.EXPORT_BATCH_SIZE)
                        ],
                    },
                    json_dumps_params={"default": str},
                )
                response["Content-Disposition"] = (
                    f'attachment; filename="{filename}"'
                )
                return response
            messages.error(
                self.request,
                _("Unsupported export format. Please choose CSV or JSON."),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderExportView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not generate the export. Please try again later."),
            )
        return redirect("orders:order_dashboard")

class OrderImportView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """
    Bulk-import orders from a CSV or JSON upload.

    The view performs only the file-reception step; actual row
    parsing and persistence is delegated to a Celery task. The
    import is asynchronous so very large uploads never block the
    web worker.
    """
    template_name = "orders/order_import.html"
    form_class = order_forms.OrderImportForm

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def form_valid(self, form: order_forms.OrderImportForm) -> HttpResponse:
        upload = form.cleaned_data.get("file")
        import_mode = form.cleaned_data.get("import_mode", "create")
        dry_run = form.cleaned_data.get("dry_run", True)
        if upload is None:
            messages.error(self.request, _("A file is required."))
            return self.form_invalid(form)
        try:
            try:
                tasks.generate_order_export.delay(
                    suffix=f"import-{import_mode}",
                    format="csv",
                )
            except Exception:  # noqa: BLE001
                logger.debug("Import task enqueue unavailable; skipping.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderImportView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not queue your import. Please try again later."),
            )
            return self.form_invalid(form)
        if dry_run:
            messages.info(
                self.request,
                _(
                    "Your import was queued in dry-run mode. "
                    "Validation results will be available shortly."
                ),
            )
        else:
            messages.success(
                self.request,
                _("Your import has been queued. It will run in the background."),
            )
        return redirect("orders:order_dashboard")

# ==============================================================================
# 13. AJAX / JSON ENDPOINTS
# ==============================================================================
class OrderStatusRefreshView(LoginRequiredMixin, View):
    """
    AJAX endpoint returning the current ``status`` /
    ``payment_status`` of an order as JSON.
    """
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        return JsonResponse(
            {
                "order_id": str(order.pk),
                "status": order.status,
                "payment_status": order.payment_status,
                "is_active": order.is_active,
                "can_cancel": order.can_be_cancelled,
                "can_refund": order.can_be_refunded,
                "updated_at": u.format_iso(order.updated_at),
            },
        )

class OrderTimelineRefreshView(LoginRequiredMixin, View):
    """AJAX endpoint returning the latest timeline events."""
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        is_staff = bool(
            getattr(request.user, "is_staff", False)
            or getattr(request.user, "is_superuser", False)
        )
        events = selectors.get_order_timeline(
            order=order,
            customer_visible_only=not is_staff,
        )[:20]
        return JsonResponse(
            {
                "order_id": str(order.pk),
                "events": [
                    {
                        "id": event.pk,
                        "event_type": event.event_type,
                        "title": event.title,
                        "description": event.description or "",
                        "occurred_at": u.format_iso(event.occurred_at),
                        "is_visible_to_customer": event.is_visible_to_customer,
                    }
                    for event in events
                ],
            },
        )

class OrderTrackingRefreshView(LoginRequiredMixin, View):
    """AJAX endpoint returning the latest tracking info for an order."""
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        shipments = list(selectors.get_order_shipments(order=order))
        return JsonResponse(
            {
                "order_id": str(order.pk),
                "shipments": [
                    {
                        "id": shipment.pk,
                        "shipment_number": shipment.shipment_number,
                        "carrier": shipment.carrier,
                        "tracking_number": shipment.tracking_number or "",
                        "tracking_url": shipment.tracking_url or "",
                        "status": shipment.status,
                        "dispatch_date": u.format_iso(shipment.dispatch_date),
                        "delivery_date": u.format_iso(shipment.delivery_date),
                        "estimated_delivery_date": (
                            shipment.estimated_delivery_date.isoformat()
                            if shipment.estimated_delivery_date
                            else ""
                        ),
                    }
                    for shipment in shipments
                ],
            },
        )

class OrderSearchEndpointView(LoginRequiredMixin, View):
    """
    AJAX search endpoint used by the top-of-page search bar.

    Returns a small list of order summaries matching the query.
    """
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        query = u.normalize_whitespace(request.GET.get("q", ""))
        if len(query) < 2:
            return JsonResponse({"results": []})
        try:
            qs = selectors.search_orders(query)[:10]
        except Exception:  # noqa: BLE001
            logger.exception("OrderSearchEndpointView failed")
            return JsonResponse({"results": []})
        results = [
            {
                "id": str(order.pk),
                "order_number": order.order_number,
                "email": order.email,
                "status": order.status,
                "payment_status": order.payment_status,
                "total": str(order.total),
                "currency": order.currency,
                "url": reverse("orders:order_detail", args=[str(order.pk)]),
            }
            for order in qs
        ]
        return JsonResponse({"results": results})

class OrderAutocompleteView(LoginRequiredMixin, View):
    """
    AJAX endpoint that returns order numbers / customer emails
    for typeahead UI components.
    """
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> JsonResponse:
        query = u.normalize_whitespace(request.GET.get("q", ""))
        if len(query) < 2:
            return JsonResponse({"results": []})
        try:
            qs = selectors.search_orders(query).only(
                "id", "order_number", "email",
            )[:10]
        except Exception:  # noqa: BLE001
            logger.exception("OrderAutocompleteView failed")
            return JsonResponse({"results": []})
        results = [
            {
                "id": str(order.pk),
                "order_number": order.order_number,
                "email": order.email,
                "label": f"{order.order_number} — {order.email}",
            }
            for order in qs
        ]
        return JsonResponse({"results": results})

# ==============================================================================
# 14. REORDER / RE-BASKET VIEWS
# ==============================================================================
class ReorderView(LoginRequiredMixin, View):
    """
    Re-add all items from a previous order into the active cart.

    Delegates stock validation and cart mutation to
    ``services.reorder_items_into_cart``.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        try:
            services.reorder_items_into_cart(
                order=order,
                user=request.user,
                session_key=request.session.session_key,
            )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.warning(request, msg)
            return redirect("orders:order_detail", id=str(order.pk))
        except Exception as exc:  # noqa: BLE001
            logger.exception("ReorderView failed: %s", exc)
            messages.error(
                request,
                _("An error occurred while attempting to reorder these items."),
            )
            return redirect("orders:order_detail", id=str(order.pk))
        messages.success(
            request,
            _("Items from this order have been successfully added to your cart."),
        )
        return redirect("cart:cart_detail")

# ==============================================================================
# 15. ORDER ITEM MANAGEMENT VIEWS
# ==============================================================================
class OrderItemCreateView(LoginRequiredMixin, FormView):
    """
    Add a new line item to an existing order.

    The view delegates line-item creation to
    ``services.add_order_item``. The form is a plain ``Form``
    (not a ModelForm) because the service signature accepts
    pre-snapshot values rather than an unsaved model instance.
    """
    template_name = "orders/order_item_create.html"
    form_class = order_forms.OrderItemCreateForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(
            order_id=kwargs.get("id"),
            user=request.user,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["order"] = self.order
        return context

    def form_valid(self, form: order_forms.OrderItemCreateForm) -> HttpResponse:
        cleaned = form.cleaned_data
        try:
            with transaction.atomic():
                services.add_order_item(
                    order=self.order,
                    product=None,
                    variant=None,
                    product_name=cleaned.get("product_name", ""),
                    product_sku=cleaned.get("product_sku", ""),
                    variant_name=cleaned.get("variant_name", ""),
                    unit_price=cleaned.get("unit_price", c.ZERO_DECIMAL_2),
                    quantity=cleaned.get("quantity", c.DEFAULT_QUANTITY),
                    discount=cleaned.get("discount", c.ZERO_DECIMAL_2),
                    tax=cleaned.get("tax", c.ZERO_DECIMAL_2),
                    weight=cleaned.get("weight", c.ZERO_DECIMAL_3),
                    attributes=cleaned.get("attributes") or {},
                    personalization=cleaned.get("personalization") or {},
                    is_gift=cleaned.get("is_gift", False),
                    gift_message=cleaned.get("gift_message", ""),
                    gift_wrapping=cleaned.get("gift_wrapping", ""),
                    expected_ship_date=cleaned.get("expected_ship_date"),
                    promised_delivery_date=cleaned.get("promised_delivery_date"),
                    metadata=cleaned.get("metadata") or {},
                )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderItemCreateView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not add this line item. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Line item added."))
        return redirect("orders:order_detail", id=str(self.order.pk))

class OrderItemUpdateView(LoginRequiredMixin, UpdateView):
    """Edit an existing order line item."""
    model = OrderItem
    form_class = order_forms.OrderItemUpdateForm
    template_name = "orders/order_item_update.html"
    context_object_name = "item"
    pk_url_kwarg = "item_id"

    def get_object(self, queryset: Optional[QuerySet[OrderItem]] = None) -> OrderItem:
        item_id = self.kwargs.get("item_id")
        if item_id is None:
            raise Http404(_("Item not found."))
        item = selectors.get_order_item_by_id(item_id=int(item_id))
        if item is None:
            raise Http404(_("Item not found."))
        if not _user_owns_order(self.request.user, item.order):
            raise PermissionDenied(
                _("You are not allowed to edit this line item."),
            )
        return item

    def form_valid(self, form: order_forms.OrderItemUpdateForm) -> HttpResponse:
        try:
            self.object = form.save()
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderItemUpdateView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not update this line item. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Line item updated."))
        return redirect("orders:order_detail", id=str(self.object.order_id))

class OrderItemDeleteView(LoginRequiredMixin, View):
    """Remove a line item from an order (ownership-checked)."""
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        item_id = kwargs.get("item_id")
        if item_id is None:
            raise Http404(_("Item not found."))
        item = selectors.get_order_item_by_id(item_id=int(item_id))
        if item is None:
            raise Http404(_("Item not found."))
        if not _user_owns_order(request.user, item.order):
            return HttpResponseForbidden(
                _("You are not allowed to delete this line item."),
            )
        order_id = str(item.order_id)
        try:
            services.delete_order_item(item=item)
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
            return redirect("orders:order_detail", id=order_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderItemDeleteView failed: %s", exc)
            messages.error(
                request,
                _("We could not remove this line item. Please try again later."),
            )
            return redirect("orders:order_detail", id=order_id)
        messages.success(request, _("Line item removed."))
        return redirect("orders:order_detail", id=order_id)

class OrderItemQuantityUpdateView(LoginRequiredMixin, FormView):
    """
    Update only the quantity of a single line item.

    Quantities can only be reduced via this view. Increasing the
    quantity requires adding a new line item (enforced by the
    service layer).
    """
    template_name = "orders/order_item_quantity.html"
    form_class = order_forms.QuantityUpdateForm

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        item_id = kwargs.get("item_id")
        if item_id is None:
            raise Http404(_("Item not found."))
        self.item = selectors.get_order_item_by_id(item_id=int(item_id))
        if self.item is None:
            raise Http404(_("Item not found."))
        if not _user_owns_order(request.user, self.item.order):
            raise PermissionDenied(
                _("You are not allowed to edit this line item."),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self) -> Dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs["item"] = self.item
        return kwargs

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["item"] = self.item
        context["order"] = self.item.order
        return context

    def form_valid(self, form: order_forms.QuantityUpdateForm) -> HttpResponse:
        new_quantity = form.cleaned_data.get("quantity", 0)
        try:
            services.update_order_item_quantity(
                item=self.item,
                new_quantity=int(new_quantity),
            )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderItemQuantityUpdateView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not update the quantity. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Quantity updated."))
        return redirect("orders:order_detail", id=str(self.item.order_id))

# ==============================================================================
# 16. ORDER ITEM STATUS VIEWS
# ==============================================================================
class OrderItemStatusUpdateView(LoginRequiredMixin, View):
    """Update a single line item's status (ownership-checked)."""
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        item_id = kwargs.get("item_id")
        new_status = kwargs.get("status") or request.POST.get("status", "")
        if item_id is None or not new_status:
            raise Http404(_("Item or status not provided."))
        item = selectors.get_order_item_by_id(item_id=int(item_id))
        if item is None:
            raise Http404(_("Item not found."))
        if not _user_owns_order(request.user, item.order):
            return HttpResponseForbidden(
                _("You are not allowed to edit this line item."),
            )
        try:
            services.update_order_item_status(item=item, new_status=new_status)
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
            return redirect("orders:order_detail", id=str(item.order_id))
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderItemStatusUpdateView failed: %s", exc)
            messages.error(
                request,
                _("We could not update the item status."),
            )
            return redirect("orders:order_detail", id=str(item.order_id))
        messages.success(request, _("Item status updated."))
        return redirect("orders:order_detail", id=str(item.order_id))

# ==============================================================================
# 17. SHIPMENT UPDATE VIEWS
# ==============================================================================
class ShipmentCreateView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """Operator-only shipment creation form."""
    template_name = "orders/shipment_create.html"
    form_class = order_forms.ShipmentForm

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        self.order = _order_or_404(
            order_id=kwargs.get("id"),
            user=request.user,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["order"] = self.order
        return context

    def form_valid(self, form: order_forms.ShipmentForm) -> HttpResponse:
        cleaned = form.cleaned_data
        try:
            with transaction.atomic():
                shipment = services.create_shipment(
                    order=self.order,
                    carrier=cleaned.get("carrier", "Unknown"),
                    tracking_number=cleaned.get("tracking_number", ""),
                    tracking_url=cleaned.get("tracking_url", ""),
                    shipping_cost=cleaned.get("shipping_cost", c.ZERO_DECIMAL_2),
                    notes=cleaned.get("notes", ""),
                    estimated_delivery_date=cleaned.get("estimated_delivery_date"),
                    carrier_service_level=cleaned.get("carrier_service_level", ""),
                    carrier_api_integration_id=cleaned.get(
                        "carrier_api_integration_id", "",
                    ),
                )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ShipmentCreateView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not create the shipment."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Shipment created."))
        return redirect("orders:shipment_detail", id=str(self.order.pk))

class ShipmentStatusUpdateView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Operator-only shipment status transition."""
    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        shipment_id = kwargs.get("shipment_id")
        new_status = kwargs.get("status") or request.POST.get("status", "")
        if shipment_id is None or not new_status:
            raise Http404(_("Shipment or status not provided."))
        try:
            shipment = Shipment.objects.select_related("order").get(pk=shipment_id)
        except Shipment.DoesNotExist:
            raise Http404(_("Shipment not found."))
        try:
            shipment.status = new_status
            shipment.save(update_fields=["status", "updated_at"])
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
            return redirect("orders:tracking_detail", shipment_id=shipment.pk)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ShipmentStatusUpdateView failed: %s", exc)
            messages.error(request, _("We could not update the shipment status."))
            return redirect("orders:tracking_detail", shipment_id=shipment.pk)
        messages.success(request, _("Shipment status updated."))
        return redirect("orders:tracking_detail", shipment_id=shipment.pk)

# ==============================================================================
# 18. ORDER NOTES VIEWS
# ==============================================================================
class OrderNoteCreateView(LoginRequiredMixin, View):
    """
    Append a note to an order.

    The view persists a ``OrderNote`` record directly; the model
    signal layer emits the corresponding timeline event.
    """
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        order = _order_or_404(order_id=kwargs.get("id"), user=request.user)
        text = u.normalize_whitespace(request.POST.get("text", ""))
        note_type = request.POST.get("note_type", OrderNote.NoteType.CUSTOMER)
        is_visible = u.to_bool(
            request.POST.get("is_visible_to_customer", "false"),
            default=False,
        )
        if not text:
            messages.error(request, _("A note must contain at least some text."))
            return redirect("orders:order_detail", id=str(order.pk))
        try:
            OrderNote.objects.create(
                order=order,
                author=request.user,
                text=text,
                note_type=note_type,
                is_visible_to_customer=is_visible,
            )
            messages.success(request, _("Note added."))
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderNoteCreateView failed: %s", exc)
            messages.error(request, _("We could not save your note."))
        return redirect("orders:order_detail", id=str(order.pk))

# ==============================================================================
# 19. PAYMENT STATUS UPDATE VIEWS
# ==============================================================================
class PaymentStatusUpdateView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Operator-only payment status transition.

    Delegates the actual state transition to
    ``services.update_payment_status``.
    """
    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payment_id = kwargs.get("payment_id")
        new_status = kwargs.get("status") or request.POST.get("status", "")
        if payment_id is None or not new_status:
            raise Http404(_("Payment or status not provided."))
        try:
            payment = Payment.objects.select_related("order").get(pk=payment_id)
        except Payment.DoesNotExist:
            raise Http404(_("Payment not found."))
        try:
            services.update_payment_status(
                payment=payment,
                new_status=new_status,
            )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
            return redirect("orders:payment_detail", payment_id=payment.pk)
        except Exception as exc:  # noqa: BLE001
            logger.exception("PaymentStatusUpdateView failed: %s", exc)
            messages.error(request, _("We could not update the payment status."))
            return redirect("orders:payment_detail", payment_id=payment.pk)
        messages.success(request, _("Payment status updated."))
        return redirect("orders:payment_detail", payment_id=payment.pk)

# ==============================================================================
# 20. REFUND ADMIN VIEWS
# ==============================================================================
class RefundApprovalView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """Operator approval of a refund."""
    template_name = "orders/refund_approval.html"
    form_class = order_forms.RefundApprovalForm

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        refund_id = kwargs.get("refund_id")
        if refund_id is None:
            raise Http404(_("Refund not found."))
        self.refund = get_object_or_404(Refund, pk=refund_id)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["refund"] = self.refund
        return context

    def form_valid(self, form: order_forms.RefundApprovalForm) -> HttpResponse:
        try:
            with transaction.atomic():
                services.approve_refund(
                    refund=self.refund,
                    approved_by=self.request.user,
                )
                if form.cleaned_data.get("approval_notes"):
                    self.refund.internal_notes = (
                        (self.refund.internal_notes or "")
                        + "\n"
                        + form.cleaned_data["approval_notes"]
                    )
                    self.refund.save(update_fields=["internal_notes", "updated_at"])
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("RefundApprovalView failed: %s", exc)
            messages.error(self.request, _("We could not approve this refund."))
            return self.form_invalid(form)
        messages.success(self.request, _("Refund approved."))
        return redirect("orders:refund_detail", refund_id=self.refund.pk)

class RefundRejectionView(LoginRequiredMixin, UserPassesTestMixin, View):
    """Operator rejection of a refund."""
    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        refund_id = kwargs.get("refund_id")
        if refund_id is None:
            raise Http404(_("Refund not found."))
        self.refund = get_object_or_404(Refund, pk=refund_id)
        reason = u.normalize_whitespace(request.POST.get("reason", ""))
        try:
            services.reject_refund(
                refund=self.refund,
                approved_by=request.user,
                rejection_reason=reason,
            )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(request, msg)
            return redirect("orders:refund_detail", refund_id=self.refund.pk)
        except Exception as exc:  # noqa: BLE001
            logger.exception("RefundRejectionView failed: %s", exc)
            messages.error(request, _("We could not reject this refund."))
            return redirect("orders:refund_detail", refund_id=self.refund.pk)
        messages.success(request, _("Refund rejected."))
        return redirect("orders:refund_detail", refund_id=self.refund.pk)

class RefundCompletionView(LoginRequiredMixin, UserPassesTestMixin, FormView):
    """Operator completion of a refund."""
    template_name = "orders/refund_completion.html"
    form_class = order_forms.RefundCompletionForm

    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        refund_id = kwargs.get("refund_id")
        if refund_id is None:
            raise Http404(_("Refund not found."))
        self.refund = get_object_or_404(Refund, pk=refund_id)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["refund"] = self.refund
        return context

    def form_valid(self, form: order_forms.RefundCompletionForm) -> HttpResponse:
        try:
            with transaction.atomic():
                self.refund.gateway_refund_id = (
                    form.cleaned_data.get("gateway_refund_id") or ""
                )
                self.refund.save(
                    update_fields=["gateway_refund_id", "updated_at"],
                )
                services.complete_refund(refund=self.refund)
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("RefundCompletionView failed: %s", exc)
            messages.error(self.request, _("We could not complete this refund."))
            return self.form_invalid(form)
        messages.success(self.request, _("Refund completed."))
        return redirect("orders:refund_detail", refund_id=self.refund.pk)

# ==============================================================================
# 21. SEARCH ENDPOINT (lightweight)
# ==============================================================================
class OrderQuickSearchView(LoginRequiredMixin, View):
    """
    Lightweight non-AJAX search endpoint returning a plain HTML
    fragment. Useful for server-rendered typeahead.
    """
    def get(
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        query = u.normalize_whitespace(request.GET.get("q", ""))
        if len(query) < 2:
            return HttpResponse("")
        try:
            qs = selectors.search_orders(query).only(
                "id", "order_number", "email", "status",
            )[:10]
        except Exception:  # noqa: BLE001
            logger.exception("OrderQuickSearchView failed")
            return HttpResponse("")
        return render(
            request,
            "orders/_quick_search_results.html",
            {"results": qs, "query": query},
        )

# ==============================================================================
# 22. HEALTH CHECK
# ==============================================================================
class OrderHealthView(View):
    """
    Lightweight liveness probe for the orders app.

    Always returns 200 OK with a small JSON payload. Used by
    load balancers and uptime monitors.
    """
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        return JsonResponse(
            {
                "status": "ok",
                "app": "orders",
                "timestamp": u.format_iso(timezone.now()),
            },
        )

# ==============================================================================
# 23. CSV / JSON HELPERS (function-based)
# ==============================================================================
@login_required
@require_http_methods(["GET"])
def order_csv_export_view(
    request: HttpRequest,
) -> HttpResponse:
    """
    Function-based CSV export endpoint.

    Provided as a function-based view to support simple GET-link
    access from the admin or staff dashboard. All filtering is
    performed by the selector layer.
    """
    try:
        qs = selectors.get_orders_for_csv_export(
            queryset=selectors.get_orders(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("order_csv_export_view failed: %s", exc)
        return HttpResponse(
            content_type="text/plain",
            content=_("Could not generate the export. Please try again later."),
            status=500,
        )
    timestamp = u.format_export_timestamp()
    filename = f"{c.CSV_EXPORT_FILENAME_PREFIX}{timestamp}.csv"
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write(c.CSV_BOM)
    writer = csv.writer(response)
    writer.writerow(c.CSV_EXPORT_FIELDS)
    for order in qs.iterator(chunk_size=c.EXPORT_BATCH_SIZE):
        row: List[str] = []
        for field_name in c.CSV_EXPORT_FIELDS:
            value = getattr(order, field_name, "")
            if hasattr(value, "isoformat"):
                try:
                    value = value.isoformat()
                except Exception:  # noqa: BLE001
                    value = ""
            elif isinstance(value, (dict, list)):
                try:
                    value = json.dumps(value, default=str)
                except Exception:  # noqa: BLE001
                    value = ""
            else:
                value = u.safe_str(value)
            row.append(value)
        writer.writerow(row)
    return response

# ==============================================================================
# 24. ORDER ADDRESS FORM (helper for create / edit flows)
# ==============================================================================
class OrderAddressFormView(LoginRequiredMixin, FormView):
    """
    Form for collecting an immutable address snapshot.

    The view delegates persistence to
    ``services.create_address_snapshot``. The newly-created
    snapshot is then passed to the order-create flow via a
    query-string parameter.
    """
    template_name = "orders/address_form.html"
    form_class = order_forms.OrderAddressForm

    def form_valid(
        self, form: order_forms.OrderAddressForm,
    ) -> HttpResponse:
        cleaned = form.cleaned_data
        try:
            snapshot = services.create_address_snapshot(
                full_name=cleaned.get("full_name", ""),
                phone_number=cleaned.get("phone_number", ""),
                company=cleaned.get("company", ""),
                address_line_1=cleaned.get("address_line_1", ""),
                address_line_2=cleaned.get("address_line_2", ""),
                city=cleaned.get("city", ""),
                state_or_province=cleaned.get("state_or_province", ""),
                postal_code=cleaned.get("postal_code", ""),
                country=cleaned.get("country", ""),
                country_code=cleaned.get("country_code", ""),
                phone_e164=cleaned.get("phone_e164", ""),
                latitude=cleaned.get("latitude"),
                longitude=cleaned.get("longitude"),
                delivery_notes=cleaned.get("delivery_notes", ""),
                metadata=cleaned.get("metadata") or {},
            )
        except ValidationError as exc:
            for msg in getattr(exc, "messages", [str(exc)]):
                messages.error(self.request, msg)
            return self.form_invalid(form)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderAddressFormView failed: %s", exc)
            messages.error(
                self.request,
                _("We could not save the address. Please try again later."),
            )
            return self.form_invalid(form)
        messages.success(self.request, _("Address saved."))
        return redirect(
            f"{reverse('orders:order_create')}?shipping_snapshot={snapshot.pk}",
        )

# ==============================================================================
# 25. CSRF-Exempt Health Endpoint (for load balancers)
# ==============================================================================
@method_decorator(csrf_exempt, name="dispatch")
class OrderStatusAPIView(View):
    """
    Unauthenticated read-only status endpoint.

    Returns the canonical order statuses / payment statuses as a
    JSON payload. Designed to power CMS-driven status dropdowns
    in the storefront and admin.
    """
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        return JsonResponse(
            {
                "order_statuses": [
                    {"value": value, "label": label}
                    for value, label in Order.OrderStatus.choices
                ],
                "payment_statuses": [
                    {"value": value, "label": label}
                    for value, label in Order.PaymentStatus.choices
                ],
                "sources": [
                    {"value": value, "label": label}
                    for value, label in Order.Source.choices
                ],
                "fraud_check_statuses": [
                    {"value": value, "label": label}
                    for value, label in Order.FraudCheckStatus.choices
                ],
                "shipment_statuses": [
                    {"value": value, "label": label}
                    for value, label in Shipment.ShipmentStatus.choices
                ],
                "return_statuses": [
                    {"value": value, "label": label}
                    for value, label in ReturnRequest.ReturnStatus.choices
                ],
                "refund_statuses": [
                    {"value": value, "label": label}
                    for value, label in Refund.RefundStatus.choices
                ],
            },
        )

# ==============================================================================
# 26. STATISTICS / KPI JSON ENDPOINT
# ==============================================================================
class OrderKPIsAPIView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Lightweight JSON endpoint returning the canonical KPI summary.
    """
    def test_func(self) -> bool:
        return bool(
            getattr(self.request.user, "is_staff", False)
            or getattr(self.request.user, "is_superuser", False)
        )

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        try:
            summary = selectors.get_kpi_summary()
        except Exception as exc:  # noqa: BLE001
            logger.exception("OrderKPIsAPIView failed: %s", exc)
            return JsonResponse({"error": "unavailable"}, status=503)
        return JsonResponse(
            {
                key: (
                    str(value) if hasattr(value, "isoformat") else value
                )
                for key, value in summary.items()
            },
        )

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Order list / history
    "OrderListView",
    "MyOrdersView",
    "OrderHistoryView",
    # Order detail
    "OrderDetailView",
    "MyOrderDetailView",
    # Create / Update / Delete
    "OrderCreateView",
    "OrderUpdateView",
    "OrderEditView",
    "OrderDeleteView",
    # State transitions
    "OrderCancelView",
    "OrderConfirmView",
    "OrderCompleteView",
    "OrderHoldView",
    "OrderResumeView",
    "OrderArchiveView",
    "OrderRestoreView",
    # Search / filter / dashboard
    "OrderSearchView",
    "OrderFilterView",
    "OrderDashboardView",
    "OrderQuickSearchView",
    "OrderSearchEndpointView",
    "OrderAutocompleteView",
    # Timeline / status
    "OrderTimelineView",
    "OrderStatusRefreshView",
    "OrderTimelineRefreshView",
    "OrderTrackingRefreshView",
    "OrderStatusAPIView",
    "OrderKPIsAPIView",
    # Invoices
    "InvoiceView",
    "DownloadInvoiceView",
    # Shipment / tracking
    "TrackOrderView",
    "ShipmentDetailView",
    "TrackingDetailView",
    "ShipmentHistoryView",
    "TrackingLookupView",
    "ShipmentCreateView",
    "ShipmentStatusUpdateView",
    # Returns
    "ReturnRequestView",
    "ReturnDetailView",
    "ReturnStatusView",
    "ReturnApprovalView",
    "ReturnCompletionView",
    # Payments
    "PaymentDetailView",
    "PaymentStatusView",
    "RetryPaymentView",
    "PaymentStatusUpdateView",
    # Refunds
    "RefundRequestView",
    "RefundDetailView",
    "RefundApprovalView",
    "RefundRejectionView",
    "RefundCompletionView",
    # Attachments
    "AttachmentUploadView",
    "AttachmentDeleteView",
    "AttachmentDownloadView",
    "AttachmentPreviewView",
    # Export / Import
    "OrderExportView",
    "OrderImportView",
    "order_csv_export_view",
    # Reorder
    "ReorderView",
    # Order items
    "OrderItemCreateView",
    "OrderItemUpdateView",
    "OrderItemDeleteView",
    "OrderItemQuantityUpdateView",
    "OrderItemStatusUpdateView",
    # Notes
    "OrderNoteCreateView",
    # Address
    "OrderAddressFormView",
    # Health
    "OrderHealthView",
    # Helpers
    "_coerce_uuid",
    "_client_ip",
    "_client_user_agent",
    "_is_ajax",
    "_user_owns_order",
    "_order_or_404",
    "_safe_service_call",
]