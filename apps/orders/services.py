from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.orders import constants as c
from apps.orders.models import (
    CouponUsage,
    DiscountLine,
    Order,
    OrderAddressSnapshot,
    OrderItem,
    OrderNote,
    OrderStatusHistory,
    Payment,
    PaymentAttempt,
    Refund,
    ReturnRequest,
    Shipment,
    ShipmentItem,
    TaxLine,
)

logger = logging.getLogger(c.LOGGER_SERVICES)

def _generate_order_number() -> str:
    prefix = c.ORDER_NUMBER_PREFIX
    date_part = timezone.now().strftime(c.RETURN_NUMBER_DATE_FORMAT)
    for _ in range(3):
        suffix = secrets.token_hex(c.RETURN_NUMBER_TOKEN_BYTES).upper()
        candidate = f"{prefix}-{date_part}-{suffix}"
        if not Order.objects.filter(order_number=candidate).exists():
            return candidate
    return f"{prefix}-{date_part}-{secrets.token_hex(4).upper()}"

def _generate_shipment_number() -> str:
    prefix = c.SHIPMENT_NUMBER_PREFIX
    date_part = timezone.now().strftime(c.RETURN_NUMBER_DATE_FORMAT)
    for _ in range(3):
        suffix = secrets.token_hex(4).upper()
        candidate = f"{prefix}-{date_part}-{suffix}"
        if not Shipment.objects.filter(shipment_number=candidate).exists():
            return candidate
    return f"{prefix}-{date_part}-{secrets.token_hex(6).upper()}"

def generate_unique_order_number() -> str:
    return _generate_order_number()

@transaction.atomic
def create_address_snapshot(
    *,
    full_name: str,
    phone_number: str,
    address_line_1: str,
    city: str,
    state_or_province: str,
    postal_code: str,
    country: str,
    company: str = "",
    address_line_2: str = "",
    country_code: str = "",
    phone_e164: str = "",
    latitude: Optional[Decimal] = None,
    longitude: Optional[Decimal] = None,
    delivery_notes: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> OrderAddressSnapshot:
    snapshot = OrderAddressSnapshot(
        full_name=full_name,
        phone_number=phone_number,
        company=company,
        address_line_1=address_line_1,
        address_line_2=address_line_2,
        city=city,
        state_or_province=state_or_province,
        postal_code=postal_code,
        country=country,
        country_code=country_code,
        phone_e164=phone_e164,
        latitude=latitude,
        longitude=longitude,
        delivery_notes=delivery_notes,
        metadata=metadata or {},
    )
    snapshot.save()
    return snapshot

@transaction.atomic
def create_order(
    *,
    email: str,
    shipping_snapshot: OrderAddressSnapshot,
    order_number: Optional[str] = None,
    customer: Optional[Any] = None,
    billing_snapshot: Optional[OrderAddressSnapshot] = None,
    currency: str = c.DEFAULT_CURRENCY_CODE,
    customer_note: str = "",
    source: str = Order.Source.WEB,
    shipping_cost: Decimal = c.ZERO_DECIMAL_2,
    tax_total: Decimal = c.ZERO_DECIMAL_2,
    discount_total: Decimal = c.ZERO_DECIMAL_2,
    is_gift: bool = False,
    gift_message: str = "",
    gift_wrapping: str = "",
    personalization_data: Optional[Dict[str, Any]] = None,
    customer_ip: Optional[str] = None,
    customer_user_agent: str = "",
    customer_locale: str = "",
    customer_timezone: str = "",
    referrer_url: str = "",
    is_active: bool = c.DEFAULT_ORDER_ACTIVE_STATE,
    status: str = Order.OrderStatus.PENDING,
    payment_status: str = Order.PaymentStatus.PENDING,
    payment_method: str = "",
    transaction_id: str = "",
    json_metadata: Optional[Dict[str, Any]] = None,
    notes: str = "",
    tags: Optional[List[str]] = None,
) -> Order:
    if not order_number:
        order_number = _generate_order_number()
    elif Order.objects.filter(order_number=order_number).exists():
        raise ValueError(_("Order number already in use."))

    order = Order.objects.create(
        order_number=order_number,
        customer=customer if customer and getattr(customer, "is_authenticated", False) else None,
        email=email,
        shipping_address=shipping_snapshot,
        billing_address=billing_snapshot or shipping_snapshot,
        status=status,
        payment_status=payment_status,
        payment_method=payment_method,
        transaction_id=transaction_id,
        currency=currency,
        shipping_cost=shipping_cost,
        tax_total=tax_total,
        discount_total=discount_total,
        customer_note=customer_note,
        is_active=is_active,
        json_metadata=json_metadata or {},
        is_gift=is_gift,
        gift_message=gift_message,
        gift_wrapping=gift_wrapping,
        personalization_data=personalization_data or {},
        source=source,
        customer_ip=customer_ip,
        customer_user_agent=customer_user_agent,
        customer_locale=customer_locale,
        customer_timezone=customer_timezone,
        referrer_url=referrer_url,
        notes=notes,
        tags=tags or [],
    )

    OrderStatusHistory.objects.create(
        order=order,
        old_status="",
        new_status=order.status,
        remarks=_("Order created."),
        created_by=order.customer,
    )
    return order

@transaction.atomic
def add_order_item(
    *,
    order: Order,
    product: Optional[Any] = None,
    variant: Optional[Any] = None,
    product_name: str = "",
    product_sku: str = "",
    variant_name: str = "",
    unit_price: Decimal = c.ZERO_DECIMAL_2,
    quantity: int = c.DEFAULT_QUANTITY,
    discount: Decimal = c.ZERO_DECIMAL_2,
    tax: Decimal = c.ZERO_DECIMAL_2,
    weight: Decimal = c.ZERO_DECIMAL_3,
    attributes: Optional[Dict[str, Any]] = None,
    personalization: Optional[Dict[str, Any]] = None,
    status: str = OrderItem.ItemStatus.ACTIVE,
    saved_reason: str = "",
    is_gift: bool = False,
    gift_message: str = "",
    gift_wrapping: str = "",
    expected_ship_date: Optional[Any] = None,
    promised_delivery_date: Optional[Any] = None,
    supplier_name: str = "",
    supplier_order_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> OrderItem:
    line_total = max(c.ZERO_DECIMAL_2, (unit_price * Decimal(quantity)) + tax - discount)

    item = OrderItem.objects.create(
        order=order,
        product=product,
        variant=variant,
        product_name_snapshot=product_name or getattr(product, "title", "") or "",
        product_sku_snapshot=product_sku or getattr(product, "sku", "") or "",
        variant_name_snapshot=variant_name or (getattr(variant, "name", "") if variant else ""),
        unit_price=unit_price,
        discount=discount,
        tax=tax,
        line_total=line_total,
        weight=weight,
        attributes=attributes or {},
        personalization=personalization or {},
        quantity=quantity,
        status=status,
        saved_reason=saved_reason,
        is_gift=is_gift,
        gift_message=gift_message,
        gift_wrapping=gift_wrapping,
        expected_ship_date=expected_ship_date,
        promised_delivery_date=promised_delivery_date,
        supplier_name_snapshot=supplier_name,
        supplier_order_id=supplier_order_id,
        metadata=metadata or {},
    )
    return item

@transaction.atomic
def bulk_add_order_items(order: Order, items_data: Iterable[Dict[str, Any]]) -> List[OrderItem]:
    created = []
    for d in items_data:
        data = dict(d)
        data.pop("order", None)
        created.append(add_order_item(order=order, **data))
    return created

@transaction.atomic
def update_order_item_quantity(item: OrderItem, new_quantity: int) -> OrderItem:
    if new_quantity < c.MIN_QUANTITY:
        raise ValueError(_(f"Quantity must be >= {c.MIN_QUANTITY}."))
    item.quantity = new_quantity
    item.line_total = max(c.ZERO_DECIMAL_2, (item.unit_price * Decimal(new_quantity)) + item.tax - item.discount)
    item.save(update_fields=["quantity", "line_total", "updated_at"])
    return item

@transaction.atomic
def update_order_item_status(item: OrderItem, new_status: str) -> OrderItem:
    item.status = new_status
    item.save(update_fields=["status", "updated_at"])
    return item

@transaction.atomic
def delete_order_item(item: OrderItem) -> None:
    if item.status in {OrderItem.ItemStatus.PARTIALLY_SHIPPED, "shipped"}:
        raise ValueError(_("Cannot delete shipped item."))
    item.delete()

@transaction.atomic
def recalculate_order_totals(order: Order) -> Order:
    subtotal = order.items.filter(status=OrderItem.ItemStatus.ACTIVE).aggregate(
        computed_subtotal=Sum(F("unit_price") * F("quantity"))
    )["computed_subtotal"] or c.ZERO_DECIMAL_2

    order.subtotal = subtotal
    order.total = max(
        c.ZERO_DECIMAL_2,
        order.subtotal - (order.discount_total or c.ZERO_DECIMAL_2)
        + (order.shipping_cost or c.ZERO_DECIMAL_2)
        + (order.tax_total or c.ZERO_DECIMAL_2),
    )
    order.save(update_fields=["subtotal", "total", "updated_at"])
    return order

@transaction.atomic
def update_order_status(
    order: Order,
    new_status: str,
    user: Optional[Any] = None,
    remarks: str = "",
) -> Order:
    old_status = order.status
    order.status = new_status
    if new_status in c.OrderStatus.TERMINAL_SUCCESS and not order.completed_at:
        order.completed_at = timezone.now()

    order.save(update_fields=["status", "completed_at", "updated_at"])
    OrderStatusHistory.objects.create(
        order=order,
        old_status=old_status,
        new_status=new_status,
        remarks=remarks,
        created_by=user,
    )
    return order

@transaction.atomic
def cancel_order(order: Order, user: Optional[Any] = None, remarks: str = _("Order cancelled.")) -> Order:
    if order.status not in c.OrderStatus.CANCELLABLE_FROM:
        raise ValueError(_("Order is not in a cancellable state."))
    return update_order_status(order=order, new_status=Order.OrderStatus.CANCELLED, user=user, remarks=remarks)

@transaction.atomic
def complete_order(order: Order, user: Optional[Any] = None, remarks: str = _("Order completed.")) -> Order:
    return update_order_status(order=order, new_status=Order.OrderStatus.COMPLETED, user=user, remarks=remarks)

@transaction.atomic
def hold_order(order: Order, user: Optional[Any] = None, remarks: str = _("Order placed on hold.")) -> Order:
    return update_order_status(order=order, new_status=Order.OrderStatus.ON_HOLD, user=user, remarks=remarks)

@transaction.atomic
def resume_order(order: Order, user: Optional[Any] = None, remarks: str = _("Order resumed.")) -> Order:
    return update_order_status(order=order, new_status=Order.OrderStatus.PROCESSING, user=user, remarks=remarks)

@transaction.atomic
def mark_order_paid(order: Order, payment_method: str = "", transaction_id: str = "", user: Optional[Any] = None) -> Order:
    order.payment_status = Order.PaymentStatus.PAID
    if payment_method:
        order.payment_method = payment_method
    if transaction_id:
        order.transaction_id = transaction_id
    order.save(update_fields=["payment_status", "payment_method", "transaction_id", "updated_at"])
    OrderStatusHistory.objects.create(
        order=order,
        old_status=order.status,
        new_status=order.status,
        remarks=_("Payment captured."),
        created_by=user,
    )
    return order

@transaction.atomic
def reorder_items_into_cart(order: Order, user: Any = None, session_key: str = "") -> Any:
    from apps.cart.services.cart_core import CartService
    from apps.cart.models import CartItem

    class DummyReq:
        def __init__(self, user, session_key):
            self.user = user
            self.session = type("S", (), {"session_key": session_key, "create": lambda: None})()

    req = DummyReq(user, session_key)
    cart, _ = CartService.get_or_create_cart(req)

    active_items = order.items.filter(status=OrderItem.ItemStatus.ACTIVE)
    for item in active_items:
        if item.product_id:
            cart_item, created = CartItem.objects.get_or_create(
                cart=cart,
                product_id=item.product_id,
                variant_id=item.variant_id,
                status=CartItem.ItemStatus.ACTIVE,
                defaults={
                    "quantity": item.quantity,
                    "unit_price_snapshot": item.unit_price,
                    "product_name_snapshot": item.product_name_snapshot,
                    "variant_name_snapshot": item.variant_name_snapshot,
                },
            )
            if not created:
                cart_item.quantity += item.quantity
                cart_item.save(update_fields=["quantity", "updated_at"])
    cart.touch()
    return cart

@transaction.atomic
def create_payment(
    *,
    order: Order,
    transaction_id: str,
    gateway: str,
    amount: Decimal,
    currency: str = c.DEFAULT_CURRENCY_CODE,
    payment_method: str = "",
    status: str = Payment.PaymentState.PENDING,
    paid_at: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None,
    risk_score: Optional[Decimal] = None,
    is_test_payment: bool = False,
) -> Payment:
    return Payment.objects.create(
        order=order,
        transaction_id=transaction_id,
        gateway=gateway,
        amount=amount,
        currency=currency,
        status=status,
        paid_at=paid_at,
        payment_method=payment_method,
        risk_score=risk_score,
        is_test_payment=is_test_payment,
        metadata=metadata or {},
    )

@transaction.atomic
def update_payment_status(payment: Payment, new_status: str, paid_at: Optional[Any] = None) -> Payment:
    payment.status = new_status
    if new_status in {Payment.PaymentState.CAPTURED, Payment.PaymentState.COMPLETED}:
        payment.paid_at = paid_at or timezone.now()
        mark_order_paid(
            order=payment.order,
            payment_method=payment.payment_method,
            transaction_id=payment.transaction_id,
        )
    payment.save(update_fields=["status", "paid_at", "updated_at"])
    return payment

@transaction.atomic
def create_refund(
    *,
    order: Order,
    payment: Payment,
    amount: Decimal,
    reason: str,
    refund_method: str = Refund.RefundMethod.ORIGINAL,
    refund_reason_category: str = "",
    customer_notes: str = "",
    internal_notes: str = "",
    evidence_images: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Refund:
    return Refund.objects.create(
        order=order,
        payment=payment,
        amount=amount,
        reason=reason,
        refund_method=refund_method,
        refund_reason_category=refund_reason_category or None,
        customer_notes=customer_notes,
        internal_notes=internal_notes,
        evidence_images=evidence_images or [],
        metadata=metadata or {},
    )

@transaction.atomic
def approve_refund(refund: Refund, approved_by: Optional[Any] = None) -> Refund:
    refund.status = Refund.RefundStatus.APPROVED
    refund.approved_by = approved_by
    refund.approved_at = timezone.now()
    refund.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return refund

@transaction.atomic
def reject_refund(refund: Refund, approved_by: Optional[Any] = None, rejection_reason: str = "") -> Refund:
    refund.status = Refund.RefundStatus.REJECTED
    if approved_by:
        refund.approved_by = approved_by
    if rejection_reason:
        refund.internal_notes = rejection_reason
    refund.save(update_fields=["status", "approved_by", "internal_notes", "updated_at"])
    return refund

@transaction.atomic
def complete_refund(refund: Refund) -> Refund:
    refund.status = Refund.RefundStatus.APPROVED
    refund.completed_at = timezone.now()
    refund.save(update_fields=["status", "completed_at", "updated_at"])
    return refund

@transaction.atomic
def create_shipment(
    *,
    order: Order,
    carrier: str,
    tracking_number: str = "",
    tracking_url: str = "",
    warehouse: Optional[Any] = None,
    shipping_cost: Decimal = c.ZERO_DECIMAL_2,
    notes: str = "",
    estimated_delivery_date: Optional[Any] = None,
    carrier_service_level: str = "",
    carrier_api_integration_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Shipment:
    shipment_number = _generate_shipment_number()
    shipment = Shipment.objects.create(
        order=order,
        shipment_number=shipment_number,
        carrier=carrier,
        tracking_number=tracking_number,
        tracking_url=tracking_url,
        warehouse=warehouse,
        shipping_cost=shipping_cost,
        notes=notes,
        estimated_delivery_date=estimated_delivery_date,
        carrier_service_level=carrier_service_level,
        carrier_api_integration_id=carrier_api_integration_id,
        metadata=metadata or {},
    )

    if tracking_number and not order.tracking_number:
        order.tracking_number = tracking_number
        order.carrier = carrier
        order.save(update_fields=["tracking_number", "carrier", "updated_at"])
    return shipment

@transaction.atomic
def create_return_request(
    *,
    order: Order,
    return_type: str = ReturnRequest.ReturnType.REFUND,
    reason_category: str = ReturnRequest.ReturnReasonCategory.OTHER,
    reason_text: str = "",
    requested_by: Optional[Any] = None,
    customer_notes: str = "",
    internal_notes: str = "",
    restock_decision: str = "",
    restock_location: str = "",
    return_shipping_address: Optional[OrderAddressSnapshot] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> ReturnRequest:
    return ReturnRequest.objects.create(
        order=order,
        return_type=return_type,
        reason_category=reason_category,
        reason_text=reason_text,
        status=ReturnRequest.ReturnStatus.REQUESTED,
        requested_by=requested_by,
        customer_notes=customer_notes,
        internal_notes=internal_notes,
        restock_decision=restock_decision or None,
        restock_location=restock_location,
        return_shipping_address_snapshot=return_shipping_address,
        metadata=metadata or {},
    )

@transaction.atomic
def approve_return(return_request: ReturnRequest, approved_by: Optional[Any] = None) -> ReturnRequest:
    return_request.status = ReturnRequest.ReturnStatus.APPROVED
    return_request.approved_by = approved_by
    return_request.approved_at = timezone.now()
    return_request.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    return return_request

@transaction.atomic
def complete_return(return_request: ReturnRequest) -> ReturnRequest:
    return_request.status = ReturnRequest.ReturnStatus.COMPLETED
    return_request.completed_at = timezone.now()
    return_request.save(update_fields=["status", "completed_at", "updated_at"])
    return return_request

def generate_invoice_document(order: Order) -> Tuple[bytes, str]:
    filename = f"invoice-{order.order_number}.pdf"
    content = f"Invoice PDF Content for Order {order.order_number}".encode("utf-8")
    return content, filename

__all__ = [
    "create_address_snapshot", "generate_unique_order_number", "create_order",
    "add_order_item", "bulk_add_order_items", "update_order_item_quantity",
    "update_order_item_status", "delete_order_item", "recalculate_order_totals",
    "update_order_status", "cancel_order", "complete_order", "hold_order",
    "resume_order", "mark_order_paid", "reorder_items_into_cart", "create_payment",
    "update_payment_status", "create_refund", "approve_refund", "reject_refund",
    "complete_refund", "create_shipment", "create_return_request", "approve_return",
    "complete_return", "generate_invoice_document",
]