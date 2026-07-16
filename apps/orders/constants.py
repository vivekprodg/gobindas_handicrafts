"""
Enterprise-grade centralized configuration module for the Orders application.

This module is the SINGLE SOURCE OF TRUTH for every reusable literal,
status value, field limit, regex, path template, default value, and
administrative display constant used across the orders app.

ARCHITECTURE
============

* The Orders app is inventory-agnostic. This constants module reflects
  that fact by centralising the immutable values that downstream
  services, selectors, signal handlers, tests, and admin customizations
  all rely on.

* ``constants.py`` NEVER imports ``models.py``, ``admin.py``,
  ``services.py``, ``selectors.py`` or ``views.py``. This guarantees
  zero circular imports, zero premature model loading, and zero side
  effects during import.

* All values declared here mirror the values already declared inside
  the corresponding ``TextChoices`` and field defaults in
  ``models.py``. The duplication is INTENTIONAL because:

        1. ``models.py`` is the runtime domain layer that
           Django's ORM introspects.
        2. ``constants.py`` is the configuration / reference layer
           consumed by services, tests, management commands, and
           future micro-services that may not want to load Django.

* The values are kept in lockstep with ``models.py`` by code review.
  If you change a value here, change the matching ``TextChoices`` in
  ``models.py`` and vice-versa.

* Every constant is:

        - Python 3.13 compliant
        - Django 5.1.4 compliant
        - PEP 8 compliant
        - PEP 257 (docstring) compliant
        - PEP 484 (type-hint) compliant
        - Immutable (declared as ``Final[...]`` where appropriate)
        - Documented with a clear purpose
        - Grouped by domain

CATEGORIES
==========

1.  Order lifecycle statuses
2.  Item lifecycle statuses
3.  Shipment lifecycle statuses
4.  Payment & refund statuses / methods / reasons
5.  Tax, discount, note, attachment, and timeline event values
6.  Return-workflow values
7.  Monetary defaults and decimal precision
8.  Field length limits (CharField / FileField)
9.  Validation regex patterns and bounds
10. Upload path templates and folder names
11. Pagination defaults
12. Admin display constants (format strings, badge colours)
13. Return-number formatting constants
14. CSV export configuration
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Final, FrozenSet, Tuple

# ==============================================================================
# 1. ORDER LIFECYCLE STATUSES
# ==============================================================================
class OrderStatus:
    """
    Mirrors ``Order.OrderStatus`` TextChoices. Defines the canonical
    string identifiers used by every order-state machine in the
    project (services, selectors, admin actions, signals, tests).
    """

    PENDING: Final[str] = "pending"
    PROCESSING: Final[str] = "processing"
    SHIPPED: Final[str] = "shipped"
    DELIVERED: Final[str] = "delivered"
    CANCELLED: Final[str] = "cancelled"
    REFUNDED: Final[str] = "refunded"
    ON_HOLD: Final[str] = "on_hold"
    PARTIALLY_SHIPPED: Final[str] = "partially_shipped"
    PARTIALLY_DELIVERED: Final[str] = "partially_delivered"
    BACKORDERED: Final[str] = "backordered"
    COMPLETED: Final[str] = "completed"
    FAILED: Final[str] = "failed"
    AWAITING_PAYMENT: Final[str] = "awaiting_payment"
    PARTIALLY_REFUNDED: Final[str] = "partially_refunded"
    DISPUTED: Final[str] = "disputed"
    DRAFT: Final[str] = "draft"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            PENDING,
            PROCESSING,
            SHIPPED,
            DELIVERED,
            CANCELLED,
            REFUNDED,
            ON_HOLD,
            PARTIALLY_SHIPPED,
            PARTIALLY_DELIVERED,
            BACKORDERED,
            COMPLETED,
            FAILED,
            AWAITING_PAYMENT,
            PARTIALLY_REFUNDED,
            DISPUTED,
            DRAFT,
        }
    )

    #: Statuses from which an order may be cancelled by the customer
    #: or by an operator via the admin "mark_cancelled" action.
    CANCELLABLE_FROM: Final[FrozenSet[str]] = frozenset(
        {PENDING, AWAITING_PAYMENT, ON_HOLD, PROCESSING}
    )

    #: Statuses considered terminal-success.
    TERMINAL_SUCCESS: Final[FrozenSet[str]] = frozenset({DELIVERED, COMPLETED})

    #: Statuses considered terminal-failure.
    TERMINAL_FAILURE: Final[FrozenSet[str]] = frozenset(
        {CANCELLED, REFUNDED, FAILED, PARTIALLY_REFUNDED}
    )

# ==============================================================================
# 2. PAYMENT STATUSES (Order-level)
# ==============================================================================
class PaymentStatus:
    """Mirrors ``Order.PaymentStatus`` TextChoices."""

    PENDING: Final[str] = "pending"
    PARTIALLY_PAID: Final[str] = "partially_paid"
    PAID: Final[str] = "paid"
    FAILED: Final[str] = "failed"
    REFUNDED: Final[str] = "refunded"
    PARTIALLY_REFUNDED: Final[str] = "partially_refunded"
    AUTHORIZED: Final[str] = "authorized"
    CAPTURED: Final[str] = "captured"
    VOIDED: Final[str] = "voided"
    DISPUTED: Final[str] = "disputed"
    EXPIRED: Final[str] = "expired"
    PENDING_PAYMENT: Final[str] = "pending_payment"
    PROCESSING: Final[str] = "processing"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            PENDING,
            PARTIALLY_PAID,
            PAID,
            FAILED,
            REFUNDED,
            PARTIALLY_REFUNDED,
            AUTHORIZED,
            CAPTURED,
            VOIDED,
            DISPUTED,
            EXPIRED,
            PENDING_PAYMENT,
            PROCESSING,
        }
    )

    #: Set of payment statuses that satisfy the ``Order.is_paid`` check.
    PAID_LIKE: Final[FrozenSet[str]] = frozenset({PAID, CAPTURED, REFUNDED})

# ==============================================================================
# 3. ORDER SOURCE
# ==============================================================================
class OrderSource:
    """Mirrors ``Order.Source`` TextChoices."""

    WEB: Final[str] = "web"
    ADMIN: Final[str] = "admin"
    API: Final[str] = "api"
    IMPORT: Final[str] = "import"
    PHONE: Final[str] = "phone"
    MARKETPLACE: Final[str] = "marketplace"
    SUBSCRIPTION: Final[str] = "subscription"
    POS: Final[str] = "pos"
    MIGRATION: Final[str] = "migration"
    OTHER: Final[str] = "other"

    ALL: Final[FrozenSet[str]] = frozenset(
        {WEB, ADMIN, API, IMPORT, PHONE, MARKETPLACE, SUBSCRIPTION, POS, MIGRATION, OTHER}
    )

# ==============================================================================
# 4. FRAUD CHECK STATUS
# ==============================================================================
class FraudCheckStatus:
    """Mirrors ``Order.FraudCheckStatus`` TextChoices."""

    NOT_CHECKED: Final[str] = "not_checked"
    PENDING: Final[str] = "pending"
    PASSED: Final[str] = "passed"
    FAILED: Final[str] = "failed"
    MANUAL_REVIEW: Final[str] = "manual_review"

    ALL: Final[FrozenSet[str]] = frozenset(
        {NOT_CHECKED, PENDING, PASSED, FAILED, MANUAL_REVIEW}
    )

# ==============================================================================
# 5. ORDER ITEM LIFECYCLE
# ==============================================================================
class ItemStatus:
    """Mirrors ``OrderItem.ItemStatus`` TextChoices."""

    ACTIVE: Final[str] = "active"
    SAVED: Final[str] = "saved"
    REMOVED: Final[str] = "removed"
    EXPIRED: Final[str] = "expired"
    RETURNED: Final[str] = "returned"
    REFUNDED: Final[str] = "refunded"
    CANCELLED: Final[str] = "cancelled"
    PARTIALLY_RETURNED: Final[str] = "partially_returned"
    PARTIALLY_SHIPPED: Final[str] = "partially_shipped"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            ACTIVE,
            SAVED,
            REMOVED,
            EXPIRED,
            RETURNED,
            REFUNDED,
            CANCELLED,
            PARTIALLY_RETURNED,
            PARTIALLY_SHIPPED,
        }
    )

    #: Item statuses eligible for shipping.
    SHIPPABLE: Final[FrozenSet[str]] = frozenset({ACTIVE, PARTIALLY_SHIPPED})

    #: Item statuses eligible for return.
    RETURNABLE: Final[FrozenSet[str]] = frozenset({ACTIVE, PARTIALLY_SHIPPED})

class SavedForLaterReason:
    """Mirrors ``OrderItem.SavedForLaterReason`` TextChoices."""

    MANUAL: Final[str] = "manual"
    REPLACED: Final[str] = "replaced"
    STOCK_OUT: Final[str] = "out_of_stock"

    ALL: Final[FrozenSet[str]] = frozenset({MANUAL, REPLACED, STOCK_OUT})

# ==============================================================================
# 6. SHIPMENT LIFECYCLE
# ==============================================================================
class ShipmentStatus:
    """Mirrors ``Shipment.ShipmentStatus`` TextChoices."""

    PENDING: Final[str] = "pending"
    DISPATCHED: Final[str] = "dispatched"
    IN_TRANSIT: Final[str] = "in_transit"
    DELIVERED: Final[str] = "delivered"
    RETURNED: Final[str] = "returned"
    EXCEPTION: Final[str] = "exception"
    OUT_FOR_DELIVERY: Final[str] = "out_for_delivery"
    FAILED_ATTEMPT: Final[str] = "failed_attempt"
    AWAITING_PICKUP: Final[str] = "awaiting_pickup"
    PICKED_UP: Final[str] = "picked_up"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            PENDING,
            DISPATCHED,
            IN_TRANSIT,
            DELIVERED,
            RETURNED,
            EXCEPTION,
            OUT_FOR_DELIVERY,
            FAILED_ATTEMPT,
            AWAITING_PICKUP,
            PICKED_UP,
        }
    )

    #: Statuses in which the parcel is in physical transit.
    IN_TRANSIT_LIKE: Final[FrozenSet[str]] = frozenset(
        {DISPATCHED, IN_TRANSIT, OUT_FOR_DELIVERY}
    )

# ==============================================================================
# 7. PAYMENT (RECORD-LEVEL) LIFECYCLE
# ==============================================================================
class PaymentState:
    """Mirrors ``Payment.PaymentState`` TextChoices."""

    PENDING: Final[str] = "pending"
    COMPLETED: Final[str] = "completed"
    FAILED: Final[str] = "failed"
    REFUNDED: Final[str] = "refunded"
    AUTHORIZED: Final[str] = "authorized"
    CAPTURED: Final[str] = "captured"
    PARTIALLY_REFUNDED: Final[str] = "partially_refunded"
    VOIDED: Final[str] = "voided"
    EXPIRED: Final[str] = "expired"
    DISPUTED: Final[str] = "disputed"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            PENDING,
            COMPLETED,
            FAILED,
            REFUNDED,
            AUTHORIZED,
            CAPTURED,
            PARTIALLY_REFUNDED,
            VOIDED,
            EXPIRED,
            DISPUTED,
        }
    )

class PaymentAttemptStatus:
    """Mirrors ``PaymentAttempt.AttemptStatus`` TextChoices."""

    PENDING: Final[str] = "pending"
    SUCCESS: Final[str] = "success"
    FAILURE: Final[str] = "failure"
    TIMEOUT: Final[str] = "timeout"
    CANCELLED: Final[str] = "cancelled"
    REQUIRES_ACTION: Final[str] = "requires_action"
    THREE_DS_REQUIRED: Final[str] = "three_ds_required"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            PENDING,
            SUCCESS,
            FAILURE,
            TIMEOUT,
            CANCELLED,
            REQUIRES_ACTION,
            THREE_DS_REQUIRED,
        }
    )

# ==============================================================================
# 8. REFUND LIFECYCLE
# ==============================================================================
class RefundStatus:
    """Mirrors ``Refund.RefundStatus`` TextChoices."""

    REQUESTED: Final[str] = "requested"
    APPROVED: Final[str] = "approved"
    PROCESSED: Final[str] = "processed"
    REJECTED: Final[str] = "rejected"
    PENDING: Final[str] = "pending"
    FAILED: Final[str] = "failed"
    CANCELLED: Final[str] = "cancelled"

    ALL: Final[FrozenSet[str]] = frozenset(
        {REQUESTED, APPROVED, PROCESSED, REJECTED, PENDING, FAILED, CANCELLED}
    )

    #: Refund statuses that allow approval.
    APPROVABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED})

    #: Refund statuses that allow rejection.
    REJECTABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED, APPROVED})

    #: Refund statuses that allow processing.
    PROCESSABLE_FROM: Final[FrozenSet[str]] = frozenset({APPROVED})

    #: Refund statuses that allow completion.
    COMPLETABLE_FROM: Final[FrozenSet[str]] = frozenset({PROCESSED})

class RefundMethod:
    """Mirrors ``Refund.RefundMethod`` TextChoices."""

    ORIGINAL: Final[str] = "original"
    STORE_CREDIT: Final[str] = "store_credit"
    BANK_TRANSFER: Final[str] = "bank_transfer"
    CHECK: Final[str] = "check"
    CASH: Final[str] = "cash"
    OTHER: Final[str] = "other"

    ALL: Final[FrozenSet[str]] = frozenset(
        {ORIGINAL, STORE_CREDIT, BANK_TRANSFER, CHECK, CASH, OTHER}
    )

class RefundReasonCategory:
    """Mirrors ``Refund.RefundReasonCategory`` TextChoices."""

    CUSTOMER_REQUEST: Final[str] = "customer_request"
    DEFECTIVE_PRODUCT: Final[str] = "defective_product"
    WRONG_ITEM: Final[str] = "wrong_item"
    NOT_AS_DESCRIBED: Final[str] = "not_as_described"
    DUPLICATE_CHARGE: Final[str] = "duplicate_charge"
    FRAUD: Final[str] = "fraud"
    GOODWILL: Final[str] = "goodwill"
    OTHER: Final[str] = "other"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            CUSTOMER_REQUEST,
            DEFECTIVE_PRODUCT,
            WRONG_ITEM,
            NOT_AS_DESCRIBED,
            DUPLICATE_CHARGE,
            FRAUD,
            GOODWILL,
            OTHER,
        }
    )

# ==============================================================================
# 9. TAX MODE
# ==============================================================================
class TaxMode:
    """Mirrors ``TaxLine.TaxMode`` TextChoices."""

    INCLUSIVE: Final[str] = "inclusive"
    EXCLUSIVE: Final[str] = "exclusive"

    ALL: Final[FrozenSet[str]] = frozenset({INCLUSIVE, EXCLUSIVE})

# ==============================================================================
# 10. DISCOUNT TYPE
# ==============================================================================
class DiscountType:
    """Mirrors ``DiscountLine.DiscountType`` TextChoices."""

    COUPON: Final[str] = "coupon"
    PROMOTION: Final[str] = "promotion"
    LOYALTY: Final[str] = "loyalty"
    STAFF: Final[str] = "staff"
    GOODWILL: Final[str] = "goodwill"
    BULK: Final[str] = "bulk"
    SEASONAL: Final[str] = "seasonal"
    OTHER: Final[str] = "other"

    ALL: Final[FrozenSet[str]] = frozenset(
        {COUPON, PROMOTION, LOYALTY, STAFF, GOODWILL, BULK, SEASONAL, OTHER}
    )

# ==============================================================================
# 11. NOTE TYPE
# ==============================================================================
class NoteType:
    """Mirrors ``OrderNote.NoteType`` TextChoices."""

    CUSTOMER: Final[str] = "customer"
    OPERATOR: Final[str] = "operator"
    GIFT: Final[str] = "gift"
    DELIVERY: Final[str] = "delivery"
    SYSTEM: Final[str] = "system"

    ALL: Final[FrozenSet[str]] = frozenset(
        {CUSTOMER, OPERATOR, GIFT, DELIVERY, SYSTEM}
    )

# ==============================================================================
# 12. ATTACHMENT TYPE
# ==============================================================================
class AttachmentType:
    """Mirrors ``OrderAttachment.AttachmentType`` TextChoices."""

    INVOICE: Final[str] = "invoice"
    PACKING_SLIP: Final[str] = "packing_slip"
    DELIVERY_PROOF: Final[str] = "delivery_proof"
    CUSTOMS: Final[str] = "customs"
    INSURANCE: Final[str] = "insurance"
    CUSTOMER_DOC: Final[str] = "customer_doc"
    OPERATOR_DOC: Final[str] = "operator_doc"
    RETURN_LABEL: Final[str] = "return_label"
    REPLACEMENT_LABEL: Final[str] = "replacement_label"
    SIGNATURE: Final[str] = "signature"
    OTHER: Final[str] = "other"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            INVOICE,
            PACKING_SLIP,
            DELIVERY_PROOF,
            CUSTOMS,
            INSURANCE,
            CUSTOMER_DOC,
            OPERATOR_DOC,
            RETURN_LABEL,
            REPLACEMENT_LABEL,
            SIGNATURE,
            OTHER,
        }
    )

# ==============================================================================
# 13. TIMELINE EVENT TYPE
# ==============================================================================
class TimelineEventType:
    """Mirrors ``OrderTimelineEvent.EventType`` TextChoices."""

    ORDER_PLACED: Final[str] = "order_placed"
    ORDER_UPDATED: Final[str] = "order_updated"
    ORDER_CANCELLED: Final[str] = "order_cancelled"
    ORDER_COMPLETED: Final[str] = "order_completed"
    PAYMENT_INITIATED: Final[str] = "payment_initiated"
    PAYMENT_AUTHORIZED: Final[str] = "payment_authorized"
    PAYMENT_CAPTURED: Final[str] = "payment_captured"
    PAYMENT_FAILED: Final[str] = "payment_failed"
    PAYMENT_REFUNDED: Final[str] = "payment_refunded"
    SHIPMENT_CREATED: Final[str] = "shipment_created"
    SHIPMENT_PICKED: Final[str] = "shipment_picked"
    SHIPMENT_IN_TRANSIT: Final[str] = "shipment_in_transit"
    SHIPMENT_OUT_FOR_DELIVERY: Final[str] = "shipment_out_for_delivery"
    SHIPMENT_DELIVERED: Final[str] = "shipment_delivered"
    SHIPMENT_FAILED: Final[str] = "shipment_failed"
    SHIPMENT_RETURNED: Final[str] = "shipment_returned"
    REFUND_INITIATED: Final[str] = "refund_initiated"
    REFUND_APPROVED: Final[str] = "refund_approved"
    REFUND_REJECTED: Final[str] = "refund_rejected"
    REFUND_COMPLETED: Final[str] = "refund_completed"
    NOTE_ADDED: Final[str] = "note_added"
    ATTACHMENT_ADDED: Final[str] = "attachment_added"
    RETURN_REQUESTED: Final[str] = "return_requested"
    RETURN_APPROVED: Final[str] = "return_approved"
    RETURN_REJECTED: Final[str] = "return_rejected"
    RETURN_RECEIVED: Final[str] = "return_received"
    RETURN_COMPLETED: Final[str] = "return_completed"
    DISCOUNT_APPLIED: Final[str] = "discount_applied"
    DISCOUNT_REVERSED: Final[str] = "discount_reversed"
    FRAUD_CHECK_PASSED: Final[str] = "fraud_check_passed"
    FRAUD_CHECK_FAILED: Final[str] = "fraud_check_failed"
    FRAUD_CHECK_REVIEW: Final[str] = "fraud_check_review"
    INVENTORY_ALLOCATED: Final[str] = "inventory_allocated"
    INVENTORY_DEDUCTED: Final[str] = "inventory_deducted"
    INVENTORY_RESTOCKED: Final[str] = "inventory_restocked"
    INVENTORY_TRANSFERRED: Final[str] = "inventory_transferred"
    SYSTEM: Final[str] = "system"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            ORDER_PLACED,
            ORDER_UPDATED,
            ORDER_CANCELLED,
            ORDER_COMPLETED,
            PAYMENT_INITIATED,
            PAYMENT_AUTHORIZED,
            PAYMENT_CAPTURED,
            PAYMENT_FAILED,
            PAYMENT_REFUNDED,
            SHIPMENT_CREATED,
            SHIPMENT_PICKED,
            SHIPMENT_IN_TRANSIT,
            SHIPMENT_OUT_FOR_DELIVERY,
            SHIPMENT_DELIVERED,
            SHIPMENT_FAILED,
            SHIPMENT_RETURNED,
            REFUND_INITIATED,
            REFUND_APPROVED,
            REFUND_REJECTED,
            REFUND_COMPLETED,
            NOTE_ADDED,
            ATTACHMENT_ADDED,
            RETURN_REQUESTED,
            RETURN_APPROVED,
            RETURN_REJECTED,
            RETURN_RECEIVED,
            RETURN_COMPLETED,
            DISCOUNT_APPLIED,
            DISCOUNT_REVERSED,
            FRAUD_CHECK_PASSED,
            FRAUD_CHECK_FAILED,
            FRAUD_CHECK_REVIEW,
            INVENTORY_ALLOCATED,
            INVENTORY_DEDUCTED,
            INVENTORY_RESTOCKED,
            INVENTORY_TRANSFERRED,
            SYSTEM,
        }
    )

# ==============================================================================
# 14. RETURN WORKFLOW
# ==============================================================================
class ReturnType:
    """Mirrors ``ReturnRequest.ReturnType`` TextChoices."""

    REFUND: Final[str] = "refund"
    REPLACEMENT: Final[str] = "replacement"
    EXCHANGE: Final[str] = "exchange"
    STORE_CREDIT: Final[str] = "store_credit"
    REPAIR: Final[str] = "repair"

    ALL: Final[FrozenSet[str]] = frozenset(
        {REFUND, REPLACEMENT, EXCHANGE, STORE_CREDIT, REPAIR}
    )

class ReturnReasonCategory:
    """Mirrors ``ReturnRequest.ReturnReasonCategory`` TextChoices."""

    DEFECTIVE: Final[str] = "defective"
    WRONG_ITEM: Final[str] = "wrong_item"
    NOT_AS_DESCRIBED: Final[str] = "not_as_described"
    SIZE_ISSUE: Final[str] = "size_issue"
    COLOR_ISSUE: Final[str] = "color_issue"
    QUALITY_ISSUE: Final[str] = "quality_issue"
    DAMAGED_IN_TRANSIT: Final[str] = "damaged_in_transit"
    LATE_DELIVERY: Final[str] = "late_delivery"
    CHANGED_MIND: Final[str] = "changed_mind"
    DUPLICATE_ORDER: Final[str] = "duplicate_order"
    BETTER_PRICE_FOUND: Final[str] = "better_price_found"
    OTHER: Final[str] = "other"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            DEFECTIVE,
            WRONG_ITEM,
            NOT_AS_DESCRIBED,
            SIZE_ISSUE,
            COLOR_ISSUE,
            QUALITY_ISSUE,
            DAMAGED_IN_TRANSIT,
            LATE_DELIVERY,
            CHANGED_MIND,
            DUPLICATE_ORDER,
            BETTER_PRICE_FOUND,
            OTHER,
        }
    )

class ReturnStatus:
    """Mirrors ``ReturnRequest.ReturnStatus`` TextChoices."""

    DRAFT: Final[str] = "draft"
    REQUESTED: Final[str] = "requested"
    UNDER_REVIEW: Final[str] = "under_review"
    APPROVED: Final[str] = "approved"
    REJECTED: Final[str] = "rejected"
    AWAITING_SHIPMENT: Final[str] = "awaiting_shipment"
    IN_TRANSIT: Final[str] = "in_transit"
    RECEIVED: Final[str] = "received"
    INSPECTING: Final[str] = "inspecting"
    REFUND_INITIATED: Final[str] = "refund_initiated"
    COMPLETED: Final[str] = "completed"
    CANCELLED: Final[str] = "cancelled"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            DRAFT,
            REQUESTED,
            UNDER_REVIEW,
            APPROVED,
            REJECTED,
            AWAITING_SHIPMENT,
            IN_TRANSIT,
            RECEIVED,
            INSPECTING,
            REFUND_INITIATED,
            COMPLETED,
            CANCELLED,
        }
    )

    #: Return statuses considered resolved (terminal).
    RESOLVED: Final[FrozenSet[str]] = frozenset({COMPLETED, REJECTED, CANCELLED})

    #: Return statuses from which approval is allowed.
    APPROVABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED})

    #: Return statuses from which rejection is allowed.
    REJECTABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED})

    #: Return statuses from which a "received" transition is allowed.
    RECEIVABLE_FROM: Final[FrozenSet[str]] = frozenset(
        {AWAITING_SHIPMENT, IN_TRANSIT}
    )

    #: Return statuses from which a "completed" transition is allowed.
    COMPLETABLE_FROM: Final[FrozenSet[str]] = frozenset({RECEIVED, INSPECTING})

class RestockDecision:
    """Mirrors ``ReturnRequest.RestockDecision`` TextChoices."""

    RESTOCK: Final[str] = "restock"
    DISPOSE: Final[str] = "dispose"
    RETURN_TO_SUPPLIER: Final[str] = "return_to_supplier"
    REPAIR: Final[str] = "repair"
    QUARANTINE: Final[str] = "quarantine"

    ALL: Final[FrozenSet[str]] = frozenset(
        {RESTOCK, DISPOSE, RETURN_TO_SUPPLIER, REPAIR, QUARANTINE}
    )

class InspectionResult:
    """Mirrors ``ReturnItem.InspectionResult`` TextChoices."""

    PENDING: Final[str] = "pending"
    PASSED: Final[str] = "passed"
    FAILED: Final[str] = "failed"
    PARTIAL: Final[str] = "partial"

    ALL: Final[FrozenSet[str]] = frozenset({PENDING, PASSED, FAILED, PARTIAL})

class ReturnImageType:
    """Mirrors ``ReturnImage.ImageType`` TextChoices."""

    EVIDENCE: Final[str] = "evidence"
    REFERENCE: Final[str] = "reference"
    PACKAGING: Final[str] = "packaging"
    LABEL: Final[str] = "label"
    OPERATOR: Final[str] = "operator"
    OTHER: Final[str] = "other"

    ALL: Final[FrozenSet[str]] = frozenset(
        {EVIDENCE, REFERENCE, PACKAGING, LABEL, OPERATOR, OTHER}
    )

# ==============================================================================
# 15. CURRENCY & MONETARY DEFAULTS
# ==============================================================================
class Currency:
    """
    ISO 4217 currency identifiers used across the Orders app.

    The Orders app does not own a live FX engine. It only stores the
    snapshot currency and an explicit exchange rate. The base currency
    is the reporting / settlement currency configured at the platform
    level.
    """

    #: Default ISO 4217 code for new orders. Matches the cart's default.
    DEFAULT_CODE: Final[str] = "NPR"

    #: Default display symbol for the default code.
    DEFAULT_SYMBOL: Final[str] = "NPR"

    #: Default base / settlement currency.
    DEFAULT_BASE: Final[str] = "NPR"

# ==============================================================================
# 16. DECIMAL PRECISION (max_digits, decimal_places)
# ==============================================================================
class DecimalPrecision:
    """
    Standard decimal precisions used by the Orders app.

    Each constant is a ``(max_digits, decimal_places)`` tuple that
    exactly matches the corresponding ``DecimalField`` declaration in
    ``models.py``. Centralising them here guarantees that:

        * Services that build manual SQL / reports use the same scale.
        * Future schema migrations only need to update this module.
        * Unit tests can assert precision invariants in one place.
    """

    #: Order header financials (subtotal, discount, shipping, tax, total).
    ORDER_MONEY: Final[Tuple[int, int]] = (14, 2)

    #: Order line item commercial values (unit_price, discount, tax, line_total).
    LINE_MONEY: Final[Tuple[int, int]] = (12, 2)

    #: Single-unit weight (per order item).
    ITEM_WEIGHT: Final[Tuple[int, int]] = (10, 3)

    #: Total parcel weight (per shipment).
    SHIPMENT_WEIGHT: Final[Tuple[int, int]] = (14, 3)

    #: Snapshot variant weight.
    VARIANT_WEIGHT: Final[Tuple[int, int]] = (10, 3)

    #: Shipped / returned / refunded running counters.
    LIFECYCLE_QUANTITY: Final[Tuple[int, int]] = (14, 2)

    #: Shipment shipping cost.
    SHIPMENT_COST: Final[Tuple[int, int]] = (12, 2)

    #: Refund amount.
    REFUND_AMOUNT: Final[Tuple[int, int]] = (14, 2)

    #: Tax base amount / tax amount / discount amount.
    MONEY_14_2: Final[Tuple[int, int]] = (14, 2)

    #: Coupon usage discount amount.
    COUPON_DISCOUNT: Final[Tuple[int, int]] = (12, 2)

    #: Base-currency snapshot total (largest precision in the schema).
    BASE_CURRENCY_TOTAL: Final[Tuple[int, int]] = (18, 2)

    #: FX snapshot exchange rate (8 fractional digits).
    EXCHANGE_RATE: Final[Tuple[int, int]] = (18, 8)

    #: Risk score (0.00 - 100.00).
    RISK_SCORE: Final[Tuple[int, int]] = (5, 2)

    #: Tax rate (0.0000 - 1.0000).
    TAX_RATE: Final[Tuple[int, int]] = (8, 4)

    #: Percentage (0.00 - 100.00).
    PERCENTAGE: Final[Tuple[int, int]] = (5, 2)

    #: Geo latitude / longitude (WGS84).
    COORDINATE: Final[Tuple[int, int]] = (9, 6)

# ==============================================================================
# 17. DEFAULT DECIMAL / NUMERIC VALUES
# ==============================================================================
#: Canonical "zero" used by every monetary field.
ZERO_DECIMAL_2: Final[Decimal] = Decimal("0.00")

#: Canonical "zero" used by every weight field.
ZERO_DECIMAL_3: Final[Decimal] = Decimal("0.000")

#: Default exchange rate when no FX conversion has been applied.
DEFAULT_EXCHANGE_RATE: Final[Decimal] = Decimal("1.00000000")

#: Minimum legal exchange rate (strictly positive, per Order.clean()).
MIN_EXCHANGE_RATE: Final[Decimal] = Decimal("0.00000001")

#: Minimum legal risk score.
MIN_RISK_SCORE: Final[Decimal] = Decimal("0.00")

#: Maximum legal risk score.
MAX_RISK_SCORE: Final[Decimal] = Decimal("100.00")

#: Minimum legal percentage.
MIN_PERCENTAGE: Final[Decimal] = Decimal("0.00")

#: Maximum legal percentage.
MAX_PERCENTAGE: Final[Decimal] = Decimal("100.00")

#: Minimum legal tax rate.
MIN_TAX_RATE: Final[Decimal] = Decimal("0.0000")

#: Maximum legal tax rate (1.0000 == 100%).
MAX_TAX_RATE: Final[Decimal] = Decimal("1.0000")

#: Minimum legal order-item quantity.
MIN_QUANTITY: Final[int] = 1

#: Default order-item quantity.
DEFAULT_QUANTITY: Final[int] = 1

#: Default payment attempts counter on a fresh Payment record.
DEFAULT_PAYMENT_ATTEMPTS: Final[int] = 1

# ==============================================================================
# 18. MODULE-LEVEL DEFAULTS (mirrored from models.py)
# ==============================================================================
#: Default ISO 4217 currency code (mirrors DEFAULT_CURRENCY_CODE in models.py).
DEFAULT_CURRENCY_CODE: Final[str] = Currency.DEFAULT_CODE

#: Default low-stock alert threshold mirrored from the inventory app.
DEFAULT_LOW_STOCK_THRESHOLD: Final[int] = 5

#: Default page size for paginated order views.
DEFAULT_ORDER_PAGE_SIZE: Final[int] = 25

#: Default payment method for legacy orders.
DEFAULT_PAYMENT_METHOD: Final[str] = "manual"

#: Default "no carrier" placeholder for legacy orders.
DEFAULT_CARRIER_NAME: Final[str] = "Unknown"

#: Default order active state for legacy records.
DEFAULT_ORDER_ACTIVE_STATE: Final[bool] = True

#: Default shipping cost when no shipping rule has been resolved yet.
DEFAULT_SHIPPING_COST: Final[Decimal] = ZERO_DECIMAL_2

#: Default display position for ordered line items.
DEFAULT_POSITION: Final[int] = 0

#: Default SLA: low-stock alert as a percentage of normal stock.
DEFAULT_LOW_STOCK_PERCENTAGE: Final[Decimal] = Decimal("0.10")

# ==============================================================================
# 19. FIELD LENGTH LIMITS (CharField / TextField max_length values)
# ==============================================================================
class FieldLength:
    """
    Centralised ``max_length`` values for every ``CharField`` declared
    in the Orders app's models. This guarantees that services,
    serializers, and form validation agree with the schema.
    """

    # ---- OrderAddressSnapshot ------------------------------------------
    FULL_NAME: Final[int] = 255
    PHONE_NUMBER: Final[int] = 50
    PHONE_E164: Final[int] = 20
    COMPANY: Final[int] = 255
    ADDRESS_LINE: Final[int] = 255
    CITY: Final[int] = 100
    STATE_OR_PROVINCE: Final[int] = 100
    POSTAL_CODE: Final[int] = 50
    COUNTRY: Final[int] = 100
    COUNTRY_CODE: Final[int] = 2
    ADDRESS_HASH: Final[int] = 64

    # ---- Order ----------------------------------------------------------
    ORDER_NUMBER: Final[int] = 50
    PAYMENT_METHOD: Final[int] = 100
    TRANSACTION_ID: Final[int] = 255
    CURRENCY: Final[int] = 10
    CURRENCY_SYMBOL: Final[int] = 10
    BASE_CURRENCY: Final[int] = 10
    CUSTOMER_LOCALE: Final[int] = 16
    CUSTOMER_TIMEZONE: Final[int] = 64
    GIFT_WRAPPING: Final[int] = 120
    EXTERNAL_ORDER_ID: Final[int] = 120
    EXTERNAL_PLATFORM: Final[int] = 64
    SOURCE: Final[int] = 32
    FRAUD_CHECK_STATUS: Final[int] = 32
    STATUS: Final[int] = 20
    TRACKING_NUMBER: Final[int] = 100
    CARRIER: Final[int] = 100
    URL_500: Final[int] = 500

    # ---- OrderItem ------------------------------------------------------
    PRODUCT_NAME_SNAPSHOT: Final[int] = 255
    PRODUCT_SKU_SNAPSHOT: Final[int] = 100
    VARIANT_NAME_SNAPSHOT: Final[int] = 255
    PRODUCT_SLUG_SNAPSHOT: Final[int] = 255
    PRODUCT_META_TITLE_SNAPSHOT: Final[int] = 255
    PRODUCT_BRAND_SNAPSHOT: Final[int] = 120
    PRODUCT_ORIGIN_SNAPSHOT: Final[int] = 120
    VARIANT_SKU_SNAPSHOT: Final[int] = 100
    VARIANT_BARCODE_SNAPSHOT: Final[int] = 100
    WAREHOUSE_NAME_SNAPSHOT: Final[int] = 120
    WAREHOUSE_CODE_SNAPSHOT: Final[int] = 50
    SUPPLIER_NAME_SNAPSHOT: Final[int] = 255
    SUPPLIER_ORDER_ID: Final[int] = 120
    SAVED_REASON: Final[int] = 32
    ITEM_STATUS: Final[int] = 20
    SHIPMENT_STATUS: Final[int] = 30

    # ---- OrderStatusHistory --------------------------------------------
    OLD_STATUS: Final[int] = 50
    NEW_STATUS: Final[int] = 50
    NOTIFICATION_METHOD: Final[int] = 32

    # ---- Shipment / ShipmentItem ---------------------------------------
    SHIPMENT_NUMBER: Final[int] = 100
    SHIPMENT_TRACKING_NUMBER: Final[int] = 150
    CARRIER_API_INTEGRATION_ID: Final[int] = 120
    CARRIER_SERVICE_LEVEL: Final[int] = 64
    SERIAL_TRACKING: Final[int] = 120
    CONDITION_AT_PICKUP: Final[int] = 32

    # ---- Payment / PaymentAttempt --------------------------------------
    GATEWAY: Final[int] = 100
    PAYMENT_METHOD_TYPE: Final[int] = 64
    GATEWAY_RESPONSE_CODE: Final[int] = 64

    # ---- Refund --------------------------------------------------------
    REFUND_STATUS: Final[int] = 20
    REFUND_METHOD: Final[int] = 32
    REFUND_REASON_CATEGORY: Final[int] = 64
    GATEWAY_REFUND_ID: Final[int] = 120

    # ---- TaxLine --------------------------------------------------------
    TAX_CLASS: Final[int] = 64
    TAX_NAME: Final[int] = 120
    TAX_AUTHORITY_CODE: Final[int] = 64
    JURISDICTION: Final[int] = 120
    TAX_MODE: Final[int] = 16

    # ---- DiscountLine ---------------------------------------------------
    DISCOUNT_TYPE: Final[int] = 32
    DISCOUNT_SOURCE: Final[int] = 64
    DISCOUNT_CODE: Final[int] = 120
    DISCOUNT_NAME: Final[int] = 255
    PROMOTION_ID: Final[int] = 120

    # ---- CouponUsage ----------------------------------------------------
    COUPON_CODE: Final[int] = 50

    # ---- OrderNote ------------------------------------------------------
    NOTE_TYPE: Final[int] = 32

    # ---- OrderAttachment ------------------------------------------------
    ATTACHMENT_TYPE: Final[int] = 32
    ORIGINAL_FILENAME: Final[int] = 255
    MIME_TYPE: Final[int] = 120
    DESCRIPTION: Final[int] = 255

    # ---- OrderTimelineEvent --------------------------------------------
    EVENT_TYPE: Final[int] = 48
    EVENT_TITLE: Final[int] = 255
    REFERENCE_MODEL: Final[int] = 80
    REFERENCE_ID: Final[int] = 80
    ICON: Final[int] = 64
    COLOR: Final[int] = 32

    # ---- ReturnRequest --------------------------------------------------
    RETURN_NUMBER: Final[int] = 50
    RETURN_TYPE: Final[int] = 24
    REASON_CATEGORY: Final[int] = 48
    RETURN_STATUS: Final[int] = 24
    RESTOCK_DECISION: Final[int] = 32
    RESTOCK_LOCATION: Final[int] = 120

    # ---- ReturnItem -----------------------------------------------------
    CONDITION_RECEIVED: Final[int] = 64
    INSPECTION_RESULT: Final[int] = 16

    # ---- ReturnImage ----------------------------------------------------
    IMAGE_TYPE: Final[int] = 24
    CAPTION: Final[int] = 255

# ==============================================================================
# 20. VALIDATION REGEX PATTERNS
# ==============================================================================
#: Canonical phone-number regex used by ``OrderAddressSnapshot``.
#:
#: Accepts digits, spaces, hyphens, parentheses, and an optional
#: leading '+'. Total length must be between 7 and 20 characters.
PHONE_REGEX: Final[str] = r"^\+?[0-9\s\-\(\)]{7,20}$"

#: Minimum length for a phone number (inclusive).
PHONE_MIN_LENGTH: Final[int] = 7

#: Maximum length for a phone number (inclusive).
PHONE_MAX_LENGTH: Final[int] = 20

# ==============================================================================
# 21. UPLOAD PATH TEMPLATES
# ==============================================================================
#: Sub-folder for order attachments (inside MEDIA_ROOT).
ORDER_ATTACHMENT_FOLDER: Final[str] = "orders/attachments"

#: Sub-folder for return evidence images.
RETURN_IMAGE_FOLDER: Final[str] = "orders/returns"

#: Sub-folder for invoice-related attachments.
INVOICE_FOLDER: Final[str] = "orders/invoices"

#: Sub-folder for shipment label attachments.
SHIPMENT_FOLDER: Final[str] = "orders/shipments"

#: Sub-folder for archived / cold-storage files.
ARCHIVE_FOLDER: Final[str] = "orders/archive"

#: Sub-folder for temporary uploads awaiting processing.
TEMP_FOLDER: Final[str] = "orders/tmp"

#: Sub-folder for CSV exports generated by admin / management commands.
EXPORT_FOLDER: Final[str] = "orders/exports"

#: Sub-folder for bulk imports staging area.
IMPORT_FOLDER: Final[str] = "orders/imports"

#: Default file extension when one cannot be inferred from the original name.
DEFAULT_BINARY_EXTENSION: Final[str] = ".bin"

#: Default extension for return image uploads when none is supplied.
DEFAULT_IMAGE_EXTENSION: Final[str] = ".webp"

# ==============================================================================
# 22. PAGINATION
# ==============================================================================
#: Default page size for changelists, list views, and management commands.
DEFAULT_PAGE_SIZE: Final[int] = 25

#: Hard ceiling for the admin "show all" override.
ADMIN_MAX_SHOW_ALL: Final[int] = 200

#: Default batch size for management command bulk operations.
BULK_OPERATION_BATCH_SIZE: Final[int] = 500

#: Default batch size for export streaming operations.
EXPORT_BATCH_SIZE: Final[int] = 1000

# ==============================================================================
# 23. ADMIN DISPLAY FORMATS
# ==============================================================================
#: Standard datetime format for changelist / detail page rendering.
ADMIN_DATETIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M"

#: Standard date format for changelist / detail page rendering.
ADMIN_DATE_FORMAT: Final[str] = "%Y-%m-%d"

#: Filename-safe timestamp used for export file names.
EXPORT_TIMESTAMP_FORMAT: Final[str] = "%Y%m%d_%H%M%S"

#: UTF-8 BOM emitted at the head of every CSV export.
CSV_BOM: Final[str] = "\ufeff"

#: MIME type for CSV export responses.
CSV_CONTENT_TYPE: Final[str] = "text/csv; charset=utf-8"

#: Prefix for the auto-generated CSV export file name.
CSV_EXPORT_FILENAME_PREFIX: Final[str] = "orders_export_"

#: File extension applied to every CSV export.
CSV_EXPORT_EXTENSION: Final[str] = ".csv"

#: Default display position of a line in an inline.
DEFAULT_INLINE_EXTRA: Final[int] = 0

# ==============================================================================
# 24. ADMIN STATUS BADGE COLOURS
# ==============================================================================
#: (background, foreground) colour pairs for the Order admin badge.
#:
#: Each key is an ``OrderStatus`` value. Values are 7-char hex strings
#: suitable for direct use in inline HTML/CSS.
ORDER_STATUS_BADGE_COLORS: Final[Dict[str, Tuple[str, str]]] = {
    "pending": ("#FFF8E7", "#9A7B54"),
    "processing": ("#E8F5E9", "#2E7D32"),
    "shipped": ("#E3F2FD", "#0D47A1"),
    "delivered": ("#E0F2F1", "#00695C"),
    "cancelled": ("#FFEBEE", "#C62828"),
    "refunded": ("#FFF8E7", "#9A7B54"),
    "completed": ("#E8F5E9", "#2E7D32"),
    "failed": ("#FFEBEE", "#C62828"),
    "on_hold": ("#FFF8E7", "#9A7B54"),
    "awaiting_payment": ("#FFF8E7", "#9A7B54"),
}

#: (background, foreground) colour pairs for the Payment admin badge.
PAYMENT_STATUS_BADGE_COLORS: Final[Dict[str, Tuple[str, str]]] = {
    "pending": ("#FFF8E7", "#9A7B54"),
    "paid": ("#E8F5E9", "#2E7D32"),
    "completed": ("#E8F5E9", "#2E7D32"),
    "failed": ("#FFEBEE", "#C62828"),
    "refunded": ("#FFF8E7", "#9A7B54"),
    "captured": ("#E0F2F1", "#00695C"),
    "authorized": ("#E3F2FD", "#0D47A1"),
    "voided": ("#FAFAFA", "#767676"),
}

#: (background, foreground) colour pairs for the timeline-event badge.
TIMELINE_EVENT_BADGE_COLORS: Final[Dict[str, Tuple[str, str]]] = {
    "order_placed": ("#E8F5E9", "#2E7D32"),
    "order_cancelled": ("#FFEBEE", "#C62828"),
    "order_completed": ("#E3F2FD", "#0D47A1"),
    "payment_captured": ("#E8F5E9", "#2E7D32"),
    "payment_failed": ("#FFEBEE", "#C62828"),
    "payment_refunded": ("#FFF8E7", "#9A7B54"),
    "shipment_delivered": ("#E8F5E9", "#2E7D32"),
    "shipment_failed": ("#FFEBEE", "#C62828"),
    "refund_completed": ("#FFF8E7", "#9A7B54"),
    "return_completed": ("#FFF8E7", "#9A7B54"),
    "fraud_check_failed": ("#FFEBEE", "#C62828"),
    "fraud_check_review": ("#FFF8E7", "#9A7B54"),
}

#: Default (background, foreground) colour pair when a status is unmapped.
DEFAULT_BADGE_COLORS: Final[Tuple[str, str]] = ("#FAFAFA", "#767676")

#: Badge colour pair for pending / under-review return states.
RETURN_STATUS_PENDING_COLORS: Final[Tuple[str, str]] = ("#FFF8E7", "#9A7B54")

#: Badge colour pair for rejected / cancelled return states.
RETURN_STATUS_REJECTED_COLORS: Final[Tuple[str, str]] = ("#FFEBEE", "#C62828")

#: Badge colour pair for received / inspecting / completed return states.
RETURN_STATUS_RECEIVED_COLORS: Final[Tuple[str, str]] = ("#E3F2FD", "#0D47A1")

#: Badge colour pair for the default / fallback return status badge.
RETURN_STATUS_DEFAULT_COLORS: Final[Tuple[str, str]] = ("#E8F5E9", "#2E7D32")

# ==============================================================================
# 25. RETURN NUMBER FORMAT
# ==============================================================================
#: Prefix prepended to every auto-generated return number.
RETURN_NUMBER_PREFIX: Final[str] = "RET"

#: Date format embedded in the return number.
#:
#: Example: an order returned on 12 March 2026 would produce a date
#: fragment of ``260312``, yielding ``RET-260312-A1B2C3``.
RETURN_NUMBER_DATE_FORMAT: Final[str] = "%y%m%d"

#: Number of bytes of cryptographic randomness embedded in the
#: return number (passed to ``secrets.token_hex``). Each byte yields
#: two hex characters, so 3 bytes produce a 6-character suffix.
RETURN_NUMBER_TOKEN_BYTES: Final[int] = 3

# ==============================================================================
# 26. CSV EXPORT WHITELIST
# ==============================================================================
#: Whitelist of Order fields that are safe to include in CSV exports.
#:
#: Free-form JSON / PII-laden text fields are deliberately excluded.
CSV_EXPORT_FIELDS: Final[Tuple[str, ...]] = (
    "id",
    "order_number",
    "email",
    "status",
    "payment_status",
    "payment_method",
    "transaction_id",
    "currency",
    "subtotal",
    "discount_total",
    "shipping_cost",
    "tax_total",
    "total",
    "coupon_code",
    "tracking_number",
    "carrier",
    "is_active",
    "source",
    "fraud_check_status",
    "is_gift",
    "created_at",
    "updated_at",
    "completed_at",
)

# ==============================================================================
# 27. NOTIFICATION METHOD TOKENS
# ==============================================================================
#: Canonical identifiers for the notification channels recognised by
#: ``OrderStatusHistory.notification_method``.
class NotificationMethod:
    NONE: Final[str] = "none"
    EMAIL: Final[str] = "email"
    SMS: Final[str] = "sms"
    WEBHOOK: Final[str] = "webhook"
    PUSH: Final[str] = "push"

    ALL: Final[FrozenSet[str]] = frozenset({NONE, EMAIL, SMS, WEBHOOK, PUSH})

# ==============================================================================
# 28. SHIPMENT-ITEM CONDITION TOKENS
# ==============================================================================
#: Canonical identifiers for the pickup-condition values stored in
#: ``ShipmentItem.condition_at_pickup``.
class PickupCondition:
    NEW: Final[str] = "new"
    OPENED: Final[str] = "opened"
    REFURBISHED: Final[str] = "refurbished"

    ALL: Final[FrozenSet[str]] = frozenset({NEW, OPENED, REFURBISHED})

# ==============================================================================
# 29. ORDER NUMBER & SHIPMENT NUMBER PATTERNS
# ==============================================================================
#: Default prefix applied to every order number.
ORDER_NUMBER_PREFIX: Final[str] = "ORD"

#: Default prefix applied to every shipment number.
SHIPMENT_NUMBER_PREFIX: Final[str] = "SHP"

#: Default prefix applied to every invoice / return reference.
INVOICE_NUMBER_PREFIX: Final[str] = "INV"

# ==============================================================================
# 30. METADATA / JSON KEYS
# ==============================================================================
#: Canonical keys for free-form JSON metadata blobs.
class MetadataKey:
    """Reserved keys for JSON metadata fields."""

    #: Key under which the source request ID is recorded.
    REQUEST_ID: Final[str] = "request_id"

    #: Key under which the idempotency key is recorded.
    IDEMPOTENCY_KEY: Final[str] = "idempotency_key"

    #: Key under which a fraud-engine verdict is recorded.
    FRAUD_VERDICT: Final[str] = "fraud_verdict"

    #: Key under which a gateway request ID is recorded.
    GATEWAY_REQUEST_ID: Final[str] = "gateway_request_id"

    #: Key under which a risk engine score is recorded.
    RISK_ENGINE_SCORE: Final[str] = "risk_engine_score"

    ALL: Final[FrozenSet[str]] = frozenset(
        {REQUEST_ID, IDEMPOTENCY_KEY, FRAUD_VERDICT, GATEWAY_REQUEST_ID, RISK_ENGINE_SCORE}
    )

# ==============================================================================
# 31. LOGGER NAMES
# ==============================================================================
#: Canonical logger name for the orders app.
LOGGER_NAME: Final[str] = "apps.orders"

#: Canonical logger name for the orders services layer.
LOGGER_SERVICES: Final[str] = "apps.orders.services"

#: Canonical logger name for the orders selectors layer.
LOGGER_SELECTORS: Final[str] = "apps.orders.selectors"

#: Canonical logger name for the orders signal layer.
LOGGER_SIGNALS: Final[str] = "apps.orders.signals"

#: Canonical logger name for the orders admin layer.
LOGGER_ADMIN: Final[str] = "apps.orders.admin"

# ==============================================================================
# 32. PERMISSION CODENAMES
# ==============================================================================
#: Canonical Django permission codenames used by the orders app.
class Permission:
    CAN_VIEW_ORDER: Final[str] = "view_order"
    CAN_ADD_ORDER: Final[str] = "add_order"
    CAN_CHANGE_ORDER: Final[str] = "change_order"
    CAN_DELETE_ORDER: Final[str] = "delete_order"
    CAN_CANCEL_ORDER: Final[str] = "cancel_order"
    CAN_REFUND_ORDER: Final[str] = "refund_order"
    CAN_EXPORT_ORDERS: Final[str] = "export_orders"
    CAN_APPROVE_REFUND: Final[str] = "approve_refund"
    CAN_APPROVE_RETURN: Final[str] = "approve_return"
    CAN_MANAGE_SHIPMENT: Final[str] = "manage_shipment"

    ALL: Final[FrozenSet[str]] = frozenset(
        {
            CAN_VIEW_ORDER,
            CAN_ADD_ORDER,
            CAN_CHANGE_ORDER,
            CAN_DELETE_ORDER,
            CAN_CANCEL_ORDER,
            CAN_REFUND_ORDER,
            CAN_EXPORT_ORDERS,
            CAN_APPROVE_REFUND,
            CAN_APPROVE_RETURN,
            CAN_MANAGE_SHIPMENT,
        }
    )

# ==============================================================================
# 33. DATABASE / QUERY LIMITS
# ==============================================================================
#: Maximum number of objects loaded by an admin changelist query.
ADMIN_QUERY_HARD_LIMIT: Final[int] = 10_000

#: Default ``prefetch_related`` depth for nested serializers.
PREFETCH_DEFAULT_DEPTH: Final[int] = 2

#: Default ``select_related`` depth for nested serializers.
SELECT_RELATED_DEFAULT_DEPTH: Final[int] = 2

# ==============================================================================
# 34. TIME-OUTS
# ==============================================================================
#: Default cache TTL for short-lived querysets, in seconds.
CACHE_TTL_SHORT: Final[int] = 60

#: Default cache TTL for medium-lived aggregations, in seconds.
CACHE_TTL_MEDIUM: Final[int] = 300

#: Default cache TTL for long-lived reference data, in seconds.
CACHE_TTL_LONG: Final[int] = 3_600

# ==============================================================================
# 35. CACHE KEY TEMPLATES
# ==============================================================================
#: Redis-style namespace for orders-app cache keys.
CACHE_NAMESPACE: Final[str] = "orders"

#: Cache key for the active order count metric.
CACHE_KEY_ORDER_COUNT: Final[str] = "{ns}:count:active"

#: Cache key for the order-by-id lookup.
CACHE_KEY_ORDER_BY_ID: Final[str] = "{ns}:order:{order_id}"

#: Cache key for the order-by-number lookup.
CACHE_KEY_ORDER_BY_NUMBER: Final[str] = "{ns}:order:number:{order_number}"

#: Cache key for an order's full timeline.
CACHE_KEY_ORDER_TIMELINE: Final[str] = "{ns}:order:{order_id}:timeline"

#: Cache key prefix for order status aggregations.
CACHE_KEY_STATUS_AGGREGATION: Final[str] = "{ns}:agg:status"

# ==============================================================================
# 36. STRING PADDING / FORMATTING TOKENS
# ==============================================================================
#: Placeholder string for empty cells in the admin changelist.
EMPTY_CELL_PLACEHOLDER: Final[str] = "-"

#: Filename separator (dash) used in generated IDs.
ID_SEPARATOR: Final[str] = "-"

#: Hyphen used in human-readable status / event names.
LABEL_HYPHEN: Final[str] = "_"

# ==============================================================================
# 37. CART / WISHLIST (CROSS-APP) CONSTANTS
# ==============================================================================
#: Identifier for the wishlist unique constraint that historically
#: lived in this app. Retained for migration compatibility.
LEGACY_WISHLIST_CONSTRAINT_NAME: Final[str] = "unique_customer_product_wishlist"

# ==============================================================================
# 38. SEED / DEMO CONSTANTS
# ==============================================================================
#: Sample currency code used by management commands to seed test data.
SAMPLE_CURRENCY_CODE: Final[str] = "USD"

#: Sample tracking number used by demo seeders.
SAMPLE_TRACKING_NUMBER: Final[str] = "DEMO-0000-0000"

#: Sample SKU used by demo seeders.
SAMPLE_SKU: Final[str] = "DEMO-SKU"

#: Sample coupon code used by demo seeders.
SAMPLE_COUPON_CODE: Final[str] = "DEMOCOUPON"

# ==============================================================================
# 39. LOOKUP TABLES
# ==============================================================================
#: Map of (OrderStatus → human-readable singular noun) used by templates.
ORDER_STATUS_SINGULAR_NOUN: Final[Dict[str, str]] = {
    OrderStatus.PENDING: "pending order",
    OrderStatus.PROCESSING: "processing order",
    OrderStatus.SHIPPED: "shipped order",
    OrderStatus.DELIVERED: "delivered order",
    OrderStatus.CANCELLED: "cancelled order",
    OrderStatus.REFUNDED: "refunded order",
    OrderStatus.ON_HOLD: "order on hold",
    OrderStatus.PARTIALLY_SHIPPED: "partially-shipped order",
    OrderStatus.PARTIALLY_DELIVERED: "partially-delivered order",
    OrderStatus.BACKORDERED: "back-ordered order",
    OrderStatus.COMPLETED: "completed order",
    OrderStatus.FAILED: "failed order",
    OrderStatus.AWAITING_PAYMENT: "order awaiting payment",
    OrderStatus.PARTIALLY_REFUNDED: "partially-refunded order",
    OrderStatus.DISPUTED: "disputed order",
    OrderStatus.DRAFT: "draft order",
}

#: Map of (ShipmentStatus → human-readable singular noun).
SHIPMENT_STATUS_SINGULAR_NOUN: Final[Dict[str, str]] = {
    ShipmentStatus.PENDING: "pending shipment",
    ShipmentStatus.DISPATCHED: "dispatched shipment",
    ShipmentStatus.IN_TRANSIT: "in-transit shipment",
    ShipmentStatus.DELIVERED: "delivered shipment",
    ShipmentStatus.RETURNED: "returned shipment",
    ShipmentStatus.EXCEPTION: "shipment with exception",
    ShipmentStatus.OUT_FOR_DELIVERY: "shipment out for delivery",
    ShipmentStatus.FAILED_ATTEMPT: "shipment with failed delivery attempt",
    ShipmentStatus.AWAITING_PICKUP: "shipment awaiting pickup",
    ShipmentStatus.PICKED_UP: "picked-up shipment",
}

#: Map of (ReturnStatus → human-readable singular noun).
RETURN_STATUS_SINGULAR_NOUN: Final[Dict[str, str]] = {
    ReturnStatus.DRAFT: "draft return",
    ReturnStatus.REQUESTED: "requested return",
    ReturnStatus.UNDER_REVIEW: "return under review",
    ReturnStatus.APPROVED: "approved return",
    ReturnStatus.REJECTED: "rejected return",
    ReturnStatus.AWAITING_SHIPMENT: "return awaiting shipment",
    ReturnStatus.IN_TRANSIT: "return in transit",
    ReturnStatus.RECEIVED: "received return",
    ReturnStatus.INSPECTING: "return under inspection",
    ReturnStatus.REFUND_INITIATED: "return with refund initiated",
    ReturnStatus.COMPLETED: "completed return",
    ReturnStatus.CANCELLED: "cancelled return",
}

# ==============================================================================
# 40. CONVENIENCE ALIASES
# ==============================================================================
#: Re-export of the module-level defaults declared in models.py, kept
#: under the canonical names already imported elsewhere in the project.
MONEY_ZERO: Final[Decimal] = ZERO_DECIMAL_2
WEIGHT_ZERO: Final[Decimal] = ZERO_DECIMAL_3

#: Canonical default page-size alias used by services that paginate.
PAGE_SIZE: Final[int] = DEFAULT_PAGE_SIZE

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # 1. Order lifecycle
    "OrderStatus",
    # 2. Payment status
    "PaymentStatus",
    # 3. Source
    "OrderSource",
    # 4. Fraud
    "FraudCheckStatus",
    # 5. Item lifecycle
    "ItemStatus",
    "SavedForLaterReason",
    # 6. Shipment
    "ShipmentStatus",
    # 7. Payment record
    "PaymentState",
    "PaymentAttemptStatus",
    # 8. Refund
    "RefundStatus",
    "RefundMethod",
    "RefundReasonCategory",
    # 9. Tax
    "TaxMode",
    # 10. Discount
    "DiscountType",
    # 11. Note
    "NoteType",
    # 12. Attachment
    "AttachmentType",
    # 13. Timeline
    "TimelineEventType",
    # 14. Returns
    "ReturnType",
    "ReturnReasonCategory",
    "ReturnStatus",
    "RestockDecision",
    "InspectionResult",
    "ReturnImageType",
    # 15. Currency
    "Currency",
    # 16. Decimal precision
    "DecimalPrecision",
    # 17. Numeric defaults
    "ZERO_DECIMAL_2",
    "ZERO_DECIMAL_3",
    "DEFAULT_EXCHANGE_RATE",
    "MIN_EXCHANGE_RATE",
    "MIN_RISK_SCORE",
    "MAX_RISK_SCORE",
    "MIN_PERCENTAGE",
    "MAX_PERCENTAGE",
    "MIN_TAX_RATE",
    "MAX_TAX_RATE",
    "MIN_QUANTITY",
    "DEFAULT_QUANTITY",
    "DEFAULT_PAYMENT_ATTEMPTS",
    # 18. Module-level defaults
    "DEFAULT_CURRENCY_CODE",
    "DEFAULT_LOW_STOCK_THRESHOLD",
    "DEFAULT_ORDER_PAGE_SIZE",
    "DEFAULT_PAYMENT_METHOD",
    "DEFAULT_CARRIER_NAME",
    "DEFAULT_ORDER_ACTIVE_STATE",
    "DEFAULT_SHIPPING_COST",
    "DEFAULT_POSITION",
    "DEFAULT_LOW_STOCK_PERCENTAGE",
    # 19. Field length limits
    "FieldLength",
    # 20. Validation regex
    "PHONE_REGEX",
    "PHONE_MIN_LENGTH",
    "PHONE_MAX_LENGTH",
    # 21. Upload paths
    "ORDER_ATTACHMENT_FOLDER",
    "RETURN_IMAGE_FOLDER",
    "INVOICE_FOLDER",
    "SHIPMENT_FOLDER",
    "ARCHIVE_FOLDER",
    "TEMP_FOLDER",
    "EXPORT_FOLDER",
    "IMPORT_FOLDER",
    "DEFAULT_BINARY_EXTENSION",
    "DEFAULT_IMAGE_EXTENSION",
    # 22. Pagination
    "DEFAULT_PAGE_SIZE",
    "ADMIN_MAX_SHOW_ALL",
    "BULK_OPERATION_BATCH_SIZE",
    "EXPORT_BATCH_SIZE",
    # 23. Admin display formats
    "ADMIN_DATETIME_FORMAT",
    "ADMIN_DATE_FORMAT",
    "EXPORT_TIMESTAMP_FORMAT",
    "CSV_BOM",
    "CSV_CONTENT_TYPE",
    "CSV_EXPORT_FILENAME_PREFIX",
    "CSV_EXPORT_EXTENSION",
    "DEFAULT_INLINE_EXTRA",
    # 24. Admin badge colours
    "ORDER_STATUS_BADGE_COLORS",
    "PAYMENT_STATUS_BADGE_COLORS",
    "TIMELINE_EVENT_BADGE_COLORS",
    "DEFAULT_BADGE_COLORS",
    "RETURN_STATUS_PENDING_COLORS",
    "RETURN_STATUS_REJECTED_COLORS",
    "RETURN_STATUS_RECEIVED_COLORS",
    "RETURN_STATUS_DEFAULT_COLORS",
    # 25. Return number format
    "RETURN_NUMBER_PREFIX",
    "RETURN_NUMBER_DATE_FORMAT",
    "RETURN_NUMBER_TOKEN_BYTES",
    # 26. CSV whitelist
    "CSV_EXPORT_FIELDS",
    # 27. Notification methods
    "NotificationMethod",
    # 28. Pickup condition
    "PickupCondition",
    # 29. Number prefixes
    "ORDER_NUMBER_PREFIX",
    "SHIPMENT_NUMBER_PREFIX",
    "INVOICE_NUMBER_PREFIX",
    # 30. Metadata keys
    "MetadataKey",
    # 31. Loggers
    "LOGGER_NAME",
    "LOGGER_SERVICES",
    "LOGGER_SELECTORS",
    "LOGGER_SIGNALS",
    "LOGGER_ADMIN",
    # 32. Permissions
    "Permission",
    # 33. Database / query limits
    "ADMIN_QUERY_HARD_LIMIT",
    "PREFETCH_DEFAULT_DEPTH",
    "SELECT_RELATED_DEFAULT_DEPTH",
    # 34. Cache TTLs
    "CACHE_TTL_SHORT",
    "CACHE_TTL_MEDIUM",
    "CACHE_TTL_LONG",
    # 35. Cache key templates
    "CACHE_NAMESPACE",
    "CACHE_KEY_ORDER_COUNT",
    "CACHE_KEY_ORDER_BY_ID",
    "CACHE_KEY_ORDER_BY_NUMBER",
    "CACHE_KEY_ORDER_TIMELINE",
    "CACHE_KEY_STATUS_AGGREGATION",
    # 36. Formatting tokens
    "EMPTY_CELL_PLACEHOLDER",
    "ID_SEPARATOR",
    "LABEL_HYPHEN",
    # 37. Cross-app
    "LEGACY_WISHLIST_CONSTRAINT_NAME",
    # 38. Seed / demo
    "SAMPLE_CURRENCY_CODE",
    "SAMPLE_TRACKING_NUMBER",
    "SAMPLE_SKU",
    "SAMPLE_COUPON_CODE",
    # 39. Lookup tables
    "ORDER_STATUS_SINGULAR_NOUN",
    "SHIPMENT_STATUS_SINGULAR_NOUN",
    "RETURN_STATUS_SINGULAR_NOUN",
    # 40. Convenience aliases
    "MONEY_ZERO",
    "WEIGHT_ZERO",
    "PAGE_SIZE",
]