import logging
import secrets
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

from django.db import transaction, models
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.orders.models import (
    Order,
    OrderAddressSnapshot,
    OrderItem,
    OrderStatusHistory,
    Payment,
    Refund,
    Shipment,
    CouponUsage,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# 1. ORDER ADDRESS & SNAPSHOT SERVICES
# ==============================================================================

def create_order_address_snapshot(address_data: Dict[str, Any]) -> OrderAddressSnapshot:
    """
    Creates an immutable snapshot of an address for historical order accuracy.
    """
    return OrderAddressSnapshot.objects.create(
        full_name=address_data.get('full_name', ''),
        phone_number=address_data.get('phone_number', ''),
        company=address_data.get('company', ''),
        address_line_1=address_data.get('address_line_1', ''),
        address_line_2=address_data.get('address_line_2', ''),
        city=address_data.get('city', ''),
        state_or_province=address_data.get('state_or_province', ''),
        postal_code=address_data.get('postal_code', ''),
        country=address_data.get('country', '')
    )


# ==============================================================================
# 2. INVENTORY MANAGEMENT SERVICES
# ==============================================================================

def deduct_inventory(order_items: Iterable[OrderItem]) -> None:
    """
    Deducts inventory stock securely, validating availability to prevent overselling.
    Only applies numeric deduction to ProductVariants, as Product relies on status strings.
    """
    for item in order_items:
        if item.variant:
            if item.variant.stock_quantity < item.quantity:
                raise ValidationError(
                    _(f"Insufficient stock for variant {item.variant.name}. "
                      f"Requested: {item.quantity}, Available: {item.variant.stock_quantity}")
                )
            item.variant.stock_quantity = models.F('stock_quantity') - item.quantity
            item.variant.save(update_fields=['stock_quantity'])


def release_inventory(order_items: Iterable[OrderItem]) -> None:
    """
    Restores inventory stock levels upon order cancellation or refund.
    """
    for item in order_items:
        if item.variant:
            item.variant.stock_quantity = models.F('stock_quantity') + item.quantity
            item.variant.save(update_fields=['stock_quantity'])


# ==============================================================================
# 3. CORE ORDER CREATION & LIFECYCLE SERVICES
# ==============================================================================

def generate_order_number() -> str:
    """
    Generates a unique order number sequence (e.g., ORD-260415-A1B2C3).
    """
    prefix = timezone.now().strftime('%y%m%d')
    random_suffix = secrets.token_hex(3).upper()
    return f"ORD-{prefix}-{random_suffix}"


@transaction.atomic
def create_order(
    user: Optional[Any],
    email: str,
    shipping_address_data: Dict[str, Any],
    billing_address_data: Optional[Dict[str, Any]],
    cart_items: Iterable[Any],
    shipping_cost: Decimal = Decimal('0.00'),
    tax_rate: Decimal = Decimal('0.00'),
    coupon_code: str = "",
    discount_amount: Decimal = Decimal('0.00'),
    customer_note: str = "",
    currency: str = "NPR"
) -> Order:
    """
    Centralized service for Order creation.
    Orchestrates address snapshotting, order item translation, pricing computations, 
    inventory deduction, and coupon validation inside a guaranteed atomic transaction.
    """
    # 1. Validate & Create Address Snapshots
    shipping_snapshot = create_order_address_snapshot(shipping_address_data)
    
    if billing_address_data:
        billing_snapshot = create_order_address_snapshot(billing_address_data)
    else:
        # Create a distinct physical snapshot to satisfy the OneToOne constraint
        billing_snapshot = create_order_address_snapshot(shipping_address_data)

    # 2. Coupon Pre-validation
    if coupon_code and user and user.is_authenticated:
        if CouponUsage.objects.filter(user=user, coupon_code=coupon_code).exists():
            raise ValidationError(_(f"Coupon '{coupon_code}' has already been used by this account."))

    # 3. Initialize Order
    order = Order(
        order_number=generate_order_number(),
        customer=user if user and user.is_authenticated else None,
        email=email,
        shipping_address=shipping_snapshot,
        billing_address=billing_snapshot,
        shipping_cost=shipping_cost,
        customer_note=customer_note,
        currency=currency,
        status=Order.OrderStatus.PENDING,
        payment_status=Order.PaymentStatus.PENDING
    )
    order.save()

    # 4. Process Cart Items to Order Items
    subtotal = Decimal('0.00')
    order_items_to_create = []

    for item in cart_items:
        product = item.product
        variant = getattr(item, 'variant', None)

        if not product.is_active:
            raise ValidationError(_(f"Product '{product.title}' is no longer active."))

        # Resolve Price
        if variant and variant.price_override is not None:
            unit_price = variant.price_override
        else:
            unit_price = product.price

        # Basic stock availability sanity check
        if variant and variant.stock_quantity < item.quantity:
            raise ValidationError(_(f"Insufficient stock for {product.title} ({variant.name})."))
        elif not variant and product.stock_status == 'out':
            raise ValidationError(_(f"Product '{product.title}' is currently out of stock."))

        line_total = unit_price * Decimal(item.quantity)
        subtotal += line_total

        oi = OrderItem(
            order=order,
            product=product,
            variant=variant,
            product_name=product.title,
            product_sku=product.sku if not variant else (variant.sku or product.sku),
            variant_name=variant.name if variant else "",
            price=unit_price,
            quantity=item.quantity,
            total=line_total,
            weight=product.weight or Decimal('0.000')
        )
        order_items_to_create.append(oi)

    if not order_items_to_create:
        raise ValidationError(_("Cannot create an order without items."))

    OrderItem.objects.bulk_create(order_items_to_create)

    # 5. Inventory Deduction
    deduct_inventory(order_items_to_create)

    # 6. Apply Financials
    order.subtotal = subtotal

    if coupon_code and discount_amount > 0:
        order.coupon_code = coupon_code
        order.calculate_discount(discount_amount)
        if user and user.is_authenticated:
            CouponUsage.objects.create(
                coupon_code=coupon_code,
                user=user,
                order=order,
                discount_amount=discount_amount
            )

    order.calculate_tax(tax_rate)
    order.calculate_total()
    order.save()

    # 7. Register Initial State
    OrderStatusHistory.objects.create(
        order=order,
        old_status='',
        new_status=order.status,
        remarks=_("Order created successfully."),
        created_by=user if user and user.is_authenticated else None
    )

    logger.info(f"Order {order.order_number} created successfully with total {order.total} {order.currency}.")
    return order


@transaction.atomic
def update_order_status(order: Order, new_status: str, user: Optional[Any] = None, remarks: str = "") -> Order:
    """
    Safely transitions an order to a new state, enforcing business logic constraints.
    """
    if order.status == new_status:
        return order

    if order.status in [Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED]:
        raise ValidationError(_("Cannot update status of an already cancelled or refunded order."))

    order.update_status(new_status, user=user, remarks=remarks)
    logger.info(f"Order {order.order_number} status updated to {new_status}.")
    return order


@transaction.atomic
def cancel_order(order: Order, user: Optional[Any] = None, remarks: str = "Customer requested cancellation") -> Order:
    """
    Cancels an active order, releasing allocated inventory and logging the transition.
    """
    if order.status in [Order.OrderStatus.CANCELLED, Order.OrderStatus.SHIPPED, Order.OrderStatus.DELIVERED, Order.OrderStatus.COMPLETED]:
        raise ValidationError(_("Order cannot be cancelled in its current state."))

    order.mark_cancelled(user=user, remarks=remarks)
    release_inventory(order.items.all())
    
    logger.info(f"Order {order.order_number} cancelled. Inventory released.")
    return order


@transaction.atomic
def complete_order(order: Order, user: Optional[Any] = None, remarks: str = "Order marked as completed") -> Order:
    """
    Finalizes an order lifecycle (usually post-delivery).
    """
    if order.status != Order.OrderStatus.DELIVERED:
        raise ValidationError(_("Order must be delivered before it can be marked as completed."))

    order.mark_completed(user=user, remarks=remarks)
    logger.info(f"Order {order.order_number} completed.")
    return order


# ==============================================================================
# 4. PAYMENT & REFUND SERVICES
# ==============================================================================

@transaction.atomic
def register_payment(
    order: Order, 
    transaction_id: str, 
    gateway: str, 
    amount: Decimal, 
    currency: str = 'NPR', 
    metadata: Optional[Dict[str, Any]] = None
) -> Payment:
    """
    Initializes a new payment intention against an order.
    """
    if order.payment_status == Order.PaymentStatus.PAID:
        raise ValidationError(_("Order is already paid."))

    payment = Payment.objects.create(
        order=order,
        transaction_id=transaction_id,
        gateway=gateway,
        amount=amount,
        currency=currency,
        status=Payment.PaymentState.PENDING,
        metadata=metadata or {}
    )
    logger.info(f"Payment intention {transaction_id} registered for Order {order.order_number}.")
    return payment


@transaction.atomic
def capture_payment(payment: Payment) -> Payment:
    """
    Marks a pending payment as completed and delegates the updated status back to the order.
    """
    if payment.status == Payment.PaymentState.COMPLETED:
        return payment

    payment.capture()
    logger.info(f"Payment {payment.transaction_id} captured successfully.")
    return payment


@transaction.atomic
def request_refund(order: Order, payment: Payment, amount: Decimal, reason: str) -> Refund:
    """
    Initiates a formal refund request against an established payment record.
    """
    if payment.order_id != order.id:
        raise ValidationError(_("Payment record does not belong to this order."))
    if payment.status != Payment.PaymentState.COMPLETED:
        raise ValidationError(_("Only completed payments can be refunded."))

    refund = Refund.objects.create(
        order=order,
        payment=payment,
        amount=amount,
        reason=reason,
        status=Refund.RefundStatus.REQUESTED
    )
    logger.info(f"Refund of {amount} requested for Payment {payment.transaction_id}.")
    return refund


@transaction.atomic
def process_refund(refund: Refund, approved_by_user: Any) -> Refund:
    """
    Administratively approves and finalizes a refund, propagating states downward.
    """
    if refund.status != Refund.RefundStatus.REQUESTED:
        raise ValidationError(_("Only newly requested refunds can be processed this way."))

    refund.approve(approved_by_user)
    refund.process()
    
    # Cascade to Order Status if fully refunded
    order = refund.order
    total_refunded = Refund.objects.filter(
        order=order, status=Refund.RefundStatus.PROCESSED
    ).aggregate(t=models.Sum('amount'))['t'] or Decimal('0.00')
    
    if total_refunded >= order.total:
        order.payment_status = Order.PaymentStatus.REFUNDED
        order.save(update_fields=['payment_status'])
        if order.status not in [Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED]:
            update_order_status(order, Order.OrderStatus.REFUNDED, user=approved_by_user, remarks="Fully Refunded")

    logger.info(f"Refund {refund.id} processed successfully by {approved_by_user.username}.")
    return refund


# ==============================================================================
# 5. SHIPMENT & LOGISTICS SERVICES
# ==============================================================================

import secrets

def generate_shipment_number() -> str:
    """
    Generates a unique tracking/shipment internal reference.
    """
    return f"SHP-{timezone.now().strftime('%y%m%d')}-{secrets.token_hex(4).upper()}"


@transaction.atomic
def create_shipment(
    order: Order, 
    carrier: str, 
    tracking_number: str = "", 
    tracking_url: str = "", 
    warehouse: str = "", 
    shipping_cost: Decimal = Decimal('0.00'), 
    notes: str = ""
) -> Shipment:
    """
    Provisions a logistical shipment envelope for an order.
    """
    if order.status in [Order.OrderStatus.CANCELLED, Order.OrderStatus.REFUNDED]:
        raise ValidationError(_("Cannot create a shipment for cancelled or refunded orders."))

    shipment = Shipment.objects.create(
        order=order,
        shipment_number=generate_shipment_number(),
        carrier=carrier,
        tracking_number=tracking_number,
        tracking_url=tracking_url,
        warehouse=warehouse,
        shipping_cost=shipping_cost,
        notes=notes,
        status=Shipment.ShipmentStatus.PENDING
    )
    
    # Mirror tracking references back up to the parent Order for ease of customer access
    if tracking_number and not order.tracking_number:
        order.tracking_number = tracking_number
        order.tracking_url = tracking_url
        order.carrier = carrier
        order.save(update_fields=['tracking_number', 'tracking_url', 'carrier'])

    logger.info(f"Shipment {shipment.shipment_number} created for Order {order.order_number}.")
    return shipment


@transaction.atomic
def dispatch_shipment(shipment: Shipment, user: Optional[Any] = None) -> Shipment:
    """
    Marks a shipment as physically dispatched and ensures parent order shifts to SHIPPED.
    """
    shipment.mark_dispatched()
    
    if shipment.order.status in [Order.OrderStatus.PENDING, Order.OrderStatus.PROCESSING]:
        update_order_status(
            shipment.order, 
            Order.OrderStatus.SHIPPED, 
            user=user, 
            remarks=f"Shipment {shipment.shipment_number} dispatched via {shipment.carrier}."
        )

    logger.info(f"Shipment {shipment.shipment_number} marked as dispatched.")
    return shipment


@transaction.atomic
def deliver_shipment(shipment: Shipment, user: Optional[Any] = None) -> Shipment:
    """
    Marks a shipment as delivered and ensures parent order shifts to DELIVERED.
    """
    shipment.mark_delivered()
    
    # Check if all shipments are delivered to mark the entire order delivered
    pending_shipments = shipment.order.shipments.exclude(status=Shipment.ShipmentStatus.DELIVERED).exists()
    
    if not pending_shipments and shipment.order.status != Order.OrderStatus.DELIVERED:
        update_order_status(
            shipment.order, 
            Order.OrderStatus.DELIVERED, 
            user=user, 
            remarks=f"Final shipment {shipment.shipment_number} confirmed delivered."
        )

    logger.info(f"Shipment {shipment.shipment_number} marked as delivered.")
    return shipment