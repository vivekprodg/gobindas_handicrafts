from __future__ import annotations

from django.urls import path

from . import views

app_name = "orders"

urlpatterns = [
    path("", views.OrderListView.as_view(), name="list"),
    path("order-list/", views.OrderListView.as_view(), name="order_list"),
    path("dashboard/", views.OrderDashboardView.as_view(), name="order_dashboard"),
    path("health/", views.OrderHealthView.as_view(), name="order_health"),

    path("my-orders/", views.MyOrdersView.as_view(), name="my_orders"),
    path("history/", views.OrderHistoryView.as_view(), name="order_history"),

    # Checkout Workflows
    path("checkout/prepare/", views.CheckoutPrepareView.as_view(), name="checkout_prepare"),
    path("checkout/success/<str:id>/", views.CheckoutSuccessView.as_view(), name="checkout_success"),

    path("create/", views.OrderCreateView.as_view(), name="order_create"),
    path("address/create/", views.OrderAddressFormView.as_view(), name="order_address_create"),
    path("search/", views.OrderSearchView.as_view(), name="order_search"),
    path("filter/", views.OrderFilterView.as_view(), name="order_filter"),

    path("export/", views.OrderExportView.as_view(), name="order_export"),
    path("import/", views.OrderImportView.as_view(), name="order_import"),
    path("csv-export/", views.order_csv_export_view, name="order_csv_export"),

    path("quick-search/", views.OrderQuickSearchView.as_view(), name="order_quick_search"),
    path("search.json", views.OrderSearchEndpointView.as_view(), name="order_search_endpoint"),
    path("autocomplete.json", views.OrderAutocompleteView.as_view(), name="order_autocomplete"),
    path("statuses.json", views.OrderStatusAPIView.as_view(), name="order_statuses_api"),
    path("kpis.json", views.OrderKPIsAPIView.as_view(), name="order_kpis_api"),

    path("tracking-lookup/", views.TrackingLookupView.as_view(), name="tracking_lookup"),

    path("<str:id>/", views.OrderDetailView.as_view(), name="order_detail"),
    path("<str:id>/my/", views.MyOrderDetailView.as_view(), name="my_order_detail"),
    path("<str:id>/edit/", views.OrderEditView.as_view(), name="order_edit"),
    path("<str:id>/update/", views.OrderUpdateView.as_view(), name="order_update"),
    path("<str:id>/delete/", views.OrderDeleteView.as_view(), name="order_delete"),

    path("<str:id>/cancel/", views.OrderCancelView.as_view(), name="cancel_order"),
    path("<str:id>/confirm/", views.OrderConfirmView.as_view(), name="order_confirm"),
    path("<str:id>/complete/", views.OrderCompleteView.as_view(), name="order_complete"),
    path("<str:id>/hold/", views.OrderHoldView.as_view(), name="order_hold"),
    path("<str:id>/resume/", views.OrderResumeView.as_view(), name="order_resume"),
    path("<str:id>/archive/", views.OrderArchiveView.as_view(), name="order_archive"),
    path("<str:id>/restore/", views.OrderRestoreView.as_view(), name="order_restore"),
    path("<str:id>/reorder/", views.ReorderView.as_view(), name="reorder"),

    path("<str:id>/timeline/", views.OrderTimelineView.as_view(), name="order_timeline"),
    path("<str:id>/track/", views.TrackOrderView.as_view(), name="track_order"),

    path("<str:id>/invoice/", views.InvoiceView.as_view(), name="invoice"),
    path("<str:id>/invoice/download/", views.DownloadInvoiceView.as_view(), name="download_invoice"),

    path("<str:id>/shipment/", views.ShipmentDetailView.as_view(), name="shipment_detail"),
    path("<str:id>/shipments/", views.ShipmentHistoryView.as_view(), name="shipment_history"),
    path("<str:id>/shipments/create/", views.ShipmentCreateView.as_view(), name="shipment_create"),

    path("<str:id>/return/", views.ReturnRequestView.as_view(), name="return_request"),
    path("<str:id>/refund/", views.RefundRequestView.as_view(), name="refund_request"),

    path("<str:id>/attachment/upload/", views.AttachmentUploadView.as_view(), name="attachment_upload"),
    path("<str:id>/item/create/", views.OrderItemCreateView.as_view(), name="order_item_create"),
    path("<str:id>/note/", views.OrderNoteCreateView.as_view(), name="order_note_create"),

    path("<str:id>/status.json", views.OrderStatusRefreshView.as_view(), name="order_status_refresh"),
    path("<str:id>/timeline.json", views.OrderTimelineRefreshView.as_view(), name="order_timeline_refresh"),
    path("<str:id>/tracking.json", views.OrderTrackingRefreshView.as_view(), name="order_tracking_refresh"),

    path("item/<int:item_id>/", views.OrderItemUpdateView.as_view(), name="order_item_update"),
    path("item/<int:item_id>/edit/", views.OrderItemUpdateView.as_view(), name="order_item_edit"),
    path("item/<int:item_id>/delete/", views.OrderItemDeleteView.as_view(), name="order_item_delete"),
    path("item/<int:item_id>/quantity/", views.OrderItemQuantityUpdateView.as_view(), name="order_item_quantity"),
    path("item/<int:item_id>/status/", views.OrderItemStatusUpdateView.as_view(), name="order_item_status"),

    path("shipment/<int:shipment_id>/", views.TrackingDetailView.as_view(), name="tracking_detail"),
    path("shipment/<int:shipment_id>/status/", views.ShipmentStatusUpdateView.as_view(), name="shipment_status"),

    path("attachment/<int:attachment_id>/", views.AttachmentDownloadView.as_view(), name="attachment_download"),
    path("attachment/<int:attachment_id>/preview/", views.AttachmentPreviewView.as_view(), name="attachment_preview"),
    path("attachment/<int:attachment_id>/delete/", views.AttachmentDeleteView.as_view(), name="attachment_delete"),

    path("payment/<int:payment_id>/", views.PaymentDetailView.as_view(), name="payment_detail"),
    path("payment/<int:payment_id>/status/", views.PaymentStatusView.as_view(), name="payment_status"),
    path("payment/<int:payment_id>/retry/", views.RetryPaymentView.as_view(), name="payment_retry"),
    path("payment/<int:payment_id>/update/", views.PaymentStatusUpdateView.as_view(), name="payment_status_update"),

    path("refund/<int:refund_id>/", views.RefundDetailView.as_view(), name="refund_detail"),
    path("refund/<int:refund_id>/approve/", views.RefundApprovalView.as_view(), name="refund_approval"),
    path("refund/<int:refund_id>/reject/", views.RefundRejectionView.as_view(), name="refund_rejection"),
    path("refund/<int:refund_id>/complete/", views.RefundCompletionView.as_view(), name="refund_completion"),

    path("return/<uuid:return_id>/", views.ReturnDetailView.as_view(), name="return_detail"),
    path("return/<uuid:return_id>/status/", views.ReturnStatusView.as_view(), name="return_status"),
    path("return/<uuid:return_id>/approve/", views.ReturnApprovalView.as_view(), name="return_approval"),
    path("return/<uuid:return_id>/complete/", views.ReturnCompletionView.as_view(), name="return_completion"),
]