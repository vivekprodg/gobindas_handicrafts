"""
Enterprise-grade URL configuration for the Inventory application.

This file is responsible ONLY for routing HTTP requests to the appropriate
views. It does not contain business logic, database queries, validation logic,
or inventory operations.

The URL configuration is:
* enterprise-grade
* production-ready
* scalable
* modular
* maintainable
* secure
* reusable
* fully dynamic
* CMS-driven
* fully parameterized
* future-proof

The routing architecture is designed to integrate cleanly with future modules
(Purchase Orders, Sales Orders, Goods Receipt Notes, Manufacturing,
Batch/Lot Tracking, Serial Numbers, Expiry Management, Barcode/QR Systems,
Reports, Dashboards, Analytics, REST APIs, GraphQL APIs, Mobile ERP,
Workflow Approvals) without requiring major refactoring.
"""

from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    # ==============================================================================
    # INVENTORY HOME
    # ==============================================================================
    path("", views.InventoryListView.as_view(), name="inventory_list"),

    # ==============================================================================
    # INVENTORY DASHBOARD
    # ==============================================================================
    path("dashboard/", views.InventoryDashboardView.as_view(), name="dashboard"),

    # ==============================================================================
    # INVENTORY DETAIL
    # ==============================================================================
    path("<int:pk>/", views.InventoryDetailView.as_view(), name="inventory_detail"),

    # ==============================================================================
    # INVENTORY TRANSACTIONS (Audit Ledger)
    # ==============================================================================
    path("transactions/", views.TransactionListView.as_view(), name="transaction_list"),

    # ==============================================================================
    # INVENTORY CONFIGURATION (CMS-driven thresholds)
    # ==============================================================================
    path(
        "<int:pk>/configure/",
        views.InventoryConfigUpdateView.as_view(),
        name="inventory_config",
    ),

    # ==============================================================================
    # STOCK ADJUSTMENT
    # ==============================================================================
    path("adjust/", views.StockAdjustmentCreateView.as_view(), name="stock_adjustment"),
    path(
        "adjust/list/",
        views.StockAdjustmentListView.as_view(),
        name="stock_adjustment_list",
    ),
    path(
        "adjust/<str:adjustment_number>/",
        views.StockAdjustmentDetailView.as_view(),
        name="stock_adjustment_detail",
    ),
    path(
        "adjust/<str:adjustment_number>/approve/",
        views.StockAdjustmentApproveView.as_view(),
        name="stock_adjustment_approve",
    ),

    # ==============================================================================
    # STOCK TRANSFER
    # ==============================================================================
    path("transfer/", views.StockTransferView.as_view(), name="stock_transfer"),

    # ==============================================================================
    # WAREHOUSE LIST
    # ==============================================================================
    path("warehouses/", views.WarehouseListView.as_view(), name="warehouse_list"),

    # ==============================================================================
    # WAREHOUSE INVENTORY SUMMARY (Detail)
    # ==============================================================================
    path(
        "warehouses/<int:pk>/",
        views.WarehouseInventorySummaryView.as_view(),
        name="warehouse_detail",
    ),

    # ==============================================================================
    # LOW STOCK REPORT
    # ==============================================================================
    path("low-stock/", views.LowStockReportView.as_view(), name="low_stock_report"),
    path(
        "low-stock/notify/",
        views.LowStockNotificationView.as_view(),
        name="low_stock_notify",
    ),

    # ==============================================================================
    # OUT-OF-STOCK REPORT
    # ==============================================================================
    path("out-of-stock/", views.OutOfStockReportView.as_view(), name="out_of_stock_report"),

    # ==============================================================================
    # RESTOCK (Inventory Receiving)
    # ==============================================================================
    path("restock/<int:pk>/", views.RestockView.as_view(), name="restock"),

    # ==============================================================================
    # RESERVATION LIST (Stock Holds)
    # ==============================================================================
    path("reservations/", views.ReservationListView.as_view(), name="reservation_list"),
    path(
        "reservations/create/",
        views.ReservationCreateView.as_view(),
        name="reservation_create",
    ),
    path(
        "reservations/<uuid:token>/",
        views.ReservationDetailView.as_view(),
        name="reservation_detail",
    ),
    path(
        "reservations/<uuid:token>/release/",
        views.ReservationReleaseView.as_view(),
        name="reservation_release",
    ),
    path(
        "reservations/<uuid:token>/convert/",
        views.ReservationConvertView.as_view(),
        name="reservation_convert",
    ),

    # ==============================================================================
    # EXPIRED RESERVATIONS (Manual trigger)
    # ==============================================================================
    path(
        "reservations/release-expired/",
        views.ReleaseExpiredReservationsView.as_view(),
        name="release_expired_reservations",
    ),

    # ==============================================================================
    # AJAX / JSON UTILITY ENDPOINTS
    # ==============================================================================
    path(
        "ajax/stock-check/",
        views.StockCheckAjaxView.as_view(),
        name="stock_check_ajax",
    ),
    path(
        "ajax/summary/",
        views.InventorySummaryAjaxView.as_view(),
        name="inventory_summary_ajax",
    ),
]