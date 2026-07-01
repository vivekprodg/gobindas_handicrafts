import csv
from django.http import HttpResponse
from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.orders.models import (
    OrderAddressSnapshot,
    Order,
    OrderItem,
    OrderStatusHistory,
    Shipment,
    Payment,
    Refund,
    CouponUsage,
)


@admin.register(OrderAddressSnapshot)
class OrderAddressSnapshotAdmin(admin.ModelAdmin):
    """
    Admin configuration for OrderAddressSnapshot.
    Treats snapshots as strictly read-only historical records for audits.
    """
    list_display = ('full_name', 'phone_number', 'city', 'state_or_province', 'country', 'created_at')
    search_fields = ('full_name', 'phone_number', 'company', 'address_line_1', 'city', 'postal_code')
    list_filter = ('country', 'created_at')
    readonly_fields = (
        'full_name', 'phone_number', 'company', 'address_line_1', 
        'address_line_2', 'city', 'state_or_province', 'postal_code', 
        'country', 'created_at'
    )
    
    fieldsets = (
        (_("Contact Information"), {
            'fields': ('full_name', 'phone_number', 'company')
        }),
        (_("Address Details"), {
            'fields': ('address_line_1', 'address_line_2', 'city', 'state_or_province', 'postal_code', 'country')
        }),
        (_("Metadata"), {
            'fields': ('created_at',)
        }),
    )

    def has_add_permission(self, request):
        return False


class OrderItemInline(admin.TabularInline):
    """
    Inline for managing Order Items. Optimized to prevent N+1 queries.
    Uses raw_id_fields for product and variant to avoid startup crashes if 
    catalog models lack proper search_fields declarations.
    """
    model = OrderItem
    extra = 0
    raw_id_fields = ['product', 'variant']
    readonly_fields = ('total',)
    fields = (
        'product', 'variant', 'product_name', 'product_sku', 'variant_name', 
        'price', 'discount', 'tax', 'quantity', 'total', 'status'
    )


class OrderStatusHistoryInline(admin.TabularInline):
    """
    Read-only inline representing the immutable lifecycle of an Order.
    """
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ('old_status', 'new_status', 'remarks', 'created_by', 'created_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ShipmentInline(admin.TabularInline):
    """
    Provides a quick view of shipments associated with an order.
    """
    model = Shipment
    extra = 0
    readonly_fields = ('created_at', 'updated_at')
    fields = ('shipment_number', 'carrier', 'tracking_number', 'status', 'shipping_cost', 'dispatch_date', 'delivery_date')
    show_change_link = True


class PaymentInline(admin.TabularInline):
    """
    Provides a quick view of payments associated with an order.
    """
    model = Payment
    extra = 0
    readonly_fields = ('created_at', 'updated_at')
    fields = ('transaction_id', 'gateway', 'amount', 'currency', 'status', 'paid_at')
    show_change_link = True


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """
    Core Order Administrative Interface.
    Orchestrates order logistics, financials, embedded snapshots, and history tracking.
    """
    list_display = (
        'order_number', 'get_customer_display', 'status', 'payment_status', 
        'total', 'currency', 'created_at'
    )
    list_filter = ('status', 'payment_status', 'created_at', 'updated_at', 'currency')
    search_fields = (
        'order_number', 'email', 'customer__email', 'customer__first_name', 
        'customer__last_name', 'transaction_id', 'tracking_number'
    )
    list_select_related = ('customer', 'shipping_address', 'billing_address')
    raw_id_fields = ('customer', 'shipping_address', 'billing_address')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = ('id', 'created_at', 'updated_at', 'subtotal', 'tax', 'total')
    
    inlines = [OrderItemInline, PaymentInline, ShipmentInline, OrderStatusHistoryInline]
    
    fieldsets = (
        (_("Order Identification"), {
            'fields': ('id', 'order_number', 'customer', 'email')
        }),
        (_("Lifecycle & Payment Status"), {
            'fields': ('status', 'payment_status', 'payment_method', 'transaction_id')
        }),
        (_("Financials (Auto-calculated)"), {
            'fields': ('subtotal', 'discount_total', 'shipping_cost', 'tax', 'total', 'currency', 'coupon_code')
        }),
        (_("Address Snapshots"), {
            'fields': ('shipping_address', 'billing_address'),
            'description': _("Immutable references to the customer's addresses at the time of purchase.")
        }),
        (_("Fulfillment & Delivery Notes"), {
            'fields': ('tracking_number', 'tracking_url', 'carrier', 'delivery_instructions', 'customer_note')
        }),
        (_("Accounting Metadata"), {
            'fields': ('invoice_url', 'has_invoice', 'created_at', 'updated_at')
        }),
    )

    actions = [
        'mark_processing', 'mark_shipped', 'mark_delivered', 
        'mark_cancelled', 'export_orders_csv'
    ]

    @admin.display(description=_("Customer Details"), ordering='customer__email')
    def get_customer_display(self, obj):
        if obj.customer:
            name = obj.customer.get_full_name() or obj.customer.username
            return f"{name} ({obj.email})"
        return f"Guest ({obj.email})"

    @admin.action(description=_("Mark selected orders as Processing"))
    def mark_processing(self, request, queryset):
        count = 0
        for order in queryset:
            order.update_status(Order.OrderStatus.PROCESSING, user=request.user)
            count += 1
        self.message_user(request, _(f"{count} orders successfully transitioned to Processing."))

    @admin.action(description=_("Mark selected orders as Shipped"))
    def mark_shipped(self, request, queryset):
        count = 0
        for order in queryset:
            order.update_status(Order.OrderStatus.SHIPPED, user=request.user)
            count += 1
        self.message_user(request, _(f"{count} orders successfully transitioned to Shipped."))

    @admin.action(description=_("Mark selected orders as Delivered"))
    def mark_delivered(self, request, queryset):
        count = 0
        for order in queryset:
            order.update_status(Order.OrderStatus.DELIVERED, user=request.user)
            count += 1
        self.message_user(request, _(f"{count} orders successfully transitioned to Delivered."))

    @admin.action(description=_("Mark selected orders as Cancelled"))
    def mark_cancelled(self, request, queryset):
        count = 0
        for order in queryset:
            order.mark_cancelled(user=request.user, remarks=_("Bulk cancelled via administration panel."))
            count += 1
        self.message_user(request, _(f"{count} orders successfully Cancelled."))

    @admin.action(description=_("Export selected orders as CSV"))
    def export_orders_csv(self, request, queryset):
        meta = self.model._meta
        field_names = [field.name for field in meta.fields]
        
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename={meta.model_name}_export.csv'
        
        writer = csv.writer(response)
        writer.writerow(field_names)
        for obj in queryset:
            writer.writerow([getattr(obj, field) for field in field_names])
            
        return response


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    """
    Administration configuration for order parcel tracking.
    """
    list_display = ('shipment_number', 'order', 'carrier', 'status', 'shipping_cost', 'dispatch_date', 'delivery_date')
    list_filter = ('status', 'carrier', 'created_at')
    search_fields = ('shipment_number', 'tracking_number', 'order__order_number')
    raw_id_fields = ('order',)
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'
    
    fieldsets = (
        (_("Shipment Identification"), {
            'fields': ('shipment_number', 'order')
        }),
        (_("Logistics Data"), {
            'fields': ('carrier', 'tracking_number', 'tracking_url', 'warehouse')
        }),
        (_("Status & Delivery Metrics"), {
            'fields': ('status', 'shipping_cost', 'dispatch_date', 'delivery_date')
        }),
        (_("Notes & Audit"), {
            'fields': ('notes', 'created_at', 'updated_at')
        }),
    )

    actions = ['mark_dispatched_action', 'mark_delivered_action']

    @admin.action(description=_("Register physical dispatch timeframe for selected shipments"))
    def mark_dispatched_action(self, request, queryset):
        for shipment in queryset:
            shipment.mark_dispatched()
        self.message_user(request, _("Selected shipments have been marked as Dispatched."))

    @admin.action(description=_("Register physical delivery completion for selected shipments"))
    def mark_delivered_action(self, request, queryset):
        for shipment in queryset:
            shipment.mark_delivered()
        self.message_user(request, _("Selected shipments have been marked as Delivered."))


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """
    Financial ledger tracker for distinct payment captures connected to an Order.
    """
    list_display = ('transaction_id', 'order', 'gateway', 'amount', 'currency', 'status', 'paid_at')
    list_filter = ('status', 'gateway', 'currency', 'created_at')
    search_fields = ('transaction_id', 'order__order_number')
    raw_id_fields = ('order',)
    readonly_fields = ('created_at', 'updated_at')
    date_hierarchy = 'created_at'
    
    fieldsets = (
        (_("Transaction Identity"), {
            'fields': ('transaction_id', 'order', 'gateway')
        }),
        (_("Financial Amounts"), {
            'fields': ('amount', 'currency')
        }),
        (_("Processing Status"), {
            'fields': ('status', 'paid_at')
        }),
        (_("Integrations & Traceability"), {
            'fields': ('metadata', 'created_at', 'updated_at')
        }),
    )


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    """
    Tracks and executes administrative reversals of order payments.
    """
    list_display = ('id', 'order', 'payment', 'amount', 'status', 'approved_by', 'processed_at')
    list_filter = ('status', 'created_at', 'processed_at')
    search_fields = ('order__order_number', 'payment__transaction_id', 'reason')
    raw_id_fields = ('order', 'payment', 'approved_by')
    readonly_fields = ('created_at', 'updated_at', 'processed_at')
    date_hierarchy = 'created_at'
    
    fieldsets = (
        (_("Target References"), {
            'fields': ('order', 'payment')
        }),
        (_("Refund Details"), {
            'fields': ('amount', 'reason')
        }),
        (_("Lifecycle State"), {
            'fields': ('status', 'approved_by', 'processed_at')
        }),
        (_("Audit Log"), {
            'fields': ('created_at', 'updated_at')
        }),
    )


@admin.register(CouponUsage)
class CouponUsageAdmin(admin.ModelAdmin):
    """
    Maintains a ledger of promotional discounts applied per user and order.
    """
    list_display = ('coupon_code', 'user', 'order', 'discount_amount', 'used_at')
    list_filter = ('used_at',)
    search_fields = ('coupon_code', 'user__email', 'user__username', 'order__order_number')
    raw_id_fields = ('user', 'order')
    readonly_fields = ('used_at',)
    date_hierarchy = 'used_at'


@admin.register(OrderStatusHistory)
class OrderStatusHistoryAdmin(admin.ModelAdmin):
    """
    Centralized auditing interface ensuring order lifecycle transitions 
    can be viewed but never tampered with manually.
    """
    list_display = ('order', 'old_status', 'new_status', 'created_by', 'created_at')
    list_filter = ('new_status', 'old_status', 'created_at')
    search_fields = ('order__order_number', 'remarks', 'created_by__email', 'created_by__username')
    raw_id_fields = ('order', 'created_by')
    readonly_fields = ('order', 'old_status', 'new_status', 'remarks', 'created_by', 'created_at')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False