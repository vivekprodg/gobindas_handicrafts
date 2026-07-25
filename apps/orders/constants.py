from __future__ import annotations

from decimal import Decimal
from typing import Dict, Final, FrozenSet, Tuple

class OrderStatus:
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

    ALL: Final[FrozenSet[str]] = frozenset({
        PENDING, PROCESSING, SHIPPED, DELIVERED, CANCELLED, REFUNDED,
        ON_HOLD, PARTIALLY_SHIPPED, PARTIALLY_DELIVERED, BACKORDERED,
        COMPLETED, FAILED, AWAITING_PAYMENT, PARTIALLY_REFUNDED, DISPUTED, DRAFT
    })

    CANCELLABLE_FROM: Final[FrozenSet[str]] = frozenset({
        PENDING, AWAITING_PAYMENT, ON_HOLD, PROCESSING
    })
    TERMINAL_SUCCESS: Final[FrozenSet[str]] = frozenset({DELIVERED, COMPLETED})
    TERMINAL_FAILURE: Final[FrozenSet[str]] = frozenset({
        CANCELLED, REFUNDED, FAILED, PARTIALLY_REFUNDED
    })

class PaymentStatus:
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

    ALL: Final[FrozenSet[str]] = frozenset({
        PENDING, PARTIALLY_PAID, PAID, FAILED, REFUNDED, PARTIALLY_REFUNDED,
        AUTHORIZED, CAPTURED, VOIDED, DISPUTED, EXPIRED, PENDING_PAYMENT, PROCESSING
    })
    PAID_LIKE: Final[FrozenSet[str]] = frozenset({PAID, CAPTURED, REFUNDED})

class OrderSource:
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

class FraudCheckStatus:
    NOT_CHECKED: Final[str] = "not_checked"
    PENDING: Final[str] = "pending"
    PASSED: Final[str] = "passed"
    FAILED: Final[str] = "failed"
    MANUAL_REVIEW: Final[str] = "manual_review"

class ItemStatus:
    ACTIVE: Final[str] = "active"
    SAVED: Final[str] = "saved"
    REMOVED: Final[str] = "removed"
    EXPIRED: Final[str] = "expired"
    RETURNED: Final[str] = "returned"
    REFUNDED: Final[str] = "refunded"
    CANCELLED: Final[str] = "cancelled"
    PARTIALLY_RETURNED: Final[str] = "partially_returned"
    PARTIALLY_SHIPPED: Final[str] = "partially_shipped"

    SHIPPABLE: Final[FrozenSet[str]] = frozenset({ACTIVE, PARTIALLY_SHIPPED})
    RETURNABLE: Final[FrozenSet[str]] = frozenset({ACTIVE, PARTIALLY_SHIPPED})

class ShipmentStatus:
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

    IN_TRANSIT_LIKE: Final[FrozenSet[str]] = frozenset({
        DISPATCHED, IN_TRANSIT, OUT_FOR_DELIVERY
    })

class PaymentState:
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

class PaymentAttemptStatus:
    PENDING: Final[str] = "pending"
    SUCCESS: Final[str] = "success"
    FAILURE: Final[str] = "failure"
    TIMEOUT: Final[str] = "timeout"
    CANCELLED: Final[str] = "cancelled"
    REQUIRES_ACTION: Final[str] = "requires_action"
    THREE_DS_REQUIRED: Final[str] = "three_ds_required"

class RefundStatus:
    REQUESTED: Final[str] = "requested"
    APPROVED: Final[str] = "approved"
    PROCESSED: Final[str] = "processed"
    REJECTED: Final[str] = "rejected"
    PENDING: Final[str] = "pending"
    FAILED: Final[str] = "failed"
    CANCELLED: Final[str] = "cancelled"

    APPROVABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED})
    REJECTABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED, APPROVED})
    PROCESSABLE_FROM: Final[FrozenSet[str]] = frozenset({APPROVED})
    COMPLETABLE_FROM: Final[FrozenSet[str]] = frozenset({PROCESSED})

class RefundMethod:
    ORIGINAL: Final[str] = "original"
    STORE_CREDIT: Final[str] = "store_credit"
    BANK_TRANSFER: Final[str] = "bank_transfer"
    CHECK: Final[str] = "check"
    CASH: Final[str] = "cash"
    OTHER: Final[str] = "other"

class RefundReasonCategory:
    CUSTOMER_REQUEST: Final[str] = "customer_request"
    DEFECTIVE_PRODUCT: Final[str] = "defective_product"
    WRONG_ITEM: Final[str] = "wrong_item"
    NOT_AS_DESCRIBED: Final[str] = "not_as_described"
    DUPLICATE_CHARGE: Final[str] = "duplicate_charge"
    FRAUD: Final[str] = "fraud"
    GOODWILL: Final[str] = "goodwill"
    OTHER: Final[str] = "other"

class TaxMode:
    INCLUSIVE: Final[str] = "inclusive"
    EXCLUSIVE: Final[str] = "exclusive"

class DiscountType:
    COUPON: Final[str] = "coupon"
    PROMOTION: Final[str] = "promotion"
    LOYALTY: Final[str] = "loyalty"
    STAFF: Final[str] = "staff"
    GOODWILL: Final[str] = "goodwill"
    BULK: Final[str] = "bulk"
    SEASONAL: Final[str] = "seasonal"
    OTHER: Final[str] = "other"

class NoteType:
    CUSTOMER: Final[str] = "customer"
    OPERATOR: Final[str] = "operator"
    GIFT: Final[str] = "gift"
    DELIVERY: Final[str] = "delivery"
    SYSTEM: Final[str] = "system"

class AttachmentType:
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

class TimelineEventType:
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

class ReturnType:
    REFUND: Final[str] = "refund"
    REPLACEMENT: Final[str] = "replacement"
    EXCHANGE: Final[str] = "exchange"
    STORE_CREDIT: Final[str] = "store_credit"
    REPAIR: Final[str] = "repair"

class ReturnReasonCategory:
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

class ReturnStatus:
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

    RESOLVED: Final[FrozenSet[str]] = frozenset({COMPLETED, REJECTED, CANCELLED})
    APPROVABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED})
    REJECTABLE_FROM: Final[FrozenSet[str]] = frozenset({REQUESTED})
    RECEIVABLE_FROM: Final[FrozenSet[str]] = frozenset({AWAITING_SHIPMENT, IN_TRANSIT})
    COMPLETABLE_FROM: Final[FrozenSet[str]] = frozenset({RECEIVED, INSPECTING})

class Currency:
    DEFAULT_CODE: Final[str] = "NPR"
    DEFAULT_SYMBOL: Final[str] = "NPR"
    DEFAULT_BASE: Final[str] = "NPR"

class DecimalPrecision:
    ORDER_MONEY: Final[Tuple[int, int]] = (14, 2)
    LINE_MONEY: Final[Tuple[int, int]] = (12, 2)
    ITEM_WEIGHT: Final[Tuple[int, int]] = (10, 3)
    SHIPMENT_WEIGHT: Final[Tuple[int, int]] = (14, 3)
    VARIANT_WEIGHT: Final[Tuple[int, int]] = (10, 3)
    LIFECYCLE_QUANTITY: Final[Tuple[int, int]] = (14, 2)
    SHIPMENT_COST: Final[Tuple[int, int]] = (12, 2)
    REFUND_AMOUNT: Final[Tuple[int, int]] = (14, 2)
    MONEY_14_2: Final[Tuple[int, int]] = (14, 2)
    COUPON_DISCOUNT: Final[Tuple[int, int]] = (12, 2)
    BASE_CURRENCY_TOTAL: Final[Tuple[int, int]] = (18, 2)
    EXCHANGE_RATE: Final[Tuple[int, int]] = (18, 8)
    RISK_SCORE: Final[Tuple[int, int]] = (5, 2)
    TAX_RATE: Final[Tuple[int, int]] = (8, 4)
    PERCENTAGE: Final[Tuple[int, int]] = (5, 2)
    COORDINATE: Final[Tuple[int, int]] = (9, 6)

class FieldLength:
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

    OLD_STATUS: Final[int] = 50
    NEW_STATUS: Final[int] = 50
    NOTIFICATION_METHOD: Final[int] = 32

    SHIPMENT_NUMBER: Final[int] = 100
    SHIPMENT_TRACKING_NUMBER: Final[int] = 150
    CARRIER_API_INTEGRATION_ID: Final[int] = 120
    CARRIER_SERVICE_LEVEL: Final[int] = 64
    SERIAL_TRACKING: Final[int] = 120
    CONDITION_AT_PICKUP: Final[int] = 32

    GATEWAY: Final[int] = 100
    PAYMENT_METHOD_TYPE: Final[int] = 64
    GATEWAY_RESPONSE_CODE: Final[int] = 64

    REFUND_STATUS: Final[int] = 20
    REFUND_METHOD: Final[int] = 32
    REFUND_REASON_CATEGORY: Final[int] = 64
    GATEWAY_REFUND_ID: Final[int] = 120

    TAX_CLASS: Final[int] = 64
    TAX_NAME: Final[int] = 120
    TAX_AUTHORITY_CODE: Final[int] = 64
    JURISDICTION: Final[int] = 120
    TAX_MODE: Final[int] = 16

    DISCOUNT_TYPE: Final[int] = 32
    DISCOUNT_SOURCE: Final[int] = 64
    DISCOUNT_CODE: Final[int] = 120
    DISCOUNT_NAME: Final[int] = 255
    PROMOTION_ID: Final[int] = 120

    COUPON_CODE: Final[int] = 50
    NOTE_TYPE: Final[int] = 32
    ATTACHMENT_TYPE: Final[int] = 32
    ORIGINAL_FILENAME: Final[int] = 255
    MIME_TYPE: Final[int] = 120
    DESCRIPTION: Final[int] = 255

    EVENT_TYPE: Final[int] = 48
    EVENT_TITLE: Final[int] = 255
    REFERENCE_MODEL: Final[int] = 80
    REFERENCE_ID: Final[int] = 80
    ICON: Final[int] = 64
    COLOR: Final[int] = 32

    RETURN_NUMBER: Final[int] = 50
    RETURN_TYPE: Final[int] = 24
    REASON_CATEGORY: Final[int] = 48
    RETURN_STATUS: Final[int] = 24
    RESTOCK_DECISION: Final[int] = 32
    RESTOCK_LOCATION: Final[int] = 120

    CONDITION_RECEIVED: Final[int] = 64
    INSPECTION_RESULT: Final[int] = 16
    IMAGE_TYPE: Final[int] = 24
    CAPTION: Final[int] = 255

ZERO_DECIMAL_2: Final[Decimal] = Decimal("0.00")
ZERO_DECIMAL_3: Final[Decimal] = Decimal("0.000")
DEFAULT_EXCHANGE_RATE: Final[Decimal] = Decimal("1.00000000")
MIN_EXCHANGE_RATE: Final[Decimal] = Decimal("0.00000001")
MIN_RISK_SCORE: Final[Decimal] = Decimal("0.00")
MAX_RISK_SCORE: Final[Decimal] = Decimal("100.00")
MIN_PERCENTAGE: Final[Decimal] = Decimal("0.00")
MAX_PERCENTAGE: Final[Decimal] = Decimal("100.00")
MIN_TAX_RATE: Final[Decimal] = Decimal("0.0000")
MAX_TAX_RATE: Final[Decimal] = Decimal("1.0000")
MIN_QUANTITY: Final[int] = 1
DEFAULT_QUANTITY: Final[int] = 1
DEFAULT_PAYMENT_ATTEMPTS: Final[int] = 1

DEFAULT_CURRENCY_CODE: Final[str] = Currency.DEFAULT_CODE
DEFAULT_LOW_STOCK_THRESHOLD: Final[int] = 5
DEFAULT_ORDER_PAGE_SIZE: Final[int] = 25
DEFAULT_PAYMENT_METHOD: Final[str] = "manual"
DEFAULT_CARRIER_NAME: Final[str] = "Unknown"
DEFAULT_ORDER_ACTIVE_STATE: Final[bool] = True
DEFAULT_SHIPPING_COST: Final[Decimal] = ZERO_DECIMAL_2
DEFAULT_POSITION: Final[int] = 0

PHONE_REGEX: Final[str] = r"^\+?[0-9\s\-\(\)]{7,20}$"
ORDER_ATTACHMENT_FOLDER: Final[str] = "orders/attachments"
RETURN_IMAGE_FOLDER: Final[str] = "orders/returns"
INVOICE_FOLDER: Final[str] = "orders/invoices"
SHIPMENT_FOLDER: Final[str] = "orders/shipments"
ARCHIVE_FOLDER: Final[str] = "orders/archive"
TEMP_FOLDER: Final[str] = "orders/tmp"
EXPORT_FOLDER: Final[str] = "orders/exports"

DEFAULT_PAGE_SIZE: Final[int] = 25
ADMIN_MAX_SHOW_ALL: Final[int] = 200
BULK_OPERATION_BATCH_SIZE: Final[int] = 500
EXPORT_BATCH_SIZE: Final[int] = 1000

ADMIN_DATETIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M"
ADMIN_DATE_FORMAT: Final[str] = "%Y-%m-%d"
EXPORT_TIMESTAMP_FORMAT: Final[str] = "%Y%m%d_%H%M%S"
CSV_BOM: Final[str] = "\ufeff"
CSV_CONTENT_TYPE: Final[str] = "text/csv; charset=utf-8"
CSV_EXPORT_FILENAME_PREFIX: Final[str] = "orders_export_"
CSV_EXPORT_EXTENSION: Final[str] = ".csv"

RETURN_NUMBER_PREFIX: Final[str] = "RET"
RETURN_NUMBER_DATE_FORMAT: Final[str] = "%y%m%d"
RETURN_NUMBER_TOKEN_BYTES: Final[int] = 3

CSV_EXPORT_FIELDS: Final[Tuple[str, ...]] = (
    "id", "order_number", "email", "status", "payment_status", "payment_method",
    "transaction_id", "currency", "subtotal", "discount_total", "shipping_cost",
    "tax_total", "total", "coupon_code", "tracking_number", "carrier",
    "is_active", "source", "fraud_check_status", "is_gift", "created_at",
    "updated_at", "completed_at",
)

ORDER_NUMBER_PREFIX: Final[str] = "ORD"
SHIPMENT_NUMBER_PREFIX: Final[str] = "SHP"
INVOICE_NUMBER_PREFIX: Final[str] = "INV"

LOGGER_NAME: Final[str] = "apps.orders"
LOGGER_SERVICES: Final[str] = "apps.orders.services"
LOGGER_SELECTORS: Final[str] = "apps.orders.selectors"
LOGGER_SIGNALS: Final[str] = "apps.orders.signals"
LOGGER_ADMIN: Final[str] = "apps.orders.admin"

CACHE_NAMESPACE: Final[str] = "orders"
CACHE_KEY_ORDER_COUNT: Final[str] = "{ns}:count:active"
CACHE_KEY_ORDER_BY_ID: Final[str] = "{ns}:order:{order_id}"
CACHE_KEY_ORDER_BY_NUMBER: Final[str] = "{ns}:order:number:{order_number}"
CACHE_KEY_ORDER_TIMELINE: Final[str] = "{ns}:order:{order_id}:timeline"
CACHE_KEY_STATUS_AGGREGATION: Final[str] = "{ns}:agg:status"

LABEL_HYPHEN: Final[str] = "_"
MONEY_ZERO: Final[Decimal] = ZERO_DECIMAL_2
WEIGHT_ZERO: Final[Decimal] = ZERO_DECIMAL_3
PAGE_SIZE: Final[int] = DEFAULT_PAGE_SIZE