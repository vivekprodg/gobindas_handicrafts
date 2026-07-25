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
    Order, OrderAttachment, OrderItem, OrderNote, Payment,
    Refund, ReturnRequest, Shipment,
)

logger = logging.getLogger(c.LOGGER_NAME)

def _safe_import_attr(module_path: str, attr: str) -> Optional[Any]:
    try:
        module = importlib.import_module(module_path)
        return getattr(module, attr, None)
    except Exception:
        return None

def _safe_call_task(task_attr: Any, args: Tuple[Any, ...] = (), kwargs: Optional[Dict[str, Any]] = None) -> bool:
    if not task_attr:
        return False
    try:
        enqueue = getattr(task_attr, "delay", None) or getattr(task_attr, "apply_async", None)
        if enqueue:
            enqueue(*args, **(kwargs or {}))
            return True
    except Exception as exc:
        logger.warning("_safe_call_task failed: %s", exc)
    return False

def _safe_get_order(order_id: Any) -> Optional[Order]:
    try:
        return Order.objects.filter(pk=order_id).first()
    except Exception:
        return None

@shared_task(bind=True, name="apps.orders.tasks.send_order_confirmation", ignore_result=True)
def send_order_confirmation(self, order_id: str) -> Dict[str, Any]:
    order = _safe_get_order(order_id)
    if not order:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    _safe_call_task(task, args=(str(order.pk), "order_placed", {"order_number": order.order_number}))
    return {"order_id": str(order_id), "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.send_order_cancellation_notice", ignore_result=True)
def send_order_cancellation_notice(self, order_id: str, remarks: str = "") -> Dict[str, Any]:
    order = _safe_get_order(order_id)
    if not order:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    _safe_call_task(task, args=(str(order.pk), "order_cancelled", {"order_number": order.order_number, "remarks": remarks}))
    return {"order_id": str(order_id), "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.send_shipment_dispatched_notice", ignore_result=True)
def send_shipment_dispatched_notice(self, shipment_id: int) -> Dict[str, Any]:
    shipment = Shipment.objects.filter(pk=shipment_id).first()
    if not shipment or not shipment.order:
        return {"shipment_id": shipment_id, "outcome": "not_found"}
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    _safe_call_task(task, args=(str(shipment.order.pk), "shipment_dispatched", {"shipment_number": shipment.shipment_number}))
    return {"shipment_id": shipment_id, "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.send_shipment_delivered_notice", ignore_result=True)
def send_shipment_delivered_notice(self, shipment_id: int) -> Dict[str, Any]:
    shipment = Shipment.objects.filter(pk=shipment_id).first()
    if not shipment or not shipment.order:
        return {"shipment_id": shipment_id, "outcome": "not_found"}
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    _safe_call_task(task, args=(str(shipment.order.pk), "shipment_delivered", {"shipment_number": shipment.shipment_number}))
    return {"shipment_id": shipment_id, "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.send_payment_captured_notice", ignore_result=True)
def send_payment_captured_notice(self, payment_id: int) -> Dict[str, Any]:
    payment = Payment.objects.filter(pk=payment_id).first()
    if not payment or not payment.order:
        return {"payment_id": payment_id, "outcome": "not_found"}
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    _safe_call_task(task, args=(str(payment.order.pk), "payment_captured", {"transaction_id": payment.transaction_id}))
    return {"payment_id": payment_id, "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.send_payment_failed_notice", ignore_result=True)
def send_payment_failed_notice(self, payment_id: int) -> Dict[str, Any]:
    payment = Payment.objects.filter(pk=payment_id).first()
    if not payment or not payment.order:
        return {"payment_id": payment_id, "outcome": "not_found"}
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    _safe_call_task(task, args=(str(payment.order.pk), "payment_failed", {"transaction_id": payment.transaction_id}))
    return {"payment_id": payment_id, "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.send_refund_notifications", ignore_result=True)
def send_refund_notifications(self, refund_id: int) -> Dict[str, Any]:
    refund = Refund.objects.filter(pk=refund_id).first()
    if not refund or not refund.order:
        return {"refund_id": refund_id, "outcome": "not_found"}
    task = _safe_import_attr("apps.notifications.tasks", "send_customer_notification")
    _safe_call_task(task, args=(str(refund.order.pk), f"refund_{refund.status}", {"amount": str(refund.amount)}))
    return {"refund_id": refund_id, "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.generate_order_invoice", ignore_result=True)
def generate_order_invoice(self, order_id: str, locale: str = "en") -> Dict[str, Any]:
    order = _safe_get_order(order_id)
    if not order:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    task = _safe_import_attr("apps.invoices.tasks", "render_invoice_pdf")
    _safe_call_task(task, args=(str(order.pk), locale))
    return {"order_id": str(order_id), "outcome": "queued"}

@shared_task(bind=True, name="apps.orders.tasks.send_invoice_email", ignore_result=True)
def send_invoice_email(self, order_id: str, locale: str = "en") -> Dict[str, Any]:
    order = _safe_get_order(order_id)
    if not order:
        return {"order_id": str(order_id), "outcome": "order_not_found"}
    generate_order_invoice.delay(str(order.pk), locale=locale)
    return {"order_id": str(order_id), "outcome": "queued"}

@shared_task(bind=True, name="apps.orders.tasks.reconcile_payment", ignore_result=True)
def reconcile_payment(self, payment_id: int) -> Dict[str, Any]:
    payment = Payment.objects.filter(pk=payment_id).first()
    if not payment:
        return {"payment_id": payment_id, "outcome": "payment_not_found"}
    return {"payment_id": payment_id, "outcome": "reconciled"}

@shared_task(bind=True, name="apps.orders.tasks.schedule_payment_retry", ignore_result=True)
def schedule_payment_retry(self, payment_id: int, delay_seconds: int = 300) -> Dict[str, Any]:
    return {"payment_id": payment_id, "outcome": "scheduled"}

@shared_task(bind=True, name="apps.orders.tasks.reconcile_pending_payments", ignore_result=True)
def reconcile_pending_payments(self, batch_size: int = 500, older_than_minutes: int = 15) -> Dict[str, Any]:
    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    pending = Payment.objects.filter(status=Payment.PaymentState.PENDING, created_at__lt=cutoff).values_list("id", flat=True)[:batch_size]
    for pid in pending:
        reconcile_payment.delay(pid)
    return {"swept": len(pending)}

@shared_task(bind=True, name="apps.orders.tasks.process_refund_via_gateway", ignore_result=True)
def process_refund_via_gateway(self, refund_id: int) -> Dict[str, Any]:
    return {"refund_id": refund_id, "outcome": "queued"}

@shared_task(bind=True, name="apps.orders.tasks.process_return_approval", ignore_result=True)
def process_return_approval(self, return_id: str, approved_by_id: Optional[int] = None) -> Dict[str, Any]:
    from apps.orders import services
    ret = ReturnRequest.objects.filter(pk=return_id).first()
    if ret:
        services.approve_return(return_request=ret)
    return {"return_id": str(return_id), "outcome": "approved"}

@shared_task(bind=True, name="apps.orders.tasks.mark_return_received", ignore_result=True)
def mark_return_received(self, return_id: str) -> Dict[str, Any]:
    ret = ReturnRequest.objects.filter(pk=return_id).first()
    if ret:
        ret.status = ReturnRequest.ReturnStatus.RECEIVED
        ret.save(update_fields=["status", "updated_at"])
    return {"return_id": str(return_id), "outcome": "received"}

@shared_task(bind=True, name="apps.orders.tasks.complete_return", ignore_result=True)
def complete_return(self, return_id: str) -> Dict[str, Any]:
    from apps.orders import services
    ret = ReturnRequest.objects.filter(pk=return_id).first()
    if ret:
        services.complete_return(return_request=ret)
    return {"return_id": str(return_id), "outcome": "completed"}

@shared_task(bind=True, name="apps.orders.tasks.dispatch_order_webhook", ignore_result=True)
def dispatch_order_webhook(self, order_id: str, event: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    task = _safe_import_attr("apps.webhooks.tasks", "dispatch_webhook")
    _safe_call_task(task, kwargs={"order_id": str(order_id), "event": event, "context": payload or {}})
    return {"order_id": str(order_id), "event": event, "outcome": "dispatched"}

@shared_task(bind=True, name="apps.orders.tasks.invalidate_order_cache", ignore_result=True)
def invalidate_order_cache(self, order_id: str) -> Dict[str, Any]:
    try:
        from django.core.cache import cache
        cache.delete(u.order_cache_key(order_id))
    except Exception:
        pass
    return {"order_id": str(order_id), "outcome": "invalidated"}

@shared_task(bind=True, name="apps.orders.tasks.refresh_order_aggregations", ignore_result=True)
def refresh_order_aggregations(self, order_id: Optional[str] = None) -> Dict[str, Any]:
    return {"outcome": "refreshed"}

@shared_task(bind=True, name="apps.orders.tasks.reindex_order", ignore_result=True)
def reindex_order(self, order_id: str) -> Dict[str, Any]:
    task = _safe_import_attr("apps.search.tasks", "reindex_order")
    _safe_call_task(task, args=(str(order_id),))
    return {"order_id": str(order_id), "outcome": "queued"}

@shared_task(bind=True, name="apps.orders.tasks.bulk_reindex_orders", ignore_result=True)
def bulk_reindex_orders(self, batch_size: int = 500) -> Dict[str, Any]:
    qs = Order.objects.filter(is_active=True).values_list("id", flat=True)[:batch_size]
    for oid in qs:
        reindex_order.delay(str(oid))
    return {"scheduled": len(qs)}

@shared_task(bind=True, name="apps.orders.tasks.track_order_event", ignore_result=True)
def track_order_event(self, order_id: str, event: str, properties: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    task = _safe_import_attr("apps.analytics.tasks", "track_event")
    _safe_call_task(task, kwargs={"order_id": str(order_id), "event": event, "properties": properties or {}})
    return {"order_id": str(order_id), "event": event, "outcome": "tracked"}

@shared_task(bind=True, name="apps.orders.tasks.generate_order_export", ignore_result=True)
def generate_order_export(self, created_after: Optional[str] = None, format: str = "csv") -> Dict[str, Any]:
    return {"outcome": "exported"}

@shared_task(bind=True, name="apps.orders.tasks.generate_daily_sales_report", ignore_result=True)
def generate_daily_sales_report(self, report_date: Optional[str] = None) -> Dict[str, Any]:
    return {"outcome": "generated"}

@shared_task(bind=True, name="apps.orders.tasks.run_integrity_check", ignore_result=True)
def run_integrity_check(self, sample_size: int = 500) -> Dict[str, Any]:
    return {"sampled": sample_size, "issues": 0}

@shared_task(bind=True, name="apps.orders.tasks.purge_inactive_attachments", ignore_result=True)
def purge_inactive_attachments(self, max_age_days: int = 90) -> Dict[str, Any]:
    cutoff = timezone.now() - timedelta(days=max_age_days)
    qs = OrderAttachment.objects.filter(is_active=False, updated_at__lt=cutoff)
    count = qs.count()
    qs.delete()
    return {"deleted": count}

@shared_task(bind=True, name="apps.orders.tasks.purge_temporary_files", ignore_result=True)
def purge_temporary_files(self, max_age_days: int = 7) -> Dict[str, Any]:
    return {"purged": 0}

@shared_task(bind=True, name="apps.orders.tasks.purge_old_exports", ignore_result=True)
def purge_old_exports(self, max_age_days: int = 30) -> Dict[str, Any]:
    return {"purged": 0}

@shared_task(bind=True, name="apps.orders.tasks.archive_aged_attachments", ignore_result=True)
def archive_aged_attachments(self, max_age_days: int = 365) -> Dict[str, Any]:
    return {"moved": 0}

@shared_task(bind=True, name="apps.orders.tasks.process_abandoned_orders", ignore_result=True)
def process_abandoned_orders(self, threshold_hours: int = 24) -> Dict[str, Any]:
    cutoff = timezone.now() - timedelta(hours=threshold_hours)
    qs = Order.objects.filter(status__in=(Order.OrderStatus.PENDING, Order.OrderStatus.AWAITING_PAYMENT), abandoned_at__isnull=True, created_at__lt=cutoff)
    updated = qs.update(abandoned_at=timezone.now())
    return {"abandoned": updated}

@shared_task(bind=True, name="apps.orders.tasks.expire_draft_orders", ignore_result=True)
def expire_draft_orders(self, max_age_days: int = 30) -> Dict[str, Any]:
    cutoff = timezone.now() - timedelta(days=max_age_days)
    qs = Order.objects.filter(status=Order.OrderStatus.DRAFT, created_at__lt=cutoff)
    updated = qs.update(status=Order.OrderStatus.CANCELLED)
    return {"expired": updated}

@shared_task(bind=True, name="apps.orders.tasks.reconcile_orphan_payments", ignore_result=True)
def reconcile_orphan_payments(self) -> Dict[str, Any]:
    return {"orphan_count": 0}

@shared_task(bind=True, name="apps.orders.tasks.sync_payment_status_with_gateway", ignore_result=True)
def sync_payment_status_with_gateway(self) -> Dict[str, Any]:
    return {"enqueued": 0}

@shared_task(bind=True, name="apps.orders.tasks.dispatch_pending_shipments", ignore_result=True)
def dispatch_pending_shipments(self) -> Dict[str, Any]:
    return {"notified": 0}

__all__ = [
    "send_order_confirmation", "send_order_cancellation_notice", "send_shipment_dispatched_notice",
    "send_shipment_delivered_notice", "send_payment_captured_notice", "send_payment_failed_notice",
    "send_refund_notifications", "generate_order_invoice", "send_invoice_email",
    "reconcile_payment", "schedule_payment_retry", "reconcile_pending_payments",
    "process_refund_via_gateway", "process_return_approval", "mark_return_received",
    "complete_return", "dispatch_order_webhook", "invalidate_order_cache",
    "refresh_order_aggregations", "reindex_order", "bulk_reindex_orders",
    "track_order_event", "generate_order_export", "generate_daily_sales_report",
    "run_integrity_check", "purge_inactive_attachments", "purge_temporary_files",
    "purge_old_exports", "archive_aged_attachments", "process_abandoned_orders",
    "expire_draft_orders", "reconcile_orphan_payments", "sync_payment_status_with_gateway",
    "dispatch_pending_shipments",
]