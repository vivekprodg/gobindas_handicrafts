"""
Enterprise-grade Class-Based Views for the Inventory application.

Thin presentation layer delegating read paths to selectors and write
paths to services.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import F, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView, FormView, ListView, TemplateView, UpdateView, View

from . import selectors, services
from .forms import InventoryForm, ReservationForm, RestockForm, StockAdjustmentForm, TransferStockForm
from .models import Inventory, InventoryTransaction, StockAdjustment, StockReservation, Warehouse

logger = logging.getLogger(__name__)
User = get_user_model()

_DEFAULT_PAGE_SIZE = 25
_DEFAULT_LARGE_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_DEFAULT_STAFF_GROUP = "Inventory Managers"

def _get_page_size(default: int = _DEFAULT_PAGE_SIZE) -> int:
    try:
        size = int(getattr(settings, "INVENTORY_DEFAULT_PAGE_SIZE", default))
        return max(1, min(size, _MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        return default

def _user_can_manage_inventory(user: Any) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    manager_group = getattr(settings, "INVENTORY_MANAGER_GROUP", _DEFAULT_STAFF_GROUP)
    return bool(manager_group and user.groups.filter(name=manager_group).exists())

# ==============================================================================
# MIXINS
# ==============================================================================
class InventoryPermissionMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self) -> bool:
        return _user_can_manage_inventory(self.request.user)

    def handle_no_permission(self) -> HttpResponse:
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        messages.error(self.request, _("You do not have permission for this inventory action."))
        return redirect("inventory:dashboard")

class PaginationMixin:
    paginate_by: Optional[int] = None

    def get_paginate_by(self, queryset: QuerySet) -> int:
        return self.paginate_by or _get_page_size()

class SearchableListMixin:
    search_fields: List[str] = []
    date_field: Optional[str] = None

    def get_search_query(self) -> str:
        return (self.request.GET.get("q") or "").strip()

    def apply_search(self, queryset: QuerySet) -> QuerySet:
        query = self.get_search_query()
        if not query or not self.search_fields:
            return queryset
        search_q = Q()
        for field in self.search_fields:
            search_q |= Q(**{f"{field}__icontains": query})
        return queryset.filter(search_q).distinct()

    def apply_date_range(self, queryset: QuerySet) -> QuerySet:
        if not self.date_field:
            return queryset
        d_from, d_to = self.request.GET.get("date_from"), self.request.GET.get("date_to")
        filters = {}
        if d_from:
            filters[f"{self.date_field}__gte"] = d_from
        if d_to:
            filters[f"{self.date_field}__lte"] = d_to
        return queryset.filter(**filters) if filters else queryset

# ==============================================================================
# VIEWS
# ==============================================================================
class InventoryDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "inventory/dashboard.html"
    login_url = reverse_lazy("customers:login")

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        page_size = _get_page_size()
        summary = services.inventory_summary(recent_transactions_limit=_get_page_size(_DEFAULT_LARGE_PAGE_SIZE))
        low_stock_qs = selectors.get_low_stock(limit=page_size)
        out_stock_qs = selectors.get_out_of_stock(limit=page_size)
        warehouses = selectors.get_warehouses(active_only=True)

        context.update({
            "summary": summary,
            "low_stock_items": selectors.serialize_inventory_list(low_stock_qs, limit=page_size),
            "out_of_stock_items": selectors.serialize_inventory_list(out_stock_qs, limit=page_size),
            "low_stock_count": len(low_stock_qs),
            "out_of_stock_count": len(out_stock_qs),
            "warehouse_summaries": [{"warehouse": w, "summary": services.inventory_summary(warehouse=w)} for w in warehouses],
            "recent_ledger": selectors.serialize_transaction_list(selectors.get_recent_transactions(limit=page_size), limit=page_size),
            "page_title": _("Inventory Dashboard"),
        })
        return context

class InventoryListView(LoginRequiredMixin, PaginationMixin, SearchableListMixin, ListView):
    template_name = "inventory/inventory_list.html"
    context_object_name = "inventory_records"
    login_url = reverse_lazy("customers:login")
    search_fields = ["product__title", "product__sku", "product_variant__sku", "warehouse__name", "location_bin"]

    def get_queryset(self) -> QuerySet:
        wh_id = self.request.GET.get("warehouse")
        warehouse = selectors.get_warehouse_by_id(int(wh_id)) if wh_id and wh_id.isdigit() else None
        stock_status = self.request.GET.get("stock_status")
        show_inactive = self.request.GET.get("show_inactive") == "1"
        order_by = self.request.GET.get("order_by", "warehouse__name")

        qs = selectors.get_inventory(warehouse=warehouse, active_only=not show_inactive, order_by=order_by)

        if stock_status == "in_stock":
            qs = qs.filter(available_quantity__gt=0)
        elif stock_status == "out_of_stock":
            qs = qs.filter(available_quantity__lte=0, is_active=True)
        elif stock_status == "low_stock":
            qs = qs.annotate(free_stock_calc=F("available_quantity") - F("reserved_quantity")).filter(
                reorder_level__isnull=False, free_stock_calc__lte=F("reorder_level")
            )

        return self.apply_search(qs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update({
            "warehouses": selectors.get_warehouses(active_only=True),
            "selected_warehouse_id": self.request.GET.get("warehouse", ""),
            "selected_stock_status": self.request.GET.get("stock_status", ""),
            "show_inactive": self.request.GET.get("show_inactive") == "1",
            "page_title": _("Inventory Records"),
        })
        return context

class InventoryDetailView(LoginRequiredMixin, DetailView):
    template_name = "inventory/inventory_detail.html"
    context_object_name = "inventory"
    login_url = reverse_lazy("customers:login")

    def get_object(self, queryset: Optional[QuerySet] = None) -> Inventory:
        pk = self.kwargs.get("pk")
        try:
            return Inventory.objects.select_related("warehouse", "product", "product_variant").get(pk=pk)
        except Inventory.DoesNotExist:
            raise Http404(_("Inventory record not found."))

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        inv = self.object
        page_size = _get_page_size()

        context.update({
            "recent_transactions": selectors.get_transactions(warehouse=inv.warehouse, limit=page_size).filter(inventory=inv),
            "active_reservations": selectors.get_reservations(warehouse=inv.warehouse, active_only=True, limit=page_size).filter(inventory=inv),
            "adjustment_history": selectors.get_stock_adjustments(inventory=inv, limit=page_size),
            "available_stock_calculation": services.calculate_available_stock(
                product_variant=inv.product_variant, product=inv.product, warehouse=inv.warehouse
            ),
            "page_title": _("Inventory Record Detail"),
        })
        return context

class TransactionListView(LoginRequiredMixin, PaginationMixin, SearchableListMixin, ListView):
    template_name = "inventory/transaction_list.html"
    context_object_name = "transactions"
    login_url = reverse_lazy("customers:login")
    date_field = "transaction_at"
    search_fields = ["reference_number", "inventory__product__title", "inventory__product__sku", "remarks"]

    def get_queryset(self) -> QuerySet:
        wh_id = self.request.GET.get("warehouse")
        warehouse = selectors.get_warehouse_by_id(int(wh_id)) if wh_id and wh_id.isdigit() else None
        qs = selectors.get_transactions(
            warehouse=warehouse,
            sku=self.request.GET.get("sku"),
            transaction_type=self.request.GET.get("transaction_type"),
            direction=self.request.GET.get("direction"),
            reference_number=self.request.GET.get("reference_number"),
            order_by=self.request.GET.get("order_by", "-transaction_at"),
        )
        return self.apply_date_range(self.apply_search(qs))

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update({
            "warehouses": selectors.get_warehouses(active_only=True),
            "transaction_type_choices": InventoryTransaction.TransactionType.choices,
            "direction_choices": InventoryTransaction.FlowDirection.choices,
            "page_title": _("Inventory Transactions"),
        })
        return context

class LowStockReportView(LoginRequiredMixin, PaginationMixin, SearchableListMixin, ListView):
    template_name = "inventory/low_stock_report.html"
    context_object_name = "low_stock_items"
    login_url = reverse_lazy("customers:login")
    search_fields = ["product__title", "product__sku", "warehouse__name"]

    def get_queryset(self) -> QuerySet:
        wh_id = self.request.GET.get("warehouse")
        warehouse = selectors.get_warehouse_by_id(int(wh_id)) if wh_id and wh_id.isdigit() else None
        thresh = Decimal(self.request.GET["threshold"]) if self.request.GET.get("threshold") else None
        qs = selectors.get_low_stock(warehouse=warehouse, threshold=thresh)
        return self.apply_search(qs)

class OutOfStockReportView(LoginRequiredMixin, PaginationMixin, SearchableListMixin, ListView):
    template_name = "inventory/out_of_stock_report.html"
    context_object_name = "out_of_stock_items"
    login_url = reverse_lazy("customers:login")
    search_fields = ["product__title", "product__sku", "warehouse__name"]

    def get_queryset(self) -> QuerySet:
        wh_id = self.request.GET.get("warehouse")
        warehouse = selectors.get_warehouse_by_id(int(wh_id)) if wh_id and wh_id.isdigit() else None
        inc_damaged = self.request.GET.get("include_damaged", "1") == "1"
        qs = selectors.get_out_of_stock(warehouse=warehouse, include_damaged=inc_damaged)
        return self.apply_search(qs)

class WarehouseListView(LoginRequiredMixin, ListView):
    template_name = "inventory/warehouse_list.html"
    context_object_name = "warehouses"
    login_url = reverse_lazy("customers:login")

    def get_queryset(self) -> QuerySet:
        return selectors.get_warehouses(active_only=self.request.GET.get("show_inactive") != "1")

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["warehouse_summaries"] = [
            {"warehouse": w, "summary": services.inventory_summary(warehouse=w)} for w in context["warehouses"]
        ]
        context["page_title"] = _("Warehouses")
        return context

class WarehouseInventorySummaryView(LoginRequiredMixin, DetailView):
    template_name = "inventory/warehouse_detail.html"
    context_object_name = "warehouse"
    login_url = reverse_lazy("customers:login")

    def get_object(self, queryset: Optional[QuerySet] = None) -> Warehouse:
        warehouse = selectors.get_warehouse_by_id(self.kwargs.get("pk"))
        if not warehouse:
            raise Http404(_("Warehouse not found."))
        return warehouse

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        context = super().get_context_data(**kwargs)
        wh = self.object
        context.update({
            "summary": services.inventory_summary(warehouse=wh),
            "low_stock_items": selectors.serialize_inventory_list(selectors.get_low_stock(warehouse=wh, limit=10), limit=10),
            "out_of_stock_items": selectors.serialize_inventory_list(selectors.get_out_of_stock(warehouse=wh, limit=10), limit=10),
            "recent_transactions": selectors.serialize_transaction_list(selectors.get_recent_transactions(warehouse=wh, limit=10), limit=10),
            "page_title": _("Warehouse Summary"),
        })
        return context

class StockAdjustmentCreateView(InventoryPermissionMixin, FormView):
    template_name = "inventory/stock_adjustment_form.html"
    form_class = StockAdjustmentForm
    login_url = reverse_lazy("customers:login")

    def form_valid(self, form: StockAdjustmentForm) -> HttpResponse:
        try:
            inv = form.cleaned_data["inventory"]
            res = services.adjust_stock(
                new_quantity=form.cleaned_data["new_quantity"],
                product=inv.product,
                product_variant=inv.product_variant,
                warehouse=inv.warehouse,
                reason=form.cleaned_data["reason"],
                description=form.cleaned_data.get("description", ""),
                supporting_documents=form.cleaned_data.get("supporting_documents", []),
                initiated_by=self.request.user,
                approved_by=form.cleaned_data.get("approved_by"),
                auto_apply=True,
            )
            messages.success(self.request, _("Adjustment %(num)s applied.") % {"num": res.get("adjustment_number")})
            return redirect("inventory:inventory_detail", pk=inv.pk)
        except Exception as exc:
            messages.error(self.request, str(exc))
            return self.form_invalid(form)

class StockAdjustmentListView(LoginRequiredMixin, PaginationMixin, SearchableListMixin, ListView):
    template_name = "inventory/stock_adjustment_list.html"
    context_object_name = "adjustments"
    login_url = reverse_lazy("customers:login")
    date_field = "created_at"
    search_fields = ["adjustment_number", "inventory__product__title", "description"]

    def get_queryset(self) -> QuerySet:
        qs = selectors.get_stock_adjustments(status=self.request.GET.get("status") or None)
        return self.apply_date_range(self.apply_search(qs))

class StockAdjustmentDetailView(LoginRequiredMixin, DetailView):
    template_name = "inventory/stock_adjustment_detail.html"
    context_object_name = "adjustment"
    login_url = reverse_lazy("customers:login")

    def get_object(self, queryset: Optional[QuerySet] = None) -> StockAdjustment:
        adj = selectors.get_adjustment_by_number(self.kwargs.get("adjustment_number"))
        if not adj:
            raise Http404(_("Stock adjustment not found."))
        return adj

class StockAdjustmentApproveView(InventoryPermissionMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        adj = selectors.get_adjustment_by_number(self.kwargs.get("adjustment_number"))
        if not adj:
            raise Http404(_("Adjustment not found."))
        try:
            res = services.approve_adjustment(
                adjustment_id=adj.pk,
                approved_by=request.user,
                apply_immediately=request.POST.get("apply_immediately", "1") == "1",
            )
            messages.success(request, res.get("message", _("Adjustment updated.")))
        except Exception as exc:
            messages.error(request, str(exc))
        return redirect("inventory:stock_adjustment_detail", adjustment_number=adj.adjustment_number)

class StockTransferView(InventoryPermissionMixin, FormView):
    template_name = "inventory/stock_transfer_form.html"
    form_class = TransferStockForm
    login_url = reverse_lazy("customers:login")

    def form_valid(self, form: TransferStockForm) -> HttpResponse:
        try:
            res = services.transfer_stock(
                quantity=form.cleaned_data["quantity"],
                source_warehouse=form.cleaned_data["source_warehouse"],
                destination_warehouse=form.cleaned_data["destination_warehouse"],
                product=form.cleaned_data.get("product"),
                product_variant=form.cleaned_data.get("product_variant"),
                reference_number=form.cleaned_data.get("reference_number", ""),
                remarks=form.cleaned_data.get("remarks", ""),
                performed_by=self.request.user,
            )
            messages.success(self.request, _("Transfer completed. Group ID: %(id)s") % {"id": res.get("transfer_group_id")})
            return redirect("inventory:warehouse_list")
        except Exception as exc:
            messages.error(self.request, str(exc))
            return self.form_invalid(form)

class ReservationListView(LoginRequiredMixin, PaginationMixin, SearchableListMixin, ListView):
    template_name = "inventory/reservation_list.html"
    context_object_name = "reservations"
    login_url = reverse_lazy("customers:login")
    date_field = "expires_at"
    search_fields = ["reservation_token", "user__email", "inventory__product__title", "notes"]

    def get_queryset(self) -> QuerySet:
        status = self.request.GET.get("status")
        qs = selectors.get_reservations(
            active_only=not status,
            expiry_status=self.request.GET.get("expiry_status"),
        )
        if status:
            qs = qs.filter(status=status)
        return self.apply_date_range(self.apply_search(qs))

class ReservationDetailView(LoginRequiredMixin, DetailView):
    template_name = "inventory/reservation_detail.html"
    context_object_name = "reservation"
    login_url = reverse_lazy("customers:login")

    def get_object(self, queryset: Optional[QuerySet] = None) -> StockReservation:
        res = selectors.get_reservation_by_token(self.kwargs.get("token"))
        if not res:
            raise Http404(_("Reservation not found."))
        return res

class ReservationReleaseView(InventoryPermissionMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        res = selectors.get_reservation_by_token(self.kwargs.get("token"))
        if not res:
            raise Http404(_("Reservation not found."))
        try:
            result = services.release_stock(reservation=res, performed_by=request.user)
            messages.success(request, result.get("message", _("Released.")))
        except Exception as exc:
            messages.error(request, str(exc))
        return redirect("inventory:reservation_list")

class ReservationConvertView(InventoryPermissionMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        res = selectors.get_reservation_by_token(self.kwargs.get("token"))
        if not res:
            raise Http404(_("Reservation not found."))
        try:
            result = services.deduct_stock(
                quantity=res.quantity,
                product=res.product,
                product_variant=res.product_variant,
                warehouse=res.warehouse,
                reservation=res,
                reference_number=request.POST.get("reference_number", ""),
                reference_model=request.POST.get("reference_model", "orders.Order"),
                reference_id=request.POST.get("reference_id", str(uuid.uuid4())),
                performed_by=request.user,
            )
            messages.success(request, result.get("message", _("Converted.")))
        except Exception as exc:
            messages.error(request, str(exc))
        return redirect("inventory:reservation_list")

class ReservationCreateView(LoginRequiredMixin, FormView):
    template_name = "inventory/reservation_create_form.html"
    form_class = ReservationForm
    login_url = reverse_lazy("customers:login")

    def form_valid(self, form: ReservationForm) -> HttpResponse:
        exp_min = form.cleaned_data.get("expires_in_minutes")
        exp_delta = timedelta(minutes=int(exp_min)) if exp_min else None
        try:
            res = services.reserve_stock(
                quantity=form.cleaned_data["quantity"],
                product=form.cleaned_data.get("product"),
                product_variant=form.cleaned_data.get("product_variant"),
                warehouse=form.cleaned_data.get("warehouse"),
                user=self.request.user,
                expires_in=exp_delta,
                reservation_type=form.cleaned_data.get("reservation_type", "manual_hold"),
                reference_number=form.cleaned_data.get("reference_number", ""),
                notes=form.cleaned_data.get("notes", ""),
                performed_by=self.request.user,
            )
            messages.success(self.request, _("Reservation created."))
            return redirect("inventory:reservation_list")
        except Exception as exc:
            messages.error(self.request, str(exc))
            return self.form_invalid(form)

class RestockView(InventoryPermissionMixin, FormView):
    template_name = "inventory/restock_form.html"
    form_class = RestockForm
    login_url = reverse_lazy("customers:login")

    def form_valid(self, form: RestockForm) -> HttpResponse:
        try:
            inv = form.cleaned_data["inventory"]
            res = services.restock(
                quantity=form.cleaned_data["quantity"],
                product=inv.product,
                product_variant=inv.product_variant,
                warehouse=inv.warehouse,
                reference_number=form.cleaned_data.get("supplier_reference", ""),
                remarks=form.cleaned_data.get("remarks", ""),
                performed_by=self.request.user,
            )
            messages.success(self.request, _("Restocked. Available: %(qty)s") % {"qty": res.get("available_after")})
            return redirect("inventory:inventory_detail", pk=inv.pk)
        except Exception as exc:
            messages.error(self.request, str(exc))
            return self.form_invalid(form)

class InventoryConfigUpdateView(InventoryPermissionMixin, UpdateView):
    template_name = "inventory/inventory_config_form.html"
    form_class = InventoryForm
    login_url = reverse_lazy("customers:login")

    def get_object(self, queryset: Optional[QuerySet] = None) -> Inventory:
        try:
            return Inventory.objects.select_related("warehouse", "product", "product_variant").get(pk=self.kwargs.get("pk"))
        except Inventory.DoesNotExist:
            raise Http404(_("Inventory record not found."))

    def form_valid(self, form: InventoryForm) -> HttpResponse:
        self.object = form.save()
        messages.success(self.request, _("Inventory updated."))
        return redirect("inventory:inventory_detail", pk=self.object.pk)

class StockCheckAjaxView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        p_id, v_id, w_id = request.POST.get("product_id"), request.POST.get("product_variant_id"), request.POST.get("warehouse_id")
        try:
            qty = Decimal(request.POST.get("quantity", "1"))
        except Exception:
            qty = Decimal("1")

        product, variant, warehouse = None, None, None
        if v_id:
            from apps.catalog.models import ProductVariant
            variant = ProductVariant.objects.filter(pk=v_id).first()
        elif p_id:
            from apps.catalog.models import Product
            product = Product.objects.filter(pk=p_id).first()

        if not product and not variant:
            return JsonResponse({"status": "error", "message": "Missing product reference."}, status=400)

        if w_id and w_id.isdigit():
            warehouse = selectors.get_warehouse_by_id(int(w_id))

        try:
            data = services.check_stock(product=product, product_variant=variant, warehouse=warehouse, quantity=qty, include_all_warehouses=not warehouse)
            return JsonResponse({"status": "success", "data": data})
        except Exception as exc:
            return JsonResponse({"status": "error", "message": str(exc)}, status=400)

class InventorySummaryAjaxView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
        w_id = request.GET.get("warehouse_id")
        warehouse = selectors.get_warehouse_by_id(int(w_id)) if w_id and w_id.isdigit() else None
        summary = services.inventory_summary(warehouse=warehouse)
        return JsonResponse({"status": "success", "data": summary})

class ReleaseExpiredReservationsView(InventoryPermissionMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        res = services.release_expired_reservations()
        messages.success(request, _("Released %(count)s expired reservations.") % {"count": res.get("released", 0)})
        return redirect("inventory:reservation_list")

class LowStockNotificationView(InventoryPermissionMixin, View):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        qs = selectors.get_low_stock()
        messages.success(request, _("Notifications sent for %(count)d items.") % {"count": len(qs)})
        return redirect("inventory:low_stock_report")

__all__ = [
    "InventoryDashboardView",
    "InventoryListView",
    "InventoryDetailView",
    "InventoryConfigUpdateView",
    "TransactionListView",
    "LowStockReportView",
    "OutOfStockReportView",
    "WarehouseListView",
    "WarehouseInventorySummaryView",
    "StockAdjustmentCreateView",
    "StockAdjustmentListView",
    "StockAdjustmentDetailView",
    "StockAdjustmentApproveView",
    "StockTransferView",
    "ReservationListView",
    "ReservationDetailView",
    "ReservationReleaseView",
    "ReservationConvertView",
    "ReservationCreateView",
    "RestockView",
    "StockCheckAjaxView",
    "InventorySummaryAjaxView",
    "ReleaseExpiredReservationsView",
    "LowStockNotificationView",
]