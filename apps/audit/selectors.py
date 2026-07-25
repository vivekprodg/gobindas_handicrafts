from typing import Any, List, Optional
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet

from .models import AuditLog, SecurityEventLog

def get_audit_logs(
    actor: Optional[Any] = None,
    action: Optional[str] = None,
    severity: Optional[str] = None,
    model_class: Optional[Any] = None,
    object_id: Optional[str] = None,
) -> QuerySet[AuditLog]:
    """
    Queries filtered audit log records.
    """
    qs = AuditLog.objects.select_related("actor", "content_type")

    if actor and getattr(actor, "is_authenticated", False):
        qs = qs.filter(actor=actor)

    if action:
        qs = qs.filter(action=action)

    if severity:
        qs = qs.filter(severity=severity)

    if model_class:
        ct = ContentType.objects.get_for_model(model_class)
        qs = qs.filter(content_type=ct)

    if object_id:
        qs = qs.filter(object_id=str(object_id))

    return qs.order_by("-timestamp")

def get_audit_log_by_id(log_id: int) -> Optional[AuditLog]:
    return AuditLog.objects.filter(pk=log_id).select_related("actor", "content_type").first()

def get_unresolved_security_events() -> QuerySet[SecurityEventLog]:
    return SecurityEventLog.objects.filter(is_resolved=False).select_related("user").order_by("-created_at")