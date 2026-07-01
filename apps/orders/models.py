import uuid
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class OrderAddressSnapshot(models.Model):
    """
    Immutable snapshot of a shipping or billing address at the time of order placement.
    Ensures historical accuracy for audits and fulfillment, surviving any subsequent 
    customer address profile edits or account deletions.
    """
    full_name = models.CharField(max_length=255, verbose_name=_("Full Name"))
    phone_number = models.CharField(max_length=50, verbose_name=_("Phone Number"))
    company = models.CharField(max_length=255, blank=True, default="", verbose_name=_("Company"))
    
    address_line_1 = models.CharField(max_length=255, verbose_name=_("Address Line 1"))
    address_line_2 = models.CharField(max_length=255, blank=True, default="", verbose_name=_("Address Line 2"))
    city = models.CharField(max_length=100, verbose_name=_("City"))
    state_or_province = models.CharField(max_length=100, verbose_name=_("State or Province"))
    postal_code = models.CharField(max_length=50, verbose_name=_("Postal Code"))
    country = models.CharField(max_length=100, verbose_name=_("Country"))

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Order Address Snapshot")
        verbose_name_plural = _("Order Address Snapshots")
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.full_name} - {self.city}, {self.country}"


class Order(models.Model):
    """
    Core Order architecture supporting customer history, guest checkouts, 
    and immutable snapshot auditing for shipping/billing details.
    """
    class OrderStatus(models.TextChoices):
        PENDING = 'pending', _('Pending')
        PROCESSING = 'processing', _('Processing')
        SHIPPED = 'shipped', _('Shipped')
        DELIVERED = 'delivered', _('Delivered')
        CANCELLED = 'cancelled', _('Cancelled')
        REFUNDED = 'refunded', _('Refunded')
        COMPLETED = 'completed', _('Completed')

    class PaymentStatus(models.TextChoices):
        PENDING = 'pending', _('Pending')
        PARTIALLY_PAID = 'partially_paid', _('Partially Paid')
        PAID = 'paid', _('Paid')
        FAILED = 'failed', _('Failed')
        REFUNDED = 'refunded', _('Refunded')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_number = models.CharField(max_length=50, unique=True, db_index=True, verbose_name=_("Order Number"))

    # =================================================
    # CUSTOMER RELATIONSHIP
    # =================================================
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='orders',
        verbose_name=_("Customer"),
        help_text=_("Associated customer account. Will be null for guest checkouts.")
    )
    email = models.EmailField(
        verbose_name=_("Order Email"),
        help_text=_("Email address for order communications, ensuring guest checkout compatibility.")
    )

    # =================================================
    # SHIPPING & BILLING ADDRESS SNAPSHOTS
    # =================================================
    shipping_address = models.OneToOneField(
        OrderAddressSnapshot,
        on_delete=models.PROTECT,
        related_name='shipping_order',
        null=True,
        blank=True,
        verbose_name=_("Shipping Address"),
        help_text=_("Immutable snapshot of the shipping address at checkout.")
    )
    billing_address = models.OneToOneField(
        OrderAddressSnapshot,
        on_delete=models.PROTECT,
        related_name='billing_order',
        null=True,
        blank=True,
        verbose_name=_("Billing Address"),
        help_text=_("Immutable snapshot of the billing address at checkout.")
    )

    # =================================================
    # CORE ORDER STATUS & PAYMENT
    # =================================================
    status = models.CharField(
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
        db_index=True,
        verbose_name=_("Order Status")
    )
    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True,
        verbose_name=_("Payment Status")
    )
    payment_method = models.CharField(max_length=100, blank=True, verbose_name=_("Payment Method"))
    transaction_id = models.CharField(max_length=255, blank=True, verbose_name=_("Transaction ID"))
    currency = models.CharField(max_length=10, default="NPR", verbose_name=_("Currency"))

    # =================================================
    # FINANCIALS
    # =================================================
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Subtotal"))
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Discount Total"))
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Shipping Cost"))
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Tax"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Total"))
    
    coupon_code = models.CharField(max_length=50, blank=True, verbose_name=_("Coupon Code"))

    # =================================================
    # FULFILLMENT & LOGISTICS (High-Level / Cache)
    # =================================================
    tracking_number = models.CharField(max_length=100, blank=True, verbose_name=_("Tracking Number"))
    tracking_url = models.URLField(max_length=500, blank=True, verbose_name=_("Tracking URL"))
    carrier = models.CharField(max_length=100, blank=True, verbose_name=_("Carrier"))
    delivery_instructions = models.TextField(blank=True, verbose_name=_("Delivery Instructions"))
    
    # =================================================
    # METADATA & NOTES
    # =================================================
    customer_note = models.TextField(blank=True, verbose_name=_("Customer Note"))
    invoice_url = models.URLField(max_length=500, blank=True, verbose_name=_("Invoice URL"))
    has_invoice = models.BooleanField(default=False, verbose_name=_("Has Invoice"))
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_("Created At"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated At"))

    class Meta:
        verbose_name = _("Order")
        verbose_name_plural = _("Orders")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', '-created_at']),
            models.Index(fields=['-created_at', 'status']),
            models.Index(fields=['order_number']),
            models.Index(fields=['email']),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(subtotal__gte=0), name='order_subtotal_gte_0'),
            models.CheckConstraint(check=models.Q(total__gte=0), name='order_total_gte_0'),
            models.CheckConstraint(check=models.Q(shipping_cost__gte=0), name='order_shipping_cost_gte_0'),
            models.CheckConstraint(check=models.Q(discount_total__gte=0), name='order_discount_total_gte_0'),
            models.CheckConstraint(check=models.Q(tax__gte=0), name='order_tax_gte_0'),
        ]

    def __str__(self) -> str:
        return f"Order {self.order_number}"

    def clean(self) -> None:
        super().clean()
        if self.subtotal < 0:
            raise ValidationError({'subtotal': _('Subtotal cannot be negative.')})
        if self.total < 0:
            raise ValidationError({'total': _('Total cannot be negative.')})
        if self.discount_total > self.subtotal:
            raise ValidationError({'discount_total': _('Discount cannot exceed the subtotal.')})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def calculate_total(self) -> Decimal:
        """
        Calculates and updates the grand total based on current financial field values.
        """
        self.total = max(Decimal('0.00'), (self.subtotal - self.discount_total) + self.shipping_cost + self.tax)
        return self.total

    def calculate_tax(self, tax_rate: Decimal) -> Decimal:
        """
        Calculates tax based on the subtotal minus discount.
        """
        taxable_amount = max(Decimal('0.00'), self.subtotal - self.discount_total)
        self.tax = (taxable_amount * tax_rate).quantize(Decimal('0.01'))
        return self.tax

    def calculate_discount(self, discount_amount: Decimal) -> Decimal:
        """
        Applies a discount amount to the order safely.
        """
        self.discount_total = min(self.subtotal, discount_amount)
        return self.discount_total

    @transaction.atomic
    def update_status(self, new_status: str, user=None, remarks: str = "") -> None:
        """
        Updates the order status and securely logs the transition in the history table.
        """
        if self.status != new_status:
            old_status = self.status
            self.status = new_status
            self.save(update_fields=['status', 'updated_at'])
            
            OrderStatusHistory.objects.create(
                order=self,
                old_status=old_status,
                new_status=new_status,
                remarks=remarks,
                created_by=user
            )

    def mark_paid(self, transaction_id: str = "", payment_method: str = "") -> None:
        """
        Marks the order as completely paid.
        """
        self.payment_status = self.PaymentStatus.PAID
        if transaction_id:
            self.transaction_id = transaction_id
        if payment_method:
            self.payment_method = payment_method
        self.save(update_fields=['payment_status', 'transaction_id', 'payment_method', 'updated_at'])

    def mark_cancelled(self, user=None, remarks: str = "") -> None:
        """
        Transitions the order safely to a cancelled state.
        """
        self.update_status(self.OrderStatus.CANCELLED, user, remarks)

    def mark_completed(self, user=None, remarks: str = "") -> None:
        """
        Transitions the order to a fully completed status.
        """
        self.update_status(self.OrderStatus.COMPLETED, user, remarks)

    @property
    def grand_total(self) -> Decimal:
        """Alias for total to support flexible templating APIs."""
        return self.total

    @property
    def tax_total(self) -> Decimal:
        """Alias for tax to support flexible templating APIs."""
        return self.tax

    # =================================================
    # TEMPLATE ALIAS PROPERTIES (Shipping & Billing)
    # =================================================
    @property
    def shipping_name(self) -> Optional[str]:
        return self.shipping_address.full_name if self.shipping_address else None

    @property
    def shipping_phone(self) -> Optional[str]:
        return self.shipping_address.phone_number if self.shipping_address else None

    @property
    def shipping_city(self) -> Optional[str]:
        return self.shipping_address.city if self.shipping_address else None

    @property
    def shipping_state(self) -> Optional[str]:
        return self.shipping_address.state_or_province if self.shipping_address else None

    @property
    def shipping_postal_code(self) -> Optional[str]:
        return self.shipping_address.postal_code if self.shipping_address else None

    @property
    def shipping_country(self) -> Optional[str]:
        return self.shipping_address.country if self.shipping_address else None

    @property
    def billing_name(self) -> Optional[str]:
        return self.billing_address.full_name if self.billing_address else None

    @property
    def billing_phone(self) -> Optional[str]:
        return self.billing_address.phone_number if self.billing_address else None

    @property
    def billing_city(self) -> Optional[str]:
        return self.billing_address.city if self.billing_address else None

    @property
    def billing_state(self) -> Optional[str]:
        return self.billing_address.state_or_province if self.billing_address else None

    @property
    def billing_postal_code(self) -> Optional[str]:
        return self.billing_address.postal_code if self.billing_address else None

    @property
    def billing_country(self) -> Optional[str]:
        return self.billing_address.country if self.billing_address else None


class OrderItem(models.Model):
    """
    Stores individual items within an order.
    Includes product snapshots (name, sku, price, variant) to protect against
    catalog changes or product deletions after purchase.
    """
    class ItemStatus(models.TextChoices):
        ACTIVE = 'active', _('Active')
        CANCELLED = 'cancelled', _('Cancelled')
        RETURNED = 'returned', _('Returned')

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name=_("Order")
    )
    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_items',
        verbose_name=_("Product")
    )
    variant = models.ForeignKey(
        'catalog.ProductVariant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_items',
        verbose_name=_("Product Variant")
    )
    
    # Snapshot fields for audit and history integrity
    product_name = models.CharField(max_length=255, verbose_name=_("Product Name"))
    product_sku = models.CharField(max_length=100, blank=True, null=True, verbose_name=_("Product SKU"))
    variant_name = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("Variant Name"))
    
    price = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Price at Purchase"))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Discount"))
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Tax"))
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)], verbose_name=_("Quantity"))
    total = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Line Total"))
    
    weight = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal('0.000'), verbose_name=_("Weight"))
    attributes = models.JSONField(default=dict, blank=True, verbose_name=_("Selected Attributes"))
    
    status = models.CharField(
        max_length=20,
        choices=ItemStatus.choices,
        default=ItemStatus.ACTIVE,
        db_index=True,
        verbose_name=_("Item Status")
    )

    class Meta:
        verbose_name = _("Order Item")
        verbose_name_plural = _("Order Items")
        ordering = ['id']
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gte=1), name='orderitem_quantity_gte_1'),
            models.CheckConstraint(check=models.Q(price__gte=0), name='orderitem_price_gte_0'),
            models.CheckConstraint(check=models.Q(total__gte=0), name='orderitem_total_gte_0'),
        ]

    def __str__(self) -> str:
        return f"{self.quantity}x {self.product_name} ({self.order.order_number})"

    def clean(self) -> None:
        super().clean()
        if self.price < 0:
            raise ValidationError({'price': _('Price cannot be negative.')})
        if self.quantity < 1:
            raise ValidationError({'quantity': _('Quantity must be at least 1.')})
        if self.discount > (self.price * self.quantity):
            raise ValidationError({'discount': _('Discount cannot exceed the gross line total.')})

    def calculate_total(self) -> Decimal:
        """
        Computes the definitive line total.
        """
        gross = self.price * Decimal(self.quantity)
        self.total = max(Decimal('0.00'), gross - self.discount + self.tax)
        return self.total
        
    def save(self, *args, **kwargs) -> None:
        self.clean()
        self.calculate_total()
        super().save(*args, **kwargs)

    @property
    def unit_price(self) -> Decimal:
        """Template alias ensuring pricing matrices resolve smoothly."""
        return self.price


class OrderStatusHistory(models.Model):
    """
    Immutable ledger of order status changes over time.
    """
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='status_history',
        verbose_name=_("Order")
    )
    old_status = models.CharField(max_length=50, verbose_name=_("Previous Status"))
    new_status = models.CharField(max_length=50, verbose_name=_("New Status"))
    remarks = models.TextField(blank=True, verbose_name=_("Remarks"))
    
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='order_status_changes',
        verbose_name=_("Changed By")
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Order Status History")
        verbose_name_plural = _("Order Status Histories")
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.order.order_number}: {self.old_status} -> {self.new_status}"


class Shipment(models.Model):
    """
    Tracks logistical delivery parcels associated with an Order.
    Supports split fulfillments with multiple shipments per order.
    """
    class ShipmentStatus(models.TextChoices):
        PENDING = 'pending', _('Pending')
        DISPATCHED = 'dispatched', _('Dispatched')
        IN_TRANSIT = 'in_transit', _('In Transit')
        DELIVERED = 'delivered', _('Delivered')
        RETURNED = 'returned', _('Returned')

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='shipments',
        verbose_name=_("Order")
    )
    shipment_number = models.CharField(max_length=100, unique=True, db_index=True, verbose_name=_("Shipment Number"))
    carrier = models.CharField(max_length=100, verbose_name=_("Carrier"))
    tracking_number = models.CharField(max_length=150, blank=True, db_index=True, verbose_name=_("Tracking Number"))
    tracking_url = models.URLField(max_length=500, blank=True, verbose_name=_("Tracking URL"))
    warehouse = models.CharField(max_length=150, blank=True, verbose_name=_("Origin Warehouse"))
    
    status = models.CharField(
        max_length=30,
        choices=ShipmentStatus.choices,
        default=ShipmentStatus.PENDING,
        db_index=True,
        verbose_name=_("Shipment Status")
    )
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'), verbose_name=_("Shipping Cost"))
    
    dispatch_date = models.DateTimeField(null=True, blank=True, verbose_name=_("Dispatch Date"))
    delivery_date = models.DateTimeField(null=True, blank=True, verbose_name=_("Delivery Date"))
    notes = models.TextField(blank=True, verbose_name=_("Shipment Notes"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Shipment")
        verbose_name_plural = _("Shipments")
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"Shipment {self.shipment_number} ({self.get_status_display()})"

    def mark_dispatched(self) -> None:
        """Registers dispatch timeframe securely."""
        self.status = self.ShipmentStatus.DISPATCHED
        if not self.dispatch_date:
            self.dispatch_date = timezone.now()
        self.save(update_fields=['status', 'dispatch_date', 'updated_at'])

    def mark_delivered(self) -> None:
        """Registers exact physical delivery completion."""
        self.status = self.ShipmentStatus.DELIVERED
        if not self.delivery_date:
            self.delivery_date = timezone.now()
        self.save(update_fields=['status', 'delivery_date', 'updated_at'])


class Payment(models.Model):
    """
    Maintains financial transaction records mapped to an Order.
    Supports multi-gateway abstraction for scale.
    """
    class PaymentState(models.TextChoices):
        PENDING = 'pending', _('Pending')
        COMPLETED = 'completed', _('Completed')
        FAILED = 'failed', _('Failed')
        REFUNDED = 'refunded', _('Refunded')

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='payments',
        verbose_name=_("Order")
    )
    transaction_id = models.CharField(max_length=255, unique=True, db_index=True, verbose_name=_("Transaction ID"))
    gateway = models.CharField(max_length=100, verbose_name=_("Payment Gateway"), help_text=_("e.g. Stripe, PayPal, Razorpay"))
    
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Amount"))
    currency = models.CharField(max_length=10, default='NPR', verbose_name=_("Currency"))
    
    status = models.CharField(
        max_length=20,
        choices=PaymentState.choices,
        default=PaymentState.PENDING,
        db_index=True,
        verbose_name=_("Status")
    )
    
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Paid At"))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_("Gateway Metadata"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Payment")
        verbose_name_plural = _("Payments")
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f"{self.gateway} - {self.transaction_id} ({self.amount} {self.currency})"

    @transaction.atomic
    def capture(self) -> None:
        """Registers a successful payment capture and delegates state up to the Order."""
        if self.status != self.PaymentState.COMPLETED:
            self.status = self.PaymentState.COMPLETED
            self.paid_at = timezone.now()
            self.save(update_fields=['status', 'paid_at', 'updated_at'])
            self.order.mark_paid(transaction_id=self.transaction_id, payment_method=self.gateway)

    @transaction.atomic
    def refund(self) -> None:
        """Declares the payment fully refunded."""
        if self.status != self.PaymentState.REFUNDED:
            self.status = self.PaymentState.REFUNDED
            self.save(update_fields=['status', 'updated_at'])

    def verify(self) -> bool:
        """
        Placeholder logic to represent webhook/gateway verifications.
        """
        return self.status == self.PaymentState.COMPLETED


class Refund(models.Model):
    """
    Formal record structure for reversing financial transitions against specific Payments.
    """
    class RefundStatus(models.TextChoices):
        REQUESTED = 'requested', _('Requested')
        APPROVED = 'approved', _('Approved')
        PROCESSED = 'processed', _('Processed')
        REJECTED = 'rejected', _('Rejected')

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='refunds',
        verbose_name=_("Order")
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        related_name='refunds',
        verbose_name=_("Original Payment")
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Refund Amount"))
    reason = models.TextField(verbose_name=_("Refund Reason"))
    
    status = models.CharField(
        max_length=20,
        choices=RefundStatus.choices,
        default=RefundStatus.REQUESTED,
        db_index=True,
        verbose_name=_("Status")
    )
    
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_refunds',
        verbose_name=_("Approved By")
    )
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Processed At"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Refund")
        verbose_name_plural = _("Refunds")
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(check=models.Q(amount__gt=0), name='refund_amount_gt_0'),
        ]

    def __str__(self) -> str:
        return f"Refund {self.id} for {self.order.order_number}"

    def approve(self, user) -> None:
        """Administratively clears the refund for processing."""
        if self.status == self.RefundStatus.REQUESTED:
            self.status = self.RefundStatus.APPROVED
            self.approved_by = user
            self.save(update_fields=['status', 'approved_by', 'updated_at'])

    def reject(self) -> None:
        """Halts the refund process."""
        if self.status in [self.RefundStatus.REQUESTED, self.RefundStatus.APPROVED]:
            self.status = self.RefundStatus.REJECTED
            self.save(update_fields=['status', 'updated_at'])

    @transaction.atomic
    def process(self) -> None:
        """Executes the final local data completion phase of the refund."""
        if self.status == self.RefundStatus.APPROVED:
            self.status = self.RefundStatus.PROCESSED
            self.processed_at = timezone.now()
            self.save(update_fields=['status', 'processed_at', 'updated_at'])
            self.payment.refund()


class CouponUsage(models.Model):
    """
    Log of promotional discounts securely tethered to specific users and orders.
    Enforces uniqueness when standard business logic requires one-time use per customer.
    """
    coupon_code = models.CharField(max_length=50, db_index=True, verbose_name=_("Coupon Code"))
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='coupon_usages',
        verbose_name=_("User")
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='coupon_usages',
        verbose_name=_("Order")
    )
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Discount Applied"))
    used_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_("Used At"))

    class Meta:
        verbose_name = _("Coupon Usage")
        verbose_name_plural = _("Coupon Usages")
        ordering = ['-used_at']
        constraints = [
            # Business rule mapping: Prevents an individual user from applying the same coupon twice.
            models.UniqueConstraint(fields=['user', 'coupon_code'], name='unique_coupon_per_user'),
            models.CheckConstraint(check=models.Q(discount_amount__gte=0), name='coupon_discount_gte_0'),
        ]

    def __str__(self) -> str:
        return f"Coupon {self.coupon_code} used by {self.user.username}"