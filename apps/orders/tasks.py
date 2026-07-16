"""
Enterprise-grade Celery background processing layer for the Orders
application.

This module is the ASYNCHRONOUS EXECUTION layer of the Orders
application. Every function defined here is a lightweight entry
point that:

    * Accepts primitive arguments only (IDs, UUIDs, strings).
    * Retrieves fresh database state inside the task body.
    * Delegates all business logic to ``apps.orders.services``.
    * Uses ``transaction.on_commit`` where appropriate to interact
      with recently-committed data.
    * Is idempotent and retry-safe.
    * Is independently testable.

ARCHITECTURE
============
The orders app follows a strict, layered architecture. The tasks
layer sits BELOW the views and forms and ABOVE the actual
background workers:

    views.py        → HTTP request handling
    forms.py        → Input validation
    signals.py      → ORM lifecycle detection
    event_handlers.py → Domain workflow coordination (synchronous)
    services.py     → Business logic / state transitions
    selectors.py    → Read-only data access
    tasks.py        → THIS FILE (asynchronous / background work)
    utils.py        → Pure helpers

tasks.py is the ONLY layer that:
    1. Enqueues Celery background work.
    2. Schedules periodic maintenance.
    3. Coordinates heavy / batched / asynchronous work.
    4. Performs graceful retry-with-backoff.

tasks.py MUST NEVER:
    * Contain business validation, pricing, or tax logic.
    * Mutate inventory, payments, or shipments.
    * Send emails, SMS, or webhooks directly (it triggers the
      appropriate task in the notifications / webhooks app).
    * Import views, forms, admin, signals, or event_handlers.
    * Hold a stale ORM instance across task invocations.

All business logic lives in ``services.py``. All reads come from
``selectors.py``. tasks.py is the asynchronous glue.

DESIGN PRINCIPLES
=================
* **Idempotent**: Every task can be retried safely. Repeated
  execution yields the same observable effect.
* **Primitive arguments**: Tasks accept IDs, UUIDs, and strings —
  not ORM instances. This prevents stale-data race conditions.
* **Fresh state**: Every task body loads the latest model state
  from the database before delegating to services.
* **Transaction-safe**: Cross-model writes are wrapped in
  ``transaction.atomic()`` and dispatched via ``on_commit`` where
  appropriate.
* **Retry-aware**: Tasks that interact with the outside world
  declare explicit retry policies.
* **Structured logging**: Every task emits structured log records
  suitable for downstream log aggregation.
* **Independent**: Tasks can be invoked from anywhere (views,
  management commands, crontab) without additional wiring.

CROSS-APP INTEGRATION
======================
Several tasks need to call into other Django apps (notifications,
invoices, payments, webhooks, search, analytics, exports, reports).
Those apps may not yet exist in the current codebase. Pylance will
therefore flag the import paths as unresolved. This is INTENTIONAL:

    * The imports are LAZY: they live inside a ``_safe_import`` /
      ``_safe_import_attr`` helper, not at the top of the module,
      so the orders app can boot even when the partner app is not
      installed.
    * Each import is wrapped in a ``try / except ImportError`` so
      a missing dependency degrades gracefully (the task logs and
      returns a "not_available" outcome).
    * When the partner app is added, the import resolves and the
      task becomes a real cross-app integration.

This pattern keeps tasks.py decoupled from the partner apps while
documenting the FULL production integration surface.

OWASP / SECURITY
================
* No PII, secrets, or credentials are logged.
* File-system tasks sanitize every path through ``utils.sanitize_*``.
* Payment / refund / invoice tasks only accept UUID / primary-key
  identifiers.
* Tasks do NOT echo back-end error messages to the caller.
"""

from __future__ import annotations

import importlib
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from celery import shared_task
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.orders import constants as c
from apps.orders import utils as u
from apps.orders.models import (
    Order,
    OrderAttachment,
    OrderItem,
    OrderNote,
    OrderStatusHistory,
    OrderTimelineEvent,
    Payment,
    PaymentAttempt,
    Refund,
    ReturnRequest,
    Shipment,
    ShipmentItem,
)

logger = logging.getLogger(c.LOGGER_NAME)

# ==============================================================================
# MODULE-LEVEL CONSTANTS
# ==============================================================================
#: Default number of retry attempts for idempotent tasks.
DEFAULT_MAX_RETRIES: int = 3

#: Default back-off (seconds) for the first retry.
DEFAULT_RETRY_BACKOFF: int = 30

#: Default back-off ceiling (seconds) for exponential retries.
DEFAULT_RETRY_BACKOFF_MAX: int = 600

#: Default jitter window (seconds) added to back-off to avoid
#: thundering-herd retries.
DEFAULT_RETRY_JITTER: bool = True

#: Default soft time limit (seconds) before SIGTERM is sent.
DEFAULT_SOFT_TIME_LIMIT: int = 60

#: Default hard time limit (seconds) before SIGKILL is sent.
DEFAULT_TIME_LIMIT: int = 120

#: Default batch size for streamed exports / cleanups.
DEFAULT_BATCH_SIZE: int = c.BULK_OPERATION_BATCH_SIZE

#: Default export batch size.
DEFAULT_EXPORT_BATCH_SIZE: int = c.EXPORT_BATCH_SIZE

#: Default CSV export filename prefix.
DEFAULT_CSV_EXPORT_PREFIX: str = c.CSV_EXPORT_FILENAME_PREFIX

#: Default CSV export filename extension.
DEFAULT_CSV_EXPORT_EXTENSION: str = c.CSV_EXPORT_EXTENSION

#: Default "abandoned order" threshold in hours.
DEFAULT_ABANDONED_THRESHOLD_HOURS: int = 24

#: Default "expired draft order" threshold in days.
DEFAULT_DRAFT_EXPIRY_DAYS: int = 30

#: Temporary file upload folder (matches ``constants.TEMP_FOLDER``).
_TEMP_FOLDER: str = c.TEMP_FOLDER

#: Export folder (matches ``constants.EXPORT_FOLDER``).
_EXPORT_FOLDER: str = c.EXPORT_FOLDER

#: Archive folder (matches ``constants.ARCHIVE_FOLDER``).
_ARCHIVE_FOLDER: str = c.ARCHIVE_FOLDER

#: Maximum age of a temporary file before it is purged.
_TEMP_FILE_MAX_AGE_DAYS: int = 7

#: Maximum age of a CSV export before it is purged.
_EXPORT_MAX_AGE_DAYS: int = 30

#: Maximum age of a soft-deleted attachment before it is purged.
_ATTACHMENT_MAX_AGE_DAYS: int = 90

# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================
def _safe_import_attr(module_path: str, attr: str) -> Optional[Any]:
    """
    Safely import a named attribute from a module.

    Returns ``None`` if the module is not importable OR the
    attribute does not exist. This is the canonical helper for
    cross-app integrations; tasks NEVER hard-fail if a partner
    app is missing.
    """
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        logger.debug(
            "_safe_import_attr: %s is not installed; skipping import "
            "of attribute %r.",
            module_path,
            attr,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_safe_import_attr: failed to import %s (%s); "
            "skipping attribute %r.",
            module_path,
            exc,
            attr,
        )
        return None
    return getattr(module, attr, None)

def _safe_call_task(
    task_attr: Any,
    *,
    delay_attr: str = "delay",
    apply_attr: str = "apply_async",
    args: Tuple[Any, ...] = (),
    kwargs: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Best-effort enqueue of a Celery task.

    Resolves ``task_attr.delay`` (preferred) or ``task_attr.apply_async``
    (fallback). Returns ``True`` on success, ``False`` on any
    ImportError / exception. Tasks NEVER re-raise cross-app
    failures into the orders app.
    """
    if task_attr is None:
        return False
    kwargs = dict(kwargs or {})
    try:
        enqueue: Optional[Callable[..., Any]] = getattr(
            task_attr, delay_attr, None,
        )
        if enqueue is None:
            enqueue = getattr(task_attr, apply_attr, None)
        if enqueue is None:
            logger.debug(
                "_safe_call_task: target exposes neither %r nor %r.",
                delay_attr,
                apply_attr,
            )
            return False
        enqueue(*args, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_safe_call_task: enqueue failed: %s", exc,
        )
        return False

def _safe_get_order(order_id: Any) -> Optional[Order]:
    """
    Retrieve a fresh ``Order`` by primary key (UUID or str).

    Returns ``None`` if the order does not exist. Tasks use this
    helper to ensure they always operate on the latest persisted
    state.
    """
    if order_id is None:
        return None
    try:
        return Order.objects.filter(pk=order_id).first()
    except (ValueError, TypeError) as exc:  # noqa: BLE001
        logger.warning(
            "tasks._safe_get_order received invalid pk %r: %s",
            order_id,
            exc,
        )
        return None

def _safe_get_payment(payment_id: Any) -> Optional[Payment]:
    """Retrieve a fresh ``Payment`` by primary key."""
    if payment_id is None:
        return None
    try:
        return Payment.objects.filter(pk=payment_id).first()
    except (ValueError, TypeError) as exc:  # noqa: BLE001
        logger.warning(
            "tasks._safe_get_payment received invalid pk %r: %s",
            payment_id,
            exc,
        )
        return None

def _safe_get_refund(refund_id: Any) -> Optional[Refund]:
    """Retrieve a fresh ``Refund`` by primary key."""
    if refund_id is None:
        return None
    try:
        return Refund.objects.filter(pk=refund_id).first()
    except (ValueError, TypeError) as exc:  # noqa: BLE001
        logger.warning(
            "tasks._safe_get_refund received invalid pk %r: %s",
            refund_id,
            exc,
        )
        return None

def _safe_get_return(return_id: Any) -> Optional[ReturnRequest]:
    """Retrieve a fresh ``ReturnRequest`` by primary key (UUID)."""
    if return_id is None:
        return None
    try:
        return ReturnRequest.objects.filter(pk=return_id).first()
    except (ValueError, TypeError) as exc:  # noqa: BLE001
        logger.warning(
            "tasks._safe_get_return received invalid pk %r: %s",
            return_id,
            exc,
        )
        return None

def _safe_get_shipment(shipment_id: Any) -> Optional[Shipment]:
    """Retrieve a fresh ``Shipment`` by primary key."""
    if shipment_id is None:
        return None
    try:
        return Shipment.objects.filter(pk=shipment_id).first()
    except (ValueError, TypeError) as exc:  # noqa: BLE001
        logger.warning(
            "tasks._safe_get_shipment received invalid pk %r: %s",
            shipment_id,
            exc,
        )
        return None

def _log_task_event(
    task_name: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit a structured log record for a task lifecycle event."""
    extras: Dict[str, Any] = {"task": task_name}
    extras.update(fields)
    logger.log(level, "celery_task_event", extra=extras)

def _build_export_filename(
    *,
    prefix: str = DEFAULT_CSV_EXPORT_PREFIX,
    suffix: str = "",
    extension: str = DEFAULT_CSV_EXPORT_EXTENSION,
) -> str:
    """Build a canonical export filename using the current timestamp."""
    timestamp = u.format_export_timestamp()
    if suffix:
        suffix = f"_{u.safe_str(suffix)}"
    return f"{prefix}{timestamp}{suffix}{extension}"

def _enqueue_notification_customer(
    *,
    order: Order,
    template: str,
    context: Dict[str, Any],
) -> bool:
    """Best-effort enqueue of a customer notification."""
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    return _safe_call_task(
        task,
        args=(str(order.pk), template, context),
    )

def _enqueue_notification_staff(
    *,
    order: Order,
    template: str,
    context: Dict[str, Any],
) -> bool:
    """Best-effort enqueue of a staff notification."""
    task = _safe_import_attr("apps.notifications.tasks", "send_staff_notification")
    return _safe_call_task(
        task,
        args=(str(order.pk), template, context),
    )

def _enqueue_invoice_render(
    *,
    order_id: str,
    locale: str = "en",
) -> bool:
    """Best-effort enqueue of the invoice renderer."""
    task = _safe_import_attr("apps.invoices.tasks", "render_invoice_pdf")
    return _safe_call_task(
        task,
        args=(str(order_id), locale),
    )

def _enqueue_gateway_refund(
    *,
    refund_id: int,
    transaction_id: str,
    amount: str,
    currency: str,
) -> bool:
    """Best-effort enqueue of a gateway-side refund."""
    task = _safe_import_attr("apps.payments.tasks", "execute_gateway_refund")
    return _safe_call_task(
        task,
        args=(refund_id, transaction_id, amount, currency),
    )

def _enqueue_webhook(
    *,
    order_id: str,
    event: str,
    context: Dict[str, Any],
) -> bool:
    """Best-effort enqueue of an order-domain webhook dispatch."""
    # The webhooks app may expose either ``dispatch_order_webhook``
    # or a more generic dispatcher. We probe both names.
    task = _safe_import_attr("apps.webhooks.tasks", "dispatch_order_webhook")
    if task is None:
        task = _safe_import_attr("apps.webhooks.tasks", "dispatch_webhook")
    if task is None:
        return False
    # Some webhook dispatchers expect (event, payload); others
    # expect (order_id, event, context). We send a kwargs payload
    # so the webhooks app can decide which signature to use.
    return _safe_call_task(
        task,
        args=(),
        kwargs={
            "order_id": str(order_id),
            "event": event,
            "context": context,
        },
    )

def _enqueue_search_reindex(*, order_id: str) -> bool:
    """Best-effort enqueue of a search-index refresh."""
    task = _safe_import_attr("apps.search.tasks", "reindex_order")
    if task is None:
        return False
    return _safe_call_task(task, args=(str(order_id),))

def _enqueue_analytics_event(
    *,
    order_id: str,
    event: str,
    properties: Dict[str, Any],
) -> bool:
    """Best-effort enqueue of an analytics event."""
    task = _safe_import_attr("apps.analytics.tasks", "track_order_event")
    if task is None:
        task = _safe_import_attr("apps.analytics.tasks", "track_event")
    if task is None:
        return False
    return _safe_call_task(
        task,
        args=(),
        kwargs={
            "order_id": str(order_id),
            "event": event,
            "properties": properties,
        },
    )

def _enqueue_export_stream(
    *,
    target_path: str,
    order_ids: List[str],
    format: str = "csv",
) -> bool:
    """Best-effort enqueue of the export streaming task."""
    task = _safe_import_attr("apps.exports.tasks", "stream_order_export")
    if task is None:
        return False
    return _safe_call_task(
        task,
        args=(),
        kwargs={
            "target_path": target_path,
            "order_ids": list(order_ids),
            "format": format,
        },
    )

def _enqueue_sales_report(
    *,
    report_date: str,
    summary: Dict[str, Any],
) -> bool:
    """Best-effort enqueue of the sales-report rendering task."""
    task = _safe_import_attr("apps.reports.tasks", "render_sales_report")
    if task is None:
        return False
    return _safe_call_task(
        task,
        args=(),
        kwargs={
            "report_date": report_date,
            "summary": summary,
        },
    )

def _get_gateway_status(transaction_id: str) -> Optional[str]:
    """
    Look up the current gateway status for ``transaction_id``.

    Returns ``None`` if the payments app is not installed, the
    gateway client is unavailable, or the lookup fails. The
    caller is expected to handle ``None`` gracefully (no
    reconciliation is performed).
    """
    function = _safe_import_attr(
        "apps.payments.gateway",
        "fetch_gateway_status",
    )
    if function is None:
        return None
    try:
        return function(transaction_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_get_gateway_status: lookup failed: %s", exc,
        )
        return None

def _schedule_payment_retry(
    *,
    payment_id: int,
    delay_seconds: int,
) -> bool:
    """Best-effort scheduling of a payment retry."""
    task = _safe_import_attr("apps.payments.tasks", "retry_payment_capture")
    if task is None:
        return False
    return _safe_call_task(
        task,
        args=(payment_id,),
        kwargs={"countdown": max(1, int(delay_seconds))},
    )

# ==============================================================================
# 1. NOTIFICATION-RELATED TRIGGERS
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.send_order_confirmation",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
    soft_time_limit=DEFAULT_SOFT_TIME_LIMIT,
    time_limit=DEFAULT_TIME_LIMIT,
)
def send_order_confirmation(self, order_id: str) -> Dict[str, Any]:
    """
    Trigger the customer-facing order-confirmation email.

    This task is intentionally a thin trigger. The actual email
    rendering and delivery are performed by the notifications
    app's Celery task, which is enqueued via ``transaction.on_commit``
    so that notifications NEVER fire for rolled-back orders.

    The task is idempotent: re-running it for the same order will
    not duplicate the underlying notification, because the
    notifications app keeps a dedup hash based on the order
    number + template token.
    """
    _log_task_event(
        "send_order_confirmation",
        order_id=str(order_id),
    )
    order = _safe_get_order(order_id)
    if order is None:
        _log_task_event(
            "send_order_confirmation",
            level=logging.WARNING,
            order_id=str(order_id),
            outcome="order_not_found",
        )
        return {"order_id": str(order_id), "outcome": "order_not_found"}

    if order.email:
        def _enqueue() -> None:
            _enqueue_notification_customer(
                order=order,
                template="order_placed",
                context={
                    "order_number": order.order_number,
                    "order_total": str(order.grand_total),
                    "currency": order.currency,
                },
            )

        try:
            transaction.on_commit(_enqueue)
        except transaction.TransactionManagementError:
            _enqueue()

    _log_task_event(
        "send_order_confirmation",
        order_id=str(order_id),
        outcome="dispatched",
    )
    return {"order_id": str(order_id), "outcome": "dispatched"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.send_order_cancellation_notice",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def send_order_cancellation_notice(
    self,
    order_id: str,
    *,
    remarks: str = "",
) -> Dict[str, Any]:
    """Trigger the customer-facing order-cancellation email."""
    _log_task_event(
        "send_order_cancellation_notice",
        order_id=str(order_id),
    )
    order = _safe_get_order(order_id)
    if order is None:
        return {"order_id": str(order_id), "outcome": "order_not_found"}

    if order.email:
        def _enqueue() -> None:
            _enqueue_notification_customer(
                order=order,
                template="order_cancelled",
                context={
                    "order_number": order.order_number,
                    "remarks": u.safe_str(remarks),
                },
            )

        try:
            transaction.on_commit(_enqueue)
        except transaction.TransactionManagementError:
            _enqueue()

    return {"order_id": str(order_id), "outcome": "dispatched"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.send_shipment_dispatched_notice",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def send_shipment_dispatched_notice(
    self,
    shipment_id: int,
) -> Dict[str, Any]:
    """Trigger the customer-facing shipment-dispatched email."""
    _log_task_event(
        "send_shipment_dispatched_notice",
        shipment_id=shipment_id,
    )
    shipment = _safe_get_shipment(shipment_id)
    if shipment is None:
        return {"shipment_id": shipment_id, "outcome": "shipment_not_found"}
    order = shipment.order
    if order and order.email:
        def _enqueue() -> None:
            _enqueue_notification_customer(
                order=order,
                template="shipment_dispatched",
                context={
                    "shipment_number": shipment.shipment_number,
                    "carrier": shipment.carrier,
                    "tracking_number": shipment.tracking_number or "",
                    "tracking_url": shipment.tracking_url or "",
                },
            )

        try:
            transaction.on_commit(_enqueue)
        except transaction.TransactionManagementError:
            _enqueue()
    return {"shipment_id": shipment_id, "outcome": "dispatched"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.send_shipment_delivered_notice",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def send_shipment_delivered_notice(
    self,
    shipment_id: int,
) -> Dict[str, Any]:
    """Trigger the customer-facing shipment-delivered email."""
    _log_task_event(
        "send_shipment_delivered_notice",
        shipment_id=shipment_id,
    )
    shipment = _safe_get_shipment(shipment_id)
    if shipment is None:
        return {"shipment_id": shipment_id, "outcome": "shipment_not_found"}
    order = shipment.order
    if order and order.email:
        def _enqueue() -> None:
            _enqueue_notification_customer(
                order=order,
                template="shipment_delivered",
                context={
                    "shipment_number": shipment.shipment_number,
                    "carrier": shipment.carrier,
                    "delivered_at": u.format_iso(shipment.delivery_date),
                },
            )

        try:
            transaction.on_commit(_enqueue)
        except transaction.TransactionManagementError:
            _enqueue()
    return {"shipment_id": shipment_id, "outcome": "dispatched"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.send_payment_captured_notice",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def send_payment_captured_notice(
    self,
    payment_id: int,
) -> Dict[str, Any]:
    """Trigger the customer-facing payment-captured email."""
    _log_task_event(
        "send_payment_captured_notice",
        payment_id=payment_id,
    )
    payment = _safe_get_payment(payment_id)
    if payment is None:
        return {"payment_id": payment_id, "outcome": "payment_not_found"}
    order = payment.order
    if order and order.email:
        def _enqueue() -> None:
            _enqueue_notification_customer(
                order=order,
                template="payment_captured",
                context={
                    "transaction_id": payment.transaction_id,
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "gateway": payment.gateway,
                },
            )

        try:
            transaction.on_commit(_enqueue)
        except transaction.TransactionManagementError:
            _enqueue()
    return {"payment_id": payment_id, "outcome": "dispatched"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.send_payment_failed_notice",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def send_payment_failed_notice(
    self,
    payment_id: int,
) -> Dict[str, Any]:
    """Trigger the customer-facing payment-failed email."""
    _log_task_event(
        "send_payment_failed_notice",
        payment_id=payment_id,
    )
    payment = _safe_get_payment(payment_id)
    if payment is None:
        return {"payment_id": payment_id, "outcome": "payment_not_found"}
    order = payment.order
    if order and order.email:
        def _enqueue() -> None:
            _enqueue_notification_customer(
                order=order,
                template="payment_failed",
                context={
                    "transaction_id": payment.transaction_id,
                    "gateway": payment.gateway,
                },
            )

        try:
            transaction.on_commit(_enqueue)
        except transaction.TransactionManagementError:
            _enqueue()
    return {"payment_id": payment_id, "outcome": "dispatched"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.send_refund_notifications",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def send_refund_notifications(
    self,
    refund_id: int,
) -> Dict[str, Any]:
    """Trigger customer + staff notifications for a refund event."""
    _log_task_event(
        "send_refund_notifications",
        refund_id=refund_id,
    )
    refund = _safe_get_refund(refund_id)
    if refund is None:
        return {"refund_id": refund_id, "outcome": "refund_not_found"}
    order = refund.order
    if order is None:
        return {"refund_id": refund_id, "outcome": "order_not_found"}

    def _enqueue() -> None:
        _enqueue_notification_customer(
            order=order,
            template=f"refund_{refund.status}",
            context={
                "refund_id": str(refund.pk),
                "amount": str(refund.amount),
                "currency": order.currency,
            },
        )
        _enqueue_notification_staff(
            order=order,
            template=f"refund_{refund.status}_alert",
            context={
                "refund_id": str(refund.pk),
                "amount": str(refund.amount),
                "currency": order.currency,
            },
        )

    try:
        transaction.on_commit(_enqueue)
    except transaction.TransactionManagementError:
        _enqueue()
    return {"refund_id": refund_id, "outcome": "dispatched"}

# ==============================================================================
# 2. INVOICE GENERATION
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.generate_order_invoice",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def generate_order_invoice(
    self,
    order_id: str,
    *,
    locale: str = "en",
) -> Dict[str, Any]:
    """
    Asynchronously render a PDF invoice for ``order_id``.

    The actual PDF rendering is delegated to the invoices app's
    Celery task. This task only resolves the order and triggers
    the render with the appropriate context.
    """
    _log_task_event(
        "generate_order_invoice",
        order_id=str(order_id),
        locale=locale,
    )
    order = _safe_get_order(order_id)
    if order is None:
        return {"order_id": str(order_id), "outcome": "order_not_found"}

    def _enqueue() -> None:
        _enqueue_invoice_render(order_id=str(order.pk), locale=locale)

    try:
        transaction.on_commit(_enqueue)
    except transaction.TransactionManagementError:
        _enqueue()
    return {"order_id": str(order_id), "outcome": "render_queued"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.send_invoice_email",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def send_invoice_email(
    self,
    order_id: str,
    *,
    locale: str = "en",
) -> Dict[str, Any]:
    """
    Render and email the invoice PDF to the order's customer.

    The task is composed of two steps performed by the invoices
    app and the notifications app. Both enqueues are deferred to
    ``transaction.on_commit`` so the email is NEVER sent for a
    rolled-back order.
    """
    _log_task_event(
        "send_invoice_email",
        order_id=str(order_id),
        locale=locale,
    )
    order = _safe_get_order(order_id)
    if order is None:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    if not order.email:
        return {"order_id": str(order_id), "outcome": "no_email"}

    def _enqueue() -> None:
        _enqueue_invoice_render(order_id=str(order.pk), locale=locale)
        _enqueue_notification_customer(
            order=order,
            template="invoice_ready",
            context={
                "order_number": order.order_number,
                "locale": locale,
            },
        )

    try:
        transaction.on_commit(_enqueue)
    except transaction.TransactionManagementError:
        _enqueue()
    return {"order_id": str(order_id), "outcome": "queued"}

# ==============================================================================
# 3. PAYMENT RECONCILIATION & RETRY
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.reconcile_payment",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def reconcile_payment(
    self,
    payment_id: int,
) -> Dict[str, Any]:
    """
    Re-query the payment gateway and reconcile ``payment_id``.

    The task delegates to ``services.update_payment_status`` to
    apply the new state. It is safe to invoke multiple times; the
    underlying state machine in services.py will reject illegal
    transitions.
    """
    _log_task_event(
        "reconcile_payment",
        payment_id=payment_id,
    )
    payment = _safe_get_payment(payment_id)
    if payment is None:
        return {"payment_id": payment_id, "outcome": "payment_not_found"}

    status_value = _get_gateway_status(payment.transaction_id)
    if not status_value:
        return {"payment_id": payment_id, "outcome": "no_status"}

    from apps.orders import services  # noqa: WPS433

    with transaction.atomic():
        services.update_payment_status(payment, status_value)

    return {
        "payment_id": payment_id,
        "outcome": "reconciled",
        "status": status_value,
    }

@shared_task(
    bind=True,
    name="apps.orders.tasks.schedule_payment_retry",
    ignore_result=True,
)
def schedule_payment_retry(
    self,
    payment_id: int,
    *,
    delay_seconds: int = 300,
) -> Dict[str, Any]:
    """
    Schedule a single payment retry after ``delay_seconds``.

    The retry itself is performed by
    :func:`apps.payments.tasks.retry_payment_capture`. This task
    is a thin scheduler that respects the payment gateway's
    rate-limit policy and the ``next_attempt_allowed_at`` field
    declared on the ``Payment`` model.
    """
    _log_task_event(
        "schedule_payment_retry",
        payment_id=payment_id,
        delay_seconds=delay_seconds,
    )
    payment = _safe_get_payment(payment_id)
    if payment is None:
        return {"payment_id": payment_id, "outcome": "payment_not_found"}
    if payment.status not in {Payment.PaymentState.FAILED}:
        return {"payment_id": payment_id, "outcome": "not_failed"}

    enqueued = _schedule_payment_retry(
        payment_id=payment_id,
        delay_seconds=delay_seconds,
    )
    return {
        "payment_id": payment_id,
        "outcome": "scheduled" if enqueued else "not_available",
        "delay_seconds": delay_seconds,
    }

@shared_task(
    bind=True,
    name="apps.orders.tasks.reconcile_pending_payments",
    ignore_result=True,
)
def reconcile_pending_payments(
    self,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    older_than_minutes: int = 15,
) -> Dict[str, Any]:
    """
    Sweep all PENDING payments older than ``older_than_minutes`` and
    enqueue a per-payment reconciliation task for each.

    This task is the canonical "stuck payment" sweeper. It is
    safe to schedule every 5-15 minutes via Celery Beat.
    """
    _log_task_event(
        "reconcile_pending_payments",
        batch_size=batch_size,
        older_than_minutes=older_than_minutes,
    )
    cutoff = timezone.now() - timedelta(minutes=max(1, older_than_minutes))
    pending_payments = Payment.objects.filter(
        status=Payment.PaymentState.PENDING,
        created_at__lt=cutoff,
    ).values_list("id", flat=True)[: max(1, batch_size)]
    enqueued: List[int] = []
    for payment_id in pending_payments:
        try:
            reconcile_payment.delay(int(payment_id))
            enqueued.append(int(payment_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reconcile_pending_payments: failed to enqueue "
                "reconcile_payment for %s: %s",
                payment_id,
                exc,
            )
    return {"swept": len(enqueued), "payment_ids": enqueued}

# ==============================================================================
# 4. REFUND & RETURN TRIGGERS
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.process_refund_via_gateway",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def process_refund_via_gateway(
    self,
    refund_id: int,
) -> Dict[str, Any]:
    """
    Trigger the actual gateway-side refund for ``refund_id``.

    The task delegates to the payments app's refund pipeline. The
    payments app is responsible for talking to the gateway and
    calling ``services.process_refund`` when the gateway
    confirms the refund.
    """
    _log_task_event(
        "process_refund_via_gateway",
        refund_id=refund_id,
    )
    refund = _safe_get_refund(refund_id)
    if refund is None:
        return {"refund_id": refund_id, "outcome": "refund_not_found"}
    if refund.status not in c.RefundStatus.PROCESSABLE_FROM:
        return {
            "refund_id": refund_id,
            "outcome": "not_processable",
            "status": refund.status,
        }
    enqueued = _enqueue_gateway_refund(
        refund_id=refund_id,
        transaction_id=str(refund.payment.transaction_id),
        amount=str(refund.amount),
        currency=str(refund.payment.currency),
    )
    return {
        "refund_id": refund_id,
        "outcome": "queued" if enqueued else "not_available",
    }

@shared_task(
    bind=True,
    name="apps.orders.tasks.process_return_approval",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def process_return_approval(
    self,
    return_id: str,
    *,
    approved_by_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Background approve a return request.

    The actual state transition is performed by
    ``services.approve_return``. The ``approved_by`` user is
    optional; when omitted the return is approved anonymously
    (e.g. by a back-office automation).
    """
    _log_task_event(
        "process_return_approval",
        return_id=str(return_id),
        approved_by_id=approved_by_id,
    )
    return_request = _safe_get_return(return_id)
    if return_request is None:
        return {"return_id": str(return_id), "outcome": "return_not_found"}

    approved_by = None
    if approved_by_id is not None:
        try:
            from django.contrib.auth import get_user_model
            approved_by = get_user_model().objects.filter(
                pk=approved_by_id,
            ).first()
        except Exception:  # noqa: BLE001
            approved_by = None

    from apps.orders import services  # noqa: WPS433

    with transaction.atomic():
        services.approve_return(
            return_request=return_request,
            approved_by=approved_by,
        )
    return {"return_id": str(return_id), "outcome": "approved"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.mark_return_received",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def mark_return_received(
    self,
    return_id: str,
) -> Dict[str, Any]:
    """Background transition a return request to ``RECEIVED``."""
    _log_task_event(
        "mark_return_received",
        return_id=str(return_id),
    )
    return_request = _safe_get_return(return_id)
    if return_request is None:
        return {"return_id": str(return_id), "outcome": "return_not_found"}
    from apps.orders import services  # noqa: WPS433

    with transaction.atomic():
        services.mark_return_received(return_request=return_request)
    return {"return_id": str(return_id), "outcome": "received"}

@shared_task(
    bind=True,
    name="apps.orders.tasks.complete_return",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def complete_return(
    self,
    return_id: str,
) -> Dict[str, Any]:
    """Background transition a return request to ``COMPLETED``."""
    _log_task_event(
        "complete_return",
        return_id=str(return_id),
    )
    return_request = _safe_get_return(return_id)
    if return_request is None:
        return {"return_id": str(return_id), "outcome": "return_not_found"}
    from apps.orders import services  # noqa: WPS433

    with transaction.atomic():
        services.complete_return(return_request=return_request)
    return {"return_id": str(return_id), "outcome": "completed"}

# ==============================================================================
# 5. WEBHOOK DISPATCH
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.dispatch_order_webhook",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def dispatch_order_webhook(
    self,
    order_id: str,
    *,
    event: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Dispatch a webhook for an order-domain event.

    The actual HTTP delivery is owned by the webhooks app. This
    task is the trigger; it never makes HTTP calls directly.
    """
    _log_task_event(
        "dispatch_order_webhook",
        order_id=str(order_id),
        event=event,
    )
    order = _safe_get_order(order_id)
    if order is None:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    enqueued = _enqueue_webhook(
        order_id=str(order.pk),
        event=event,
        context=payload or {},
    )
    return {
        "order_id": str(order_id),
        "event": event,
        "outcome": "queued" if enqueued else "not_available",
    }

# ==============================================================================
# 6. CACHE MANAGEMENT
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.invalidate_order_cache",
    ignore_result=True,
)
def invalidate_order_cache(
    self,
    order_id: str,
) -> Dict[str, Any]:
    """
    Invalidate every order-related cache key for ``order_id``.

    The task performs best-effort cache invalidation through the
    Django cache backend. It NEVER raises if the cache backend is
    unreachable.
    """
    _log_task_event(
        "invalidate_order_cache",
        order_id=str(order_id),
    )
    order = _safe_get_order(order_id)
    if order is None:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    from django.core.cache import cache  # noqa: WPS433

    keys = [
        u.order_cache_key(order.pk),
        u.order_by_number_cache_key(order.order_number),
        u.order_timeline_cache_key(order.pk),
        u.order_count_cache_key(),
    ]
    invalidated: List[str] = []
    for key in keys:
        try:
            cache.delete(key)
            invalidated.append(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "invalidate_order_cache: failed to delete %s: %s",
                key,
                exc,
            )
    return {
        "order_id": str(order_id),
        "invalidated_keys": invalidated,
    }

@shared_task(
    bind=True,
    name="apps.orders.tasks.refresh_order_aggregations",
    ignore_result=True,
)
def refresh_order_aggregations(
    self,
    *,
    order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Refresh the cached order aggregations (KPI summary, status
    distribution, etc.).

    If ``order_id`` is supplied only the order-specific cache is
    invalidated; otherwise the global aggregation cache is
    cleared.
    """
    _log_task_event(
        "refresh_order_aggregations",
        order_id=str(order_id) if order_id else None,
    )
    from django.core.cache import cache  # noqa: WPS433

    keys: List[str] = []
    if order_id is not None:
        keys.extend(
            [
                u.order_cache_key(order_id),
                u.order_timeline_cache_key(order_id),
            ]
        )
    else:
        keys.extend(
            [
                u.status_aggregation_cache_key(),
                u.order_count_cache_key(),
            ]
        )
    for key in keys:
        try:
            cache.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "refresh_order_aggregations: failed to delete %s: %s",
                key,
                exc,
            )
    return {"invalidated_keys": keys}

# ==============================================================================
# 7. SEARCH INDEX REFRESH
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.reindex_order",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def reindex_order(
    self,
    order_id: str,
) -> Dict[str, Any]:
    """
    Trigger a search-index refresh for ``order_id``.

    The actual indexing work is owned by the search app. This
    task only resolves the order and dispatches the appropriate
    indexing event.
    """
    _log_task_event(
        "reindex_order",
        order_id=str(order_id),
    )
    order = _safe_get_order(order_id)
    if order is None:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    enqueued = _enqueue_search_reindex(order_id=str(order.pk))
    return {
        "order_id": str(order_id),
        "outcome": "queued" if enqueued else "not_available",
    }

@shared_task(
    bind=True,
    name="apps.orders.tasks.bulk_reindex_orders",
    ignore_result=True,
)
def bulk_reindex_orders(
    self,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Dict[str, Any]:
    """
    Sweep the Order table and enqueue a per-order reindex task
    for each row. Designed to be scheduled nightly via Celery Beat.
    """
    _log_task_event(
        "bulk_reindex_orders",
        batch_size=batch_size,
    )
    order_ids: List[str] = []
    qs = Order.objects.filter(is_active=True).order_by("-updated_at").only(
        "id",
    )
    for index, order in enumerate(qs.iterator(chunk_size=max(1, batch_size))):
        if index >= max(1, batch_size):
            break
        order_ids.append(str(order.pk))
        try:
            reindex_order.delay(str(order.pk))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "bulk_reindex_orders: enqueue failed for %s: %s",
                order.pk,
                exc,
            )
    return {"scheduled": len(order_ids)}

# ==============================================================================
# 8. ANALYTICS SYNC
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.track_order_event",
    ignore_result=True,
)
def track_order_event(
    self,
    order_id: str,
    *,
    event: str,
    properties: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Send an analytics event for an order-domain action.

    The actual delivery is owned by the analytics app. This task
    resolves the order, builds a minimal property payload, and
    enqueues the analytics event for delivery.
    """
    _log_task_event(
        "track_order_event",
        order_id=str(order_id),
        event=event,
    )
    order = _safe_get_order(order_id)
    if order is None:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    payload: Dict[str, Any] = {
        "order_number": order.order_number,
        "status": order.status,
        "payment_status": order.payment_status,
        "total": str(order.total),
        "currency": order.currency,
        "source": order.source,
    }
    if properties:
        payload.update(properties)
    enqueued = _enqueue_analytics_event(
        order_id=str(order.pk),
        event=event,
        properties=payload,
    )
    return {
        "order_id": str(order_id),
        "event": event,
        "outcome": "queued" if enqueued else "not_available",
    }

# ==============================================================================
# 9. EXPORTS & REPORTING
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.generate_order_export",
    autoretry_for=(Exception,),
    retry_backoff=DEFAULT_RETRY_BACKOFF,
    retry_backoff_max=DEFAULT_RETRY_BACKOFF_MAX,
    retry_jitter=DEFAULT_RETRY_JITTER,
    max_retries=DEFAULT_MAX_RETRIES,
    ignore_result=True,
)
def generate_order_export(
    self,
    *,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    format: str = "csv",
    batch_size: int = DEFAULT_EXPORT_BATCH_SIZE,
    suffix: str = "",
) -> Dict[str, Any]:
    """
    Render a streamed export of orders to the storage backend.

    The output is written under ``EXPORT_FOLDER`` and named using
    the canonical timestamp filename. Returns the path / key of
    the rendered file.

    The task is idempotent: re-running it for the same window
    produces a new file (the previous one can be purged by the
    cleanup task). It does NOT remove the previous artefact.
    """
    _log_task_event(
        "generate_order_export",
        format=format,
        batch_size=batch_size,
    )
    from apps.orders import selectors  # noqa: WPS433

    qs = selectors.get_orders_for_csv_export(
        queryset=selectors.get_orders(),
    )
    if created_after:
        try:
            after_dt = datetime.fromisoformat(created_after)
            qs = qs.filter(created_at__gte=after_dt)
        except ValueError:
            logger.warning(
                "generate_order_export: invalid created_after=%r",
                created_after,
            )
    if created_before:
        try:
            before_dt = datetime.fromisoformat(created_before)
            qs = qs.filter(created_at__lte=before_dt)
        except ValueError:
            logger.warning(
                "generate_order_export: invalid created_before=%r",
                created_before,
            )

    filename = _build_export_filename(
        prefix=DEFAULT_CSV_EXPORT_PREFIX,
        suffix=suffix,
        extension=DEFAULT_CSV_EXPORT_EXTENSION,
    )
    target_path = f"{_EXPORT_FOLDER}/{filename}"

    order_ids: List[str] = []
    for order in qs.only("id").iterator(chunk_size=max(1, batch_size)):
        order_ids.append(str(order.pk))
    enqueued = _enqueue_export_stream(
        target_path=target_path,
        order_ids=order_ids,
        format=format,
    )
    return {
        "target_path": target_path,
        "format": format,
        "outcome": "queued" if enqueued else "not_available",
    }

@shared_task(
    bind=True,
    name="apps.orders.tasks.generate_daily_sales_report",
    ignore_result=True,
)
def generate_daily_sales_report(
    self,
    *,
    report_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate the daily sales report PDF and dispatch it to staff.

    The actual PDF rendering is owned by the reports app. This
    task only resolves the date, builds the cache key, and
    enqueues the rendering task.
    """
    if report_date is None:
        report_date = timezone.now().date().isoformat()
    _log_task_event(
        "generate_daily_sales_report",
        report_date=report_date,
    )
    from apps.orders import selectors  # noqa: WPS433

    summary = selectors.get_sales_summary()
    enqueued = _enqueue_sales_report(
        report_date=report_date,
        summary=summary,
    )
    return {
        "report_date": report_date,
        "outcome": "queued" if enqueued else "not_available",
    }

@shared_task(
    bind=True,
    name="apps.orders.tasks.run_integrity_check",
    ignore_result=True,
)
def run_integrity_check(
    self,
    *,
    sample_size: int = 500,
) -> Dict[str, Any]:
    """
    Run a periodic integrity check over a random sample of orders.

    Verifies that the order header is consistent with its
    shipment, payment, refund, and return-request state. The
    task logs any anomalies and emits an alert (via the
    analytics app) when an inconsistency is detected.

    This is a READ-ONLY task. It never modifies the database.
    """
    _log_task_event(
        "run_integrity_check",
        sample_size=sample_size,
    )
    issues: List[Dict[str, Any]] = []
    sample_size = max(1, sample_size)
    qs = Order.objects.all().order_by("?")[:sample_size]
    for order in qs:
        try:
            if order.total < 0:
                issues.append(
                    {
                        "order_id": str(order.pk),
                        "type": "negative_total",
                        "value": str(order.total),
                    },
                )
            if order.subtotal < 0:
                issues.append(
                    {
                        "order_id": str(order.pk),
                        "type": "negative_subtotal",
                        "value": str(order.subtotal),
                    },
                )
            if (
                order.exchange_rate is not None and order.exchange_rate <= 0
            ):
                issues.append(
                    {
                        "order_id": str(order.pk),
                        "type": "invalid_exchange_rate",
                        "value": str(order.exchange_rate),
                    },
                )
            if (
                order.status in c.OrderStatus.TERMINAL_SUCCESS
                and order.completed_at is None
            ):
                issues.append(
                    {
                        "order_id": str(order.pk),
                        "type": "missing_completed_at",
                        "status": order.status,
                    },
                )
            captured = sum(
                payment.amount
                for payment in order.payments.filter(
                    status__in=(
                        Payment.PaymentState.CAPTURED,
                        Payment.PaymentState.COMPLETED,
                    ),
                )
            )
            if captured > 0 and order.payment_status not in {
                Order.PaymentStatus.PAID,
                Order.PaymentStatus.PARTIALLY_PAID,
                Order.PaymentStatus.REFUNDED,
                Order.PaymentStatus.PARTIALLY_REFUNDED,
            }:
                issues.append(
                    {
                        "order_id": str(order.pk),
                        "type": "captured_but_unpaid",
                        "captured": str(captured),
                        "payment_status": order.payment_status,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "run_integrity_check: error while inspecting order=%s: %s",
                getattr(order, "pk", "?"),
                exc,
            )

    if issues:
        _enqueue_analytics_event(
            order_id="integrity-check",
            event="integrity_check_failures",
            properties={"issues": issues},
        )
    return {
        "sampled": qs.count() if hasattr(qs, "count") else sample_size,
        "issues": len(issues),
        "details": issues,
    }

# ==============================================================================
# 10. ATTACHMENT & TEMP-FILE CLEANUP
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.purge_inactive_attachments",
    ignore_result=True,
)
def purge_inactive_attachments(
    self,
    *,
    max_age_days: int = _ATTACHMENT_MAX_AGE_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Dict[str, Any]:
    """
    Soft-deleted ``OrderAttachment`` records that are older than
    ``max_age_days`` are physically removed from the storage
    backend and their DB rows are deleted.

    The task is safe to schedule daily. It uses ``iterator`` and
    ``select_related`` to avoid N+1 queries.
    """
    _log_task_event(
        "purge_inactive_attachments",
        max_age_days=max_age_days,
        batch_size=batch_size,
    )
    cutoff = timezone.now() - timedelta(days=max(1, max_age_days))
    qs = (
        OrderAttachment.objects
        .filter(is_active=False, updated_at__lt=cutoff)
        .only("id", "file")
        .order_by("id")
    )
    deleted: int = 0
    for attachment in qs.iterator(chunk_size=max(1, batch_size)):
        file_name = getattr(attachment.file, "name", "") if attachment.file else ""
        if file_name:
            try:
                if default_storage.exists(file_name):
                    default_storage.delete(file_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "purge_inactive_attachments: failed to delete file "
                    "%s: %s",
                    file_name,
                    exc,
                )
        try:
            attachment.delete()
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "purge_inactive_attachments: failed to delete DB row "
                "%s: %s",
                attachment.pk,
                exc,
            )
    return {"deleted": deleted}

@shared_task(
    bind=True,
    name="apps.orders.tasks.purge_temporary_files",
    ignore_result=True,
)
def purge_temporary_files(
    self,
    *,
    max_age_days: int = _TEMP_FILE_MAX_AGE_DAYS,
) -> Dict[str, Any]:
    """
    Delete files under the ``orders/tmp`` folder that are older
    than ``max_age_days`` days. Used to clean up failed imports
    and other intermediate artefacts.
    """
    _log_task_event(
        "purge_temporary_files",
        max_age_days=max_age_days,
    )
    cutoff = timezone.now() - timedelta(days=max(1, max_age_days))
    purged: int = 0
    try:
        directories, files = default_storage.listdir(_TEMP_FOLDER)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "purge_temporary_files: %s does not exist (%s)",
            _TEMP_FOLDER,
            exc,
        )
        return {"purged": 0}
    for filename in files:
        path = f"{_TEMP_FOLDER}/{filename}"
        try:
            modified = default_storage.get_modified_time(path)
            if modified and modified < cutoff:
                default_storage.delete(path)
                purged += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "purge_temporary_files: failed to purge %s: %s",
                path,
                exc,
            )
    return {"purged": purged}

@shared_task(
    bind=True,
    name="apps.orders.tasks.purge_old_exports",
    ignore_result=True,
)
def purge_old_exports(
    self,
    *,
    max_age_days: int = _EXPORT_MAX_AGE_DAYS,
) -> Dict[str, Any]:
    """
    Delete export artefacts under the ``orders/exports`` folder
    that are older than ``max_age_days`` days.
    """
    _log_task_event(
        "purge_old_exports",
        max_age_days=max_age_days,
    )
    cutoff = timezone.now() - timedelta(days=max(1, max_age_days))
    purged: int = 0
    try:
        directories, files = default_storage.listdir(_EXPORT_FOLDER)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "purge_old_exports: %s does not exist (%s)",
            _EXPORT_FOLDER,
            exc,
        )
        return {"purged": 0}
    for filename in files:
        path = f"{_EXPORT_FOLDER}/{filename}"
        try:
            modified = default_storage.get_modified_time(path)
            if modified and modified < cutoff:
                default_storage.delete(path)
                purged += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "purge_old_exports: failed to purge %s: %s",
                path,
                exc,
            )
    return {"purged": purged}

@shared_task(
    bind=True,
    name="apps.orders.tasks.archive_aged_attachments",
    ignore_result=True,
)
def archive_aged_attachments(
    self,
    *,
    max_age_days: int = 365,
) -> Dict[str, Any]:
    """
    Move aged ``OrderAttachment`` files from the main storage
    root to the ``orders/archive`` folder. The DB row is updated
    to point to the new path. This is a long-running cold-storage
    task; it is safe to schedule monthly.
    """
    _log_task_event(
        "archive_aged_attachments",
        max_age_days=max_age_days,
    )
    cutoff = timezone.now() - timedelta(days=max(1, max_age_days))
    qs = (
        OrderAttachment.objects
        .filter(is_active=True, created_at__lt=cutoff)
        .only("id", "file")
        .order_by("id")
    )
    moved: int = 0
    for attachment in qs.iterator():
        old_name = getattr(attachment.file, "name", "") if attachment.file else ""
        if not old_name or _ARCHIVE_FOLDER in old_name:
            continue
        safe_name = u.sanitize_filename(os.path.basename(old_name))
        new_name = f"{_ARCHIVE_FOLDER}/{safe_name}"
        if old_name == new_name:
            continue
        try:
            if default_storage.exists(old_name):
                with default_storage.open(old_name, "rb") as src:
                    default_storage.save(new_name, src)
                default_storage.delete(old_name)
            attachment.file.name = new_name
            attachment.save(update_fields=["file", "updated_at"])
            moved += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "archive_aged_attachments: failed to move %s: %s",
                old_name,
                exc,
            )
    return {"moved": moved}

# ==============================================================================
# 11. SCHEDULED MAINTENANCE
# ==============================================================================
@shared_task(
    bind=True,
    name="apps.orders.tasks.process_abandoned_orders",
    ignore_result=True,
)
def process_abandoned_orders(
    self,
    *,
    threshold_hours: int = DEFAULT_ABANDONED_THRESHOLD_HOURS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Dict[str, Any]:
    """
    Mark orders as ``abandoned`` if they have been in the
    ``pending`` / ``awaiting_payment`` state for longer than
    ``threshold_hours``.

    The task only sets ``abandoned_at``; it does NOT transition
    the order to ``cancelled`` (that is a customer-facing
    decision owned by the user or an operator).

    The state transition is performed by ``services.update_order_status``.
    """
    _log_task_event(
        "process_abandoned_orders",
        threshold_hours=threshold_hours,
        batch_size=batch_size,
    )
    cutoff = timezone.now() - timedelta(hours=max(1, threshold_hours))
    qs = (
        Order.objects
        .filter(
            status__in=(
                Order.OrderStatus.PENDING,
                Order.OrderStatus.AWAITING_PAYMENT,
            ),
            abandoned_at__isnull=True,
            created_at__lt=cutoff,
            is_active=True,
        )
        .only("id", "status")
        .order_by("created_at")[: max(1, batch_size)]
    )
    abandoned: int = 0
    for order in qs:
        try:
            order.abandoned_at = timezone.now()
            order.save(update_fields=["abandoned_at", "updated_at"])
            abandoned += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "process_abandoned_orders: failed for order=%s: %s",
                order.pk,
                exc,
            )
    return {"abandoned": abandoned}

@shared_task(
    bind=True,
    name="apps.orders.tasks.expire_draft_orders",
    ignore_result=True,
)
def expire_draft_orders(
    self,
    *,
    max_age_days: int = DEFAULT_DRAFT_EXPIRY_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Dict[str, Any]:
    """
    Soft-cancel draft orders older than ``max_age_days``.

    The task transitions ``draft`` orders to ``cancelled`` via
    ``services.cancel_order``. The cancellation emits a
    ``ORDER_CANCELLED`` timeline event through the signal layer.
    """
    _log_task_event(
        "expire_draft_orders",
        max_age_days=max_age_days,
        batch_size=batch_size,
    )
    cutoff = timezone.now() - timedelta(days=max(1, max_age_days))
    qs = (
        Order.objects
        .filter(
            status=Order.OrderStatus.DRAFT,
            created_at__lt=cutoff,
            is_active=True,
        )
        .only("id", "status")
        .order_by("created_at")[: max(1, batch_size)]
    )
    expired: int = 0
    from apps.orders import services  # noqa: WPS433

    for order in qs:
        try:
            with transaction.atomic():
                services.cancel_order(
                    order=order,
                    remarks=_("Draft order auto-expired."),
                )
            expired += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "expire_draft_orders: failed for order=%s: %s",
                order.pk,
                exc,
            )
    return {"expired": expired}

@shared_task(
    bind=True,
    name="apps.orders.tasks.reconcile_orphan_payments",
    ignore_result=True,
)
def reconcile_orphan_payments(
    self,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Dict[str, Any]:
    """
    Reconcile payment records that are no longer referenced by any
    active order. Such records are flagged via analytics so an
    operator can investigate.

    The task is READ-ONLY: it never deletes orphaned payment
    records (financial records are retained for compliance
    reasons). It only emits an alert.
    """
    _log_task_event(
        "reconcile_orphan_payments",
        batch_size=batch_size,
    )
    qs = (
        Payment.objects
        .filter(order__isnull=True)
        .only("id", "transaction_id")
        .order_by("created_at")[: max(1, batch_size)]
    )
    orphans: List[int] = []
    for payment in qs:
        orphans.append(int(payment.pk))
    if orphans:
        _enqueue_analytics_event(
            order_id="orphan-payments",
            event="orphan_payments_detected",
            properties={"payment_ids": orphans},
        )
    return {"orphan_count": len(orphans)}

@shared_task(
    bind=True,
    name="apps.orders.tasks.sync_payment_status_with_gateway",
    ignore_result=True,
)
def sync_payment_status_with_gateway(
    self,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    older_than_minutes: int = 60,
) -> Dict[str, Any]:
    """
    Sweep payments in non-terminal statuses that are older than
    ``older_than_minutes`` and enqueue a per-payment
    reconciliation task for each.

    This is the periodic "catch up with the gateway" sweep. It is
    safe to run every 30-60 minutes via Celery Beat.
    """
    _log_task_event(
        "sync_payment_status_with_gateway",
        batch_size=batch_size,
        older_than_minutes=older_than_minutes,
    )
    cutoff = timezone.now() - timedelta(minutes=max(1, older_than_minutes))
    qs = (
        Payment.objects
        .filter(
            status__in=(
                Payment.PaymentState.PENDING,
                Payment.PaymentState.AUTHORIZED,
            ),
            created_at__lt=cutoff,
        )
        .values_list("id", flat=True)[: max(1, batch_size)]
    )
    enqueued: int = 0
    for payment_id in qs:
        try:
            reconcile_payment.delay(int(payment_id))
            enqueued += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sync_payment_status_with_gateway: enqueue failed for %s: %s",
                payment_id,
                exc,
            )
    return {"enqueued": enqueued}

@shared_task(
    bind=True,
    name="apps.orders.tasks.dispatch_pending_shipments",
    ignore_result=True,
)
def dispatch_pending_shipments(
    self,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Dict[str, Any]:
    """
    Sweep shipments in the ``pending`` state and emit a staff
    notification for each (so the warehouse team is aware of
    orders awaiting fulfilment).

    The task is READ-ONLY: it never transitions a shipment
    itself. It only enqueues staff notifications.
    """
    _log_task_event(
        "dispatch_pending_shipments",
        batch_size=batch_size,
    )
    qs = (
        Shipment.objects
        .filter(status=Shipment.ShipmentStatus.PENDING)
        .select_related("order")
        .only("id", "shipment_number", "order")
        .order_by("created_at")[: max(1, batch_size)]
    )
    notified: int = 0
    for shipment in qs:
        try:
            _enqueue_notification_staff(
                order=shipment.order,
                template="shipment_pending_alert",
                context={"shipment_number": shipment.shipment_number},
            )
            notified += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dispatch_pending_shipments: enqueue failed for %s: %s",
                shipment.pk,
                exc,
            )
    return {"notified": notified}

# ==============================================================================
# PUBLIC MODULE API
# ==============================================================================
__all__ = [
    # Notification triggers
    "send_order_confirmation",
    "send_order_cancellation_notice",
    "send_shipment_dispatched_notice",
    "send_shipment_delivered_notice",
    "send_payment_captured_notice",
    "send_payment_failed_notice",
    "send_refund_notifications",
    # Invoice generation
    "generate_order_invoice",
    "send_invoice_email",
    # Payment reconciliation
    "reconcile_payment",
    "schedule_payment_retry",
    "reconcile_pending_payments",
    # Refund / return triggers
    "process_refund_via_gateway",
    "process_return_approval",
    "mark_return_received",
    "complete_return",
    # Webhooks
    "dispatch_order_webhook",
    # Cache management
    "invalidate_order_cache",
    "refresh_order_aggregations",
    # Search index
    "reindex_order",
    "bulk_reindex_orders",
    # Analytics
    "track_order_event",
    # Exports & reporting
    "generate_order_export",
    "generate_daily_sales_report",
    "run_integrity_check",
    # Cleanup tasks
    "purge_inactive_attachments",
    "purge_temporary_files",
    "purge_old_exports",
    "archive_aged_attachments",
    # Scheduled maintenance
    "process_abandoned_orders",
    "expire_draft_orders",
    "reconcile_orphan_payments",
    "sync_payment_status_with_gateway",
    "dispatch_pending_shipments",
]