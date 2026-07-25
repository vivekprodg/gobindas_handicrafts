"""
Enterprise-grade URL configuration for the Inventory application.
"""

from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    # Dashboard & Main List
    path("", views.InventoryListView.as_view(), name="inventory_list"),
    path("dashboard/", views.InventoryDashboardView.as_view(), name="dashboard"),
    path("<int:pk>/", views.InventoryDetailView.as_view(), name="inventory_detail"),
    path("<int:pk>/configure/", views.InventoryConfigUpdateView.as_view(), name="inventory_config"),

    # Transactions Ledger
    path("transactions/", views.TransactionListView.as_view(), name="transaction_list"),

    # Adjustments
    path("adjust/", views.StockAdjustmentCreateView.as_view(), name="stock_adjustment"),
    path("adjust/list/", views.StockAdjustmentListView.as_view(), name="stock_adjustment_list"),
    path("adjust/<str:adjustment_number>/", views.StockAdjustmentDetailView.as_view(), name="stock_adjustment_detail"),
    path("adjust/<str:adjustment_number>/approve/", views.StockAdjustmentApproveView.as_view(), name="stock_adjustment_approve"),

    # Transfers
    path("transfer/", views.StockTransferView.as_view(), name="stock_transfer"),

    # Warehouses
    path("warehouses/", views.WarehouseListView.as_view(), name="warehouse_list"),
    path("warehouses/<int:pk>/", views.WarehouseInventorySummaryView.as_view(), name="warehouse_detail"),

    # Reports & Notifications
    path("low-stock/", views.LowStockReportView.as_view(), name="low_stock_report"),
    path("low-stock/notify/", views.LowStockNotificationView.as_view(), name="low_stock_notify"),
    path("out-of-stock/", views.OutOfStockReportView.as_view(), name="out_of_stock_report"),

    # Restock & Receiving
    path("restock/<int:pk>/", views.RestockView.as_view(), name="restock"),

    # Reservations
    path("reservations/", views.ReservationListView.as_view(), name="reservation_list"),
    path("reservations/create/", views.ReservationCreateView.as_view(), name="reservation_create"),
    path("reservations/<uuid:token>/", views.ReservationDetailView.as_view(), name="reservation_detail"),
    path("reservations/<uuid:token>/release/", views.ReservationReleaseView.as_view(), name="reservation_release"),
    path("reservations/<uuid:token>/convert/", views.ReservationConvertView.as_view(), name="reservation_convert"),
    path("reservations/release-expired/", views.ReleaseExpiredReservationsView.as_view(), name="release_expired_reservations"),

    # AJAX Utilities
    path("ajax/stock-check/", views.StockCheckAjaxView.as_view(), name="stock_check_ajax"),
    path("ajax/summary/", views.InventorySummaryAjaxView.as_view(), name="inventory_summary_ajax"),
]