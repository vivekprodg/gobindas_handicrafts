# D:\Websites\handicraft-ecommerce\apps\orders\urls.py
from django.urls import path
from . import views

app_name = "orders"

urlpatterns = [
    # --------------------------------------------------------------------------
    # Order Discovery & History
    # --------------------------------------------------------------------------
    path("", views.OrderListView.as_view(), name="list"),
    path("history/", views.OrderHistoryView.as_view(), name="order_history"),

    # --------------------------------------------------------------------------
    # Core Order Lifecycle & Details
    # --------------------------------------------------------------------------
    # Using <str:id> to seamlessly support INT, UUID, or String-based identifiers
    # matching the project's existing template references (e.g., order.id)
    path("<str:id>/", views.OrderDetailView.as_view(), name="order_detail"),
    path("<str:id>/track/", views.TrackOrderView.as_view(), name="track_order"),
    path("<str:id>/cancel/", views.CancelOrderView.as_view(), name="cancel_order"),
    path("<str:id>/reorder/", views.ReorderView.as_view(), name="reorder"),

    # --------------------------------------------------------------------------
    # Invoicing & Financials
    # --------------------------------------------------------------------------
    path("<str:id>/invoice/", views.InvoiceView.as_view(), name="invoice"),
    path("<str:id>/invoice/download/", views.DownloadInvoiceView.as_view(), name="download_invoice"),

    # --------------------------------------------------------------------------
    # Fulfillment & Reverse Logistics
    # --------------------------------------------------------------------------
    path("<str:id>/shipment/", views.ShipmentDetailsView.as_view(), name="shipment_details"),
    path("<str:id>/return/", views.ReturnRequestView.as_view(), name="return_request"),
    path("<str:id>/refund/", views.RefundRequestView.as_view(), name="refund_request"),
]